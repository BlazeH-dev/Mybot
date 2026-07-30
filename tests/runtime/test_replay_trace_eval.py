from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from nanobot.agent.hook import AgentHookContext, AgentRunHookContext
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.evals.metrics import (
    CaseContext,
    OpenXmlValidationMetric,
    VisualSanityMetric,
)
from nanobot.runtime.evals.report import evaluate_cases, markdown_report
from nanobot.runtime.replay import CassetteMismatchError, CassetteProvider
from nanobot.runtime.trace import TraceHook, export_jsonl_to_otlp


class FakeProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(id="call-1", name="plan", arguments={"action": "get"})],
        )

    def get_default_model(self) -> str:
        return "fake"


@pytest.mark.asyncio
async def test_cassette_record_replay_has_no_delegate_or_network_on_replay(tmp_path: Path) -> None:
    cassette = tmp_path / "case.jsonl"
    delegate = FakeProvider()
    recorder = CassetteProvider(cassette, mode="record", delegate=delegate)
    expected = await recorder.chat(
        messages=[{"role": "user", "content": "plan"}],
        tools=[{"type": "function", "function": {"name": "plan"}}],
        model="cassette-model",
    )
    assert delegate.calls == 1
    replay = CassetteProvider(cassette, mode="replay")
    actual = await replay.chat(
        messages=[{"role": "user", "content": "plan"}],
        tools=[{"type": "function", "function": {"name": "plan"}}],
        model="cassette-model",
    )
    assert actual.tool_calls[0].name == expected.tool_calls[0].name
    assert replay.calls == 1
    replay.assert_consumed()


@pytest.mark.asyncio
async def test_cassette_mismatch_has_readable_diff(tmp_path: Path) -> None:
    cassette = tmp_path / "case.jsonl"
    recorder = CassetteProvider(cassette, mode="record", delegate=FakeProvider())
    await recorder.chat(messages=[{"role": "user", "content": "one"}], model="cassette-model")
    replay = CassetteProvider(cassette, mode="replay")
    with pytest.raises(CassetteMismatchError, match="cassette request mismatch"):
        await replay.chat(messages=[{"role": "user", "content": "two"}], model="cassette-model")


@pytest.mark.asyncio
async def test_trace_hook_writes_otel_shaped_span_and_exports(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    hook = TraceHook(
        path,
        task_id="task-1",
        actor="main",
        model="fake",
        initial_events=[{
            "name": "mybot.interaction.resumed",
            "attributes": {"mybot.human_wait_ms": 250},
        }],
    )
    run = AgentRunHookContext(messages=[{"role": "user", "content": "secret"}])
    await hook.before_run(run)
    await hook.after_iteration(AgentHookContext(
        iteration=0,
        messages=[],
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        tool_calls=[ToolCallRequest(id="c", name="read_file", arguments={"path": "x"})],
        tool_events=[{"name": "read_file", "status": "ok", "detail": "done"}],
    ))
    run.final_content = "done"
    run.stop_reason = "completed"
    run.usage = {"prompt_tokens": 10, "completion_tokens": 2}
    await hook.after_run(run)
    await hook.on_finally(run)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["gen_ai.system"] == "mybot"
    assert rows[0]["attributes"]["mybot.input.summary"]["sha256"]
    assert any(
        row.get("event.name") == "mybot.interaction.resumed"
        and row["attributes"]["mybot.human_wait_ms"] == 250
        for row in rows
    )
    assert "secret" not in path.read_text()
    output = tmp_path / "otlp.json"
    assert export_jsonl_to_otlp(path, output) == 1
    assert json.loads(output.read_text())["resourceSpans"]


def test_five_case_eval_report_has_no_hard_failures() -> None:
    root = Path("tests/fixtures/runtime_eval")
    paths = sorted(root.glob("*.json"))
    report = evaluate_cases(paths)
    repeated = evaluate_cases(list(reversed(paths)))
    assert report["case_count"] >= 5
    assert report["hard_failures"] == 0
    assert report["passed"] is True
    assert report == repeated
    assert len(report["fixture_digest"]) == 64
    rendered = markdown_report(report)
    assert "Overall: PASS" in rendered


def test_openxml_metric_parses_parts_and_rejects_broken_relationships(tmp_path: Path) -> None:
    valid = CaseContext(
        case_id="valid",
        root=Path.cwd(),
        data={"files": ["tests/fixtures/runtime_eval/office_baseline.xlsx"]},
    )
    assert OpenXmlValidationMetric().score(valid).passed

    broken = tmp_path / "broken.xlsx"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types' />",
        )
        archive.writestr(
            "_rels/.rels",
            (
                "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                "<Relationship Id='r1' Type='officeDocument' Target='xl/missing.xml' />"
                "</Relationships>"
            ),
        )
        archive.writestr(
            "xl/workbook.xml",
            "<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' />",
        )
    result = OpenXmlValidationMetric().score(CaseContext(
        case_id="broken",
        root=tmp_path,
        data={"files": ["broken.xlsx"]},
    ))
    assert not result.passed
    assert any("missing part" in issue for issue in result.issues)


def test_visual_sanity_rejects_blank_screenshot(tmp_path: Path) -> None:
    Image.new("RGB", (64, 64), "white").save(tmp_path / "blank.png")
    result = VisualSanityMetric().score(CaseContext(
        case_id="blank",
        root=tmp_path,
        data={"screenshots": ["blank.png"], "page_count": 1},
    ))
    assert not result.passed
    assert any("visually blank" in issue for issue in result.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "prompt", "tool_name"),
    [
        ("plan_automatic", "Create and automatically activate a plan.", "plan"),
        ("plan_explicit", "Create a plan-only plan and wait for confirmation.", "plan"),
        ("interaction_deadlines", "Ask a non-blocking preference question.", "request_user_input"),
        ("checkpoint_conflict", "Resume from checkpoint and avoid overwriting a changed file.", "read_file"),
    ],
)
async def test_committed_runtime_cassettes_replay_without_network(
    name: str,
    prompt: str,
    tool_name: str,
) -> None:
    provider = CassetteProvider(Path("tests/fixtures/cassettes") / f"{name}.jsonl", mode="replay")
    response = await provider.chat(
        messages=[{"role": "user", "content": prompt}],
        tools=[
            {"type": "function", "function": {"name": "plan"}},
            {"type": "function", "function": {"name": "request_user_input"}},
            {"type": "function", "function": {"name": "read_file"}},
        ],
        model="cassette-model",
    )
    assert response.tool_calls[0].name == tool_name
    provider.assert_consumed()
