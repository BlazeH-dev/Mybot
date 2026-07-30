from __future__ import annotations

import os
import time
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nanobot.agent.hook import AgentRunHookContext, CompositeHook
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config, LangfuseConfig
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.runtime.langfuse import (
    LangfuseFlushTimeoutError,
    LangfuseRuntime,
    LangfuseScoreUploadError,
    _install_bounded_flush,
    _repair_consumer_threads,
    _wait_for_queue,
    build_span_mask,
)
from nanobot.runtime.langfuse_hook import LangfuseTraceHook


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo a value."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        return f"tool-secret:{kwargs['value']}"


class _TwoTurnProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="working-secret",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(id="call-1", name="echo", arguments={"value": "body"})
                ],
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            )
        return LLMResponse(
            content="final-secret",
            finish_reason="stop",
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    def get_default_model(self) -> str:
        return "fake"


def _runtime(exporter: InMemorySpanExporter) -> LangfuseRuntime:
    return LangfuseRuntime(
        LangfuseConfig(
            enabled=True,
            public_key=f"pk-test-{uuid4().hex}",
            secret_key="sk-test",
            capture_content=False,
        ),
        span_exporter=exporter,
    )


def test_langfuse_config_defaults_env_fallback_and_secret_repr(monkeypatch) -> None:
    config = Config()
    assert config.observability.langfuse.enabled is False
    assert config.observability.langfuse.base_url == "https://jp.cloud.langfuse.com"

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://example.langfuse.test")
    langfuse = LangfuseConfig()
    assert langfuse.resolved_public_key() == "pk-env"
    assert langfuse.resolved_secret_key() == "sk-env"
    assert langfuse.resolved_base_url() == "https://example.langfuse.test"
    rendered = repr(LangfuseConfig(public_key="pk-visible", secret_key="sk-visible"))
    assert "pk-visible" not in rendered
    assert "sk-visible" not in rendered


def test_langfuse_enabled_requires_project_keys(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="publicKey/secretKey"):
        LangfuseRuntime(LangfuseConfig(enabled=True))


def test_langfuse_queue_wait_is_bounded() -> None:
    queue = Queue()
    queue.put("never-acknowledged")

    started = time.monotonic()
    with pytest.raises(LangfuseFlushTimeoutError, match="1 unfinished"):
        _wait_for_queue(queue, label="test", timeout=0.01)

    assert time.monotonic() - started < 0.5


def test_langfuse_client_internal_flush_uses_bounded_queue_wait() -> None:
    resources = SimpleNamespace(
        _shutdown=True,
        tracer_provider=None,
        _score_ingestion_queue=Queue(),
        _media_upload_queue=Queue(),
    )

    class Client:
        _resources = resources

        def flush(self) -> None:
            self._resources.flush()

    client = Client()
    _install_bounded_flush(client, timeout=0.01)
    resources._score_ingestion_queue.put("never-acknowledged")

    with pytest.raises(LangfuseFlushTimeoutError, match="score ingestion"):
        client.flush()

    resources._score_ingestion_queue.get_nowait()
    resources._score_ingestion_queue.task_done()


def test_langfuse_restarts_dead_consumer_threads() -> None:
    score_queue = Queue()
    media_queue = Queue()

    class MediaManager:
        def process_next_media_upload(self) -> None:
            try:
                media_queue.get(timeout=0.01)
            except Exception:
                return
            media_queue.task_done()

        def signal_shutdown(self, *, count: int = 1) -> None:
            for _ in range(count):
                media_queue.put(object())

    resources = SimpleNamespace(
        _shutdown=False,
        _ingestion_consumers=[],
        _media_upload_consumers=[],
        _score_ingestion_queue=score_queue,
        _score_ingestion_client=Mock(),
        _media_upload_queue=media_queue,
        _media_upload_enabled=True,
        _media_upload_thread_count=1,
        _media_manager=MediaManager(),
        public_key="pk-test",
        flush_at=1,
        flush_interval=0.01,
    )

    _repair_consumer_threads(resources)

    assert len(resources._ingestion_consumers) == 1
    assert resources._ingestion_consumers[0].is_alive()
    assert len(resources._media_upload_consumers) == 1
    assert resources._media_upload_consumers[0].is_alive()
    for consumer in resources._ingestion_consumers:
        consumer.pause()
    resources._media_manager.signal_shutdown(count=len(resources._media_upload_consumers))
    for consumer in resources._media_upload_consumers:
        consumer.pause()
    for consumer in [*resources._ingestion_consumers, *resources._media_upload_consumers]:
        consumer.join(timeout=1)


def test_benchmark_scores_use_synchronous_sdk_ingestion(monkeypatch) -> None:
    uploaded: list[list[dict]] = []
    resources = SimpleNamespace(
        add_score_task=Mock(),
        _score_ingestion_client=SimpleNamespace(
            batch_post=lambda *, batch, metadata: uploaded.append(batch),
        ),
        public_key="pk-test",
        flush_at=1,
        flush_interval=0.01,
    )
    runtime = LangfuseRuntime.__new__(LangfuseRuntime)
    runtime.client = SimpleNamespace(_resources=resources)
    original = resources.add_score_task

    ingestion = runtime.synchronous_score_ingestion()
    with ingestion:
        resources.add_score_task({"type": "score-create", "body": {"value": 1}})
    ingestion.raise_for_errors()

    assert resources.add_score_task is original
    assert uploaded == [[{"type": "score-create", "body": {"value": 1}}]]


def test_synchronous_score_ingestion_does_not_wait_for_stale_background_score() -> None:
    score_queue = Queue()
    score_queue.put("stale-unfinished-score")
    original_flush = Mock()
    resources = SimpleNamespace(
        add_score_task=Mock(),
        flush=original_flush,
        _shutdown=True,
        tracer_provider=None,
        _score_ingestion_queue=score_queue,
        _media_upload_queue=Queue(),
        _score_ingestion_client=Mock(),
        public_key="pk-test",
        flush_at=1,
        flush_interval=0.01,
    )
    runtime = LangfuseRuntime.__new__(LangfuseRuntime)
    runtime.client = SimpleNamespace(_resources=resources)

    with runtime.synchronous_score_ingestion():
        started = time.monotonic()
        resources.flush()
        assert time.monotonic() - started < 0.5
        original_flush.assert_not_called()

    assert resources.flush is original_flush


def test_benchmark_score_upload_error_is_not_silently_dropped() -> None:
    def fail_upload(**_kwargs) -> None:
        raise RuntimeError("score endpoint unavailable")

    resources = SimpleNamespace(
        add_score_task=Mock(),
        _score_ingestion_client=SimpleNamespace(batch_post=fail_upload),
        public_key="pk-test",
        flush_at=1,
        flush_interval=0.01,
    )
    runtime = LangfuseRuntime.__new__(LangfuseRuntime)
    runtime.client = SimpleNamespace(_resources=resources)

    ingestion = runtime.synchronous_score_ingestion()
    with ingestion:
        resources.add_score_task({"type": "score-create", "body": {"value": 1}})

    with pytest.raises(LangfuseScoreUploadError, match="score endpoint unavailable"):
        ingestion.raise_for_errors()


@pytest.mark.asyncio
async def test_agent_close_releases_shared_observability_without_forcing_flush() -> None:
    from nanobot.agent.loop import AgentLoop

    observability = SimpleNamespace(flush=Mock(), release=Mock())
    loop = AgentLoop.__new__(AgentLoop)
    loop._background_tasks = []
    loop._mcp_stacks = {}
    loop.observability = observability
    loop.runner = SimpleNamespace(observability=observability)
    loop.subagents = SimpleNamespace(observability=observability)

    await loop.close_mcp()

    observability.flush.assert_not_called()
    observability.release.assert_called_once_with()


@pytest.mark.asyncio
async def test_openai_compat_uses_config_driven_langfuse_drop_in(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-before")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-before")
    config = LangfuseConfig(
        enabled=True,
        public_key="pk-configured",
        secret_key="sk-configured",
        base_url="https://jp.cloud.langfuse.com",
    )
    provider = OpenAICompatProvider(
        api_key="provider-key",
        api_base="https://llm.example.test/v1",
        langfuse_config=config,
    )
    with patch("langfuse.openai.AsyncOpenAI") as client_class:
        await provider._ensure_client()
    assert client_class.call_count == 1
    assert provider.uses_langfuse_drop_in is True
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-configured"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-configured"


def test_span_mask_removes_content_and_secrets() -> None:
    from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

    identifier = OtelSpanIdentifier(trace_id="1", span_id="2")
    result = build_span_mask(False)(MaskOtelSpansParams(spans={identifier: OtelSpanData(
        trace_id="1",
        span_id="2",
        parent_span_id=None,
        name="generation",
        instrumentation_scope_name="test",
        instrumentation_scope_version="1",
        attributes={
            "langfuse.observation.input": "private body",
            "langfuse.observation.metadata.api_key": "sk-private",
            "mybot.status": "ok",
        },
        resource_attributes={},
    )}))
    patch = result.span_patches[identifier]
    assert patch is not None
    assert "langfuse.observation.input" in patch.delete_attributes
    assert "langfuse.observation.metadata.api_key" in patch.delete_attributes
    assert any(key.endswith(".sha256") for key in patch.set_attributes)
    assert "mybot.status" not in patch.delete_attributes


@pytest.mark.asyncio
async def test_runner_exports_one_generation_per_request_and_one_tool_span(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    runtime = _runtime(exporter)
    hook = LangfuseTraceHook(
        runtime,
        task_id="task-1",
        actor="main",
        model="fake",
        session_id="session-1",
    )
    tools = ToolRegistry()
    tools.register(_EchoTool())
    result = await AgentRunner(_TwoTurnProvider(), observability=runtime).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "user-secret"}],
        tools=tools,
        model="fake",
        max_iterations=3,
        max_tool_result_chars=16_000,
        hook=CompositeHook([hook]),
        workspace=tmp_path,
        task_id="task-1",
    ))
    runtime.flush()

    assert result.final_content == "final-secret"
    spans = exporter.get_finished_spans()
    generations = [span for span in spans if span.name == "mybot.llm.request"]
    tools_spans = [span for span in spans if span.name == "echo"]
    agents = [span for span in spans if span.name == "mybot.agent.run"]
    assert len(generations) == 2
    assert len(tools_spans) == 1
    assert len(agents) == 1
    assert all(span.parent and span.parent.span_id == agents[0].context.span_id for span in generations)
    assert tools_spans[0].parent and tools_spans[0].parent.span_id == agents[0].context.span_id
    exported = "\n".join(
        f"{key}={value}"
        for span in spans
        for key, value in span.attributes.items()
    )
    for secret in ("user-secret", "working-secret", "tool-secret", "final-secret"):
        assert secret not in exported
    assert "mybot.stop_reason=completed" in exported
    runtime.shutdown()


@pytest.mark.asyncio
async def test_langfuse_child_agent_inherits_parent_otel_context() -> None:
    exporter = InMemorySpanExporter()
    runtime = _runtime(exporter)
    parent = LangfuseTraceHook(runtime, task_id="task", actor="main", model="fake")
    parent_context = AgentRunHookContext(messages=[])
    await parent.before_run(parent_context)
    child = LangfuseTraceHook(runtime, task_id="task", actor="child:1", model="fake")
    child_context = AgentRunHookContext(messages=[])
    await child.before_run(child_context)
    child_context.stop_reason = "completed"
    await child.after_run(child_context)
    await child.on_finally(child_context)
    parent_context.stop_reason = "completed"
    await parent.after_run(parent_context)
    await parent.on_finally(parent_context)
    runtime.flush()

    spans = [span for span in exporter.get_finished_spans() if span.name == "mybot.agent.run"]
    assert len(spans) == 2
    root = next(span for span in spans if span.parent is None)
    nested = next(span for span in spans if span.parent is not None)
    assert nested.parent.span_id == root.context.span_id
    runtime.shutdown()
