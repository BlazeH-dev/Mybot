"""Thin Langfuse SDK integration for Mybot runtime observations."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Queue
from types import MethodType
from typing import TYPE_CHECKING, Any, Callable, Iterator

from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import LangfuseConfig


_CONTENT_ATTRIBUTE_KEYS = frozenset({
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.trace.input",
    "langfuse.trace.output",
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.request.messages",
    "gen_ai.response.messages",
})
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[._-])(secret|password|passwd|authorization|api[_-]?key|access[_-]?token)(?:$|[._-])",
    re.IGNORECASE,
)
_SUMMARY_KEY_RE = re.compile(
    r"(?:content|prompt|completion|message|arguments|params|result|output|input|reason|path|detail)",
    re.IGNORECASE,
)
_DEFAULT_FLUSH_TIMEOUT_SECONDS = 30.0


class LangfuseFlushTimeoutError(TimeoutError):
    """Raised when Langfuse cannot drain one of its local queues in time."""


class LangfuseScoreUploadError(RuntimeError):
    """Raised when a synchronous benchmark score upload fails."""


def _run_bounded(callback: Callable[[], Any], *, label: str, timeout: float) -> None:
    completed = threading.Event()
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            callback()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(target=invoke, name=f"mybot-langfuse-{label}", daemon=True)
    thread.start()
    if not completed.wait(timeout):
        raise LangfuseFlushTimeoutError(
            f"Langfuse {label} did not finish within {timeout:g} seconds"
        )
    if errors:
        raise errors[0]


def _wait_for_queue(queue: Any, *, label: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    with queue.all_tasks_done:
        while queue.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not queue.all_tasks_done.wait(remaining):
                raise LangfuseFlushTimeoutError(
                    f"Langfuse {label} queue did not drain within {timeout:g} seconds "
                    f"({queue.unfinished_tasks} unfinished)"
                )


def _repair_consumer_threads(resources: Any) -> None:
    if getattr(resources, "_shutdown", False):
        return

    ingestion_consumers = [
        consumer
        for consumer in getattr(resources, "_ingestion_consumers", [])
        if consumer.is_alive()
    ]
    if not ingestion_consumers:
        from langfuse._task_manager.score_ingestion_consumer import ScoreIngestionConsumer

        consumer = ScoreIngestionConsumer(
            ingestion_queue=resources._score_ingestion_queue,
            identifier=0,
            client=resources._score_ingestion_client,
            public_key=resources.public_key,
            flush_at=resources.flush_at,
            flush_interval=resources.flush_interval,
            max_retries=3,
        )
        consumer.start()
        ingestion_consumers.append(consumer)
        logger.warning("Restarted a stopped Langfuse score ingestion consumer")
    resources._ingestion_consumers = ingestion_consumers

    media_consumers = [
        consumer
        for consumer in getattr(resources, "_media_upload_consumers", [])
        if consumer.is_alive()
    ]
    if getattr(resources, "_media_upload_enabled", False) and not media_consumers:
        from langfuse._task_manager.media_upload_consumer import MediaUploadConsumer

        for identifier in range(resources._media_upload_thread_count):
            consumer = MediaUploadConsumer(
                identifier=identifier,
                media_manager=resources._media_manager,
            )
            consumer.start()
            media_consumers.append(consumer)
        logger.warning("Restarted stopped Langfuse media upload consumers")
    resources._media_upload_consumers = media_consumers


def _bounded_resource_flush(
    resources: Any,
    *,
    timeout: float,
    wait_for_score_queue: bool = True,
) -> None:
    _repair_consumer_threads(resources)
    tracer_provider = getattr(resources, "tracer_provider", None)
    if tracer_provider is not None:
        from opentelemetry import trace as otel_trace_api

        if not isinstance(tracer_provider, otel_trace_api.ProxyTracerProvider):
            _run_bounded(
                tracer_provider.force_flush,
                label="OTEL flush",
                timeout=timeout,
            )
    if wait_for_score_queue:
        _wait_for_queue(
            resources._score_ingestion_queue,
            label="score ingestion",
            timeout=timeout,
        )
    _wait_for_queue(
        resources._media_upload_queue,
        label="media upload",
        timeout=timeout,
    )


def _install_bounded_flush(client: Any, *, timeout: float) -> None:
    resources = getattr(client, "_resources", None)
    if resources is None:
        return

    def flush(bound_resources: Any) -> None:
        _bounded_resource_flush(bound_resources, timeout=timeout)

    resources.flush = MethodType(flush, resources)


class _SynchronousScoreIngestion:
    """Upload benchmark scores without Langfuse's stuck background consumer.

    Langfuse 4.14.1 acknowledges a queue item only after its batch request
    returns, but a consumer can remain alive while that request is stalled.
    Benchmark runs need a bounded, observable upload path, so this adapter
    reuses the SDK consumer's serializer/retry implementation synchronously.
    """

    def __init__(self, runtime: LangfuseRuntime) -> None:
        self.runtime = runtime
        self.errors: list[BaseException] = []
        self._resources: Any | None = None
        self._original_add_score_task: Any | None = None
        self._original_flush: Any | None = None
        self._consumer: Any | None = None
        self._upload_lock = threading.Lock()

    def __enter__(self) -> _SynchronousScoreIngestion:
        from langfuse._task_manager.score_ingestion_consumer import ScoreIngestionConsumer

        resources = getattr(self.runtime.client, "_resources", None)
        if resources is None:
            return self
        self._resources = resources
        self._original_add_score_task = resources.add_score_task
        self._original_flush = getattr(resources, "flush", None)
        self._consumer = ScoreIngestionConsumer(
            ingestion_queue=Queue(),
            identifier=0,
            client=resources._score_ingestion_client,
            public_key=resources.public_key,
            flush_at=resources.flush_at,
            flush_interval=min(float(resources.flush_interval or 1), 0.05),
            max_retries=3,
        )

        def upload(bound_resources: Any, event: dict[str, Any], *, force_sample: bool = False) -> None:
            del bound_resources, force_sample
            with self._upload_lock:
                queue = self._consumer._ingestion_queue
                queue.put(event)
                batch: list[dict[str, Any]] = []
                try:
                    batch = self._consumer._next()
                    if batch:
                        self._consumer._upload_batch(batch)
                except BaseException as exc:
                    self.errors.append(exc)
                finally:
                    for _ in batch:
                        queue.task_done()

        resources.add_score_task = MethodType(upload, resources)

        def flush_without_background_scores(bound_resources: Any) -> None:
            # Scores created in this context are uploaded synchronously above,
            # so an older unfinished background task must not prevent
            # run_experiment() from returning its otherwise complete result.
            _bounded_resource_flush(
                bound_resources,
                timeout=_DEFAULT_FLUSH_TIMEOUT_SECONDS,
                wait_for_score_queue=False,
            )

        if self._original_flush is not None:
            resources.flush = MethodType(flush_without_background_scores, resources)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._resources is not None and self._original_add_score_task is not None:
            self._resources.add_score_task = self._original_add_score_task
        if self._resources is not None and self._original_flush is not None:
            self._resources.flush = self._original_flush

    def raise_for_errors(self) -> None:
        if self.errors:
            raise LangfuseScoreUploadError(
                f"Langfuse benchmark score upload failed ({len(self.errors)} event(s)): "
                f"{self.errors[0]}"
            ) from self.errors[0]


def value_summary(value: Any) -> dict[str, Any]:
    """Return a stable content-free digest for arbitrary structured data."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "chars": len(raw),
    }


def _safe_metadata(value: Any, *, capture_content: bool, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(item_key): _safe_metadata(
                item_value,
                capture_content=capture_content,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
            if not _SENSITIVE_KEY_RE.search(str(item_key))
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_metadata(item, capture_content=capture_content, key=key)
            for item in value
        ]
    semantic_key = key.lower().replace("-", "_")
    preserve_semantic_value = semantic_key.endswith((
        "stop_reason",
        "finish_reason",
        "status",
        "kind",
        "type",
        "mode",
    ))
    if (
        not capture_content
        and key
        and not preserve_semantic_value
        and _SUMMARY_KEY_RE.search(key)
    ):
        return value_summary(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def build_span_mask(capture_content: bool):
    """Build the Langfuse export-stage span masking callback."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    def _mask(params):
        patches = {}
        for identifier, span in params.spans.items():
            delete: list[str] = []
            replacements: dict[str, str | int] = {}
            for key, value in span.attributes.items():
                should_mask_content = not capture_content and (
                    key in _CONTENT_ATTRIBUTE_KEYS
                    or key.startswith("gen_ai.prompt")
                    or key.startswith("gen_ai.completion")
                )
                if should_mask_content or _SENSITIVE_KEY_RE.search(key):
                    summary = value_summary(value)
                    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
                    replacements[f"mybot.masked.{suffix}.sha256"] = summary["sha256"]
                    replacements[f"mybot.masked.{suffix}.chars"] = summary["chars"]
                    delete.append(key)
            if delete:
                patches[identifier] = OtelSpanPatch(
                    set_attributes=replacements,
                    delete_attributes=delete,
                )
        return MaskOtelSpansResult(span_patches=patches)

    return _mask


def configure_langfuse_environment(config: LangfuseConfig) -> None:
    """Expose enabled Config credentials to the Langfuse OpenAI drop-in."""
    if not config.enabled:
        return
    public_key = config.resolved_public_key()
    secret_key = config.resolved_secret_key()
    if public_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    if secret_key:
        os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    os.environ["LANGFUSE_BASE_URL"] = config.resolved_base_url()


@dataclass(slots=True)
class ObservationHandle:
    """Fail-open wrapper around a Langfuse observation."""

    observation: Any | None

    def update(self, **kwargs: Any) -> None:
        if self.observation is None:
            return
        try:
            self.observation.update(**kwargs)
        except Exception:
            logger.exception("Langfuse observation update failed")

    def mark_completion_started(self) -> None:
        self.update(completion_start_time=datetime.now(timezone.utc))


_CURRENT_RUNTIME: contextvars.ContextVar[LangfuseRuntime | None] = contextvars.ContextVar(
    "mybot_langfuse_runtime",
    default=None,
)
_CURRENT_OBSERVATION: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "mybot_langfuse_observation",
    default=None,
)


class LangfuseRuntime:
    """Own one configured Langfuse client and Mybot's content policy."""

    def __init__(self, config: LangfuseConfig, *, span_exporter: Any | None = None) -> None:
        if not config.enabled:
            raise ValueError("LangfuseRuntime requires observability.langfuse.enabled=true")
        public_key = config.resolved_public_key()
        secret_key = config.resolved_secret_key()
        if not public_key or not secret_key:
            raise ValueError(
                "Langfuse is enabled but publicKey/secretKey are missing; configure them in "
                "observability.langfuse or LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY"
            )
        configure_langfuse_environment(config)
        from langfuse import Langfuse

        self.capture_content = config.capture_content
        self.base_url = config.resolved_base_url()
        self._registry_key: tuple[str, str, str, bool] | None = None
        self._ref_count = 0
        self.client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=self.base_url,
            mask_otel_spans=build_span_mask(config.capture_content),
            span_exporter=span_exporter,
        )
        _install_bounded_flush(
            self.client,
            timeout=_DEFAULT_FLUSH_TIMEOUT_SECONDS,
        )

    @classmethod
    def acquire(cls, config: LangfuseConfig) -> LangfuseRuntime:
        """Return the process singleton for one Langfuse project configuration."""
        public_key = config.resolved_public_key() or ""
        secret_key = config.resolved_secret_key() or ""
        key = (public_key, secret_key, config.resolved_base_url(), config.capture_content)
        with _RUNTIME_REGISTRY_LOCK:
            runtime = _RUNTIME_REGISTRY.get(key)
            if runtime is None:
                runtime = cls(config)
                runtime._registry_key = key
                _RUNTIME_REGISTRY[key] = runtime
            runtime._ref_count += 1
            return runtime

    def content(self, value: Any) -> Any:
        return value if self.capture_content else value_summary(value)

    def metadata(self, value: dict[str, Any] | None) -> dict[str, Any]:
        return _safe_metadata(
            value or {},
            capture_content=self.capture_content,
        )

    @contextmanager
    def observation(self, **kwargs: Any) -> Iterator[ObservationHandle]:
        cm = None
        try:
            if "input" in kwargs:
                kwargs["input"] = self.content(kwargs["input"])
            if "metadata" in kwargs:
                kwargs["metadata"] = self.metadata(kwargs["metadata"])
            cm = self.client.start_as_current_observation(**kwargs)
            observation = cm.__enter__()
        except Exception:
            logger.exception("Langfuse observation start failed")
            observation = None
            cm = None
        token = _CURRENT_OBSERVATION.set(observation)
        try:
            yield ObservationHandle(observation)
        except BaseException as exc:
            if observation is not None:
                try:
                    observation.update(
                        level="ERROR",
                        status_message=f"{type(exc).__name__}: {exc}"[:500],
                    )
                except Exception:
                    logger.exception("Langfuse observation error update failed")
            raise
        finally:
            _CURRENT_OBSERVATION.reset(token)
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    logger.exception("Langfuse observation close failed")

    def create_event(self, name: str, attributes: dict[str, Any] | None = None) -> bool:
        observation = _CURRENT_OBSERVATION.get()
        if observation is None:
            return False
        try:
            observation.create_event(name=name, metadata=self.metadata(attributes))
        except Exception:
            logger.exception("Langfuse event creation failed")
        return True

    def bind(self) -> contextvars.Token[LangfuseRuntime | None]:
        return _CURRENT_RUNTIME.set(self)

    @staticmethod
    def reset(token: contextvars.Token[LangfuseRuntime | None]) -> None:
        _CURRENT_RUNTIME.reset(token)

    def flush(self, *, strict: bool = False) -> None:
        try:
            self.client.flush()
        except Exception:
            logger.exception("Langfuse flush failed")
            if strict:
                raise

    def synchronous_score_ingestion(self) -> _SynchronousScoreIngestion:
        """Return a bounded score uploader for benchmark Experiment runs."""
        return _SynchronousScoreIngestion(self)

    def shutdown(self) -> None:
        try:
            self.client.shutdown()
        except Exception:
            logger.exception("Langfuse shutdown failed")

    def release(self) -> None:
        """Release one owner and shut down the SDK client after the last owner."""
        key = self._registry_key
        if key is None:
            self.shutdown()
            return
        should_shutdown = False
        with _RUNTIME_REGISTRY_LOCK:
            self._ref_count = max(0, self._ref_count - 1)
            if self._ref_count == 0:
                _RUNTIME_REGISTRY.pop(key, None)
                self._registry_key = None
                should_shutdown = True
        if should_shutdown:
            self.shutdown()


def current_langfuse_runtime() -> LangfuseRuntime | None:
    return _CURRENT_RUNTIME.get()


def emit_langfuse_event(name: str, attributes: dict[str, Any] | None = None) -> bool:
    runtime = current_langfuse_runtime()
    return runtime.create_event(name, attributes) if runtime is not None else False


_RUNTIME_REGISTRY: dict[tuple[str, str, str, bool], LangfuseRuntime] = {}
_RUNTIME_REGISTRY_LOCK = threading.Lock()
