"""Thin Langfuse SDK integration for Mybot runtime observations."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

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
