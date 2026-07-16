"""Compile the Office DSL to guarded OfficeCLI batch commands and execute them."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nanobot.officecli_runtime import OFFICECLI_DIR_ENV, get_officecli_runtime_dir
from nanobot.skills._shared.office_core.common import (
    load_facts,
    read_json,
    render_text_value,
    replace_fact_placeholders,
    write_json,
)

OFFICECLI_CONTRACT = read_json(
    Path(__file__).resolve().parent.parent / "references" / "officecli-runtime.json"
)
SUPPORTED_OFFICECLI_VERSION = str(OFFICECLI_CONTRACT["validated_version"])
OFFICECLI_BINARY_ENV = "OFFICECLI_BIN"


class OfficeCliBackendError(RuntimeError):
    """Raised when the pinned OfficeCLI backend cannot safely complete a request."""


@dataclass(frozen=True)
class OfficeCliInfo:
    binary: str
    version: str


def _json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_binary(explicit: str | None) -> str:
    candidate = explicit or os.environ.get(OFFICECLI_BINARY_ENV) or "officecli"
    if os.sep in candidate or (os.altsep and os.altsep in candidate):
        path = Path(candidate).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise OfficeCliBackendError(f"OfficeCLI binary is not executable: {path}")
        return str(path)

    resolved = shutil.which(candidate)
    if resolved is None:
        raise OfficeCliBackendError(
            "OfficeCLI is required for the officecli skill. Mybot must provide the pinned "
            f"version {SUPPORTED_OFFICECLI_VERSION}, or set {OFFICECLI_BINARY_ENV}."
        )
    return resolved


def get_officecli_info(
    binary: str | None = None,
    *,
    allow_unverified_version: bool = False,
) -> OfficeCliInfo:
    resolved = _resolve_binary(binary)
    result = subprocess.run(
        [resolved, "--version"],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
        env={**os.environ, "OFFICECLI_SKIP_UPDATE": "1"},
    )
    if result.returncode != 0:
        raise OfficeCliBackendError(
            f"OfficeCLI version probe failed with exit {result.returncode}: {result.stderr.strip()}"
        )
    version = result.stdout.strip()
    if version != SUPPORTED_OFFICECLI_VERSION and not allow_unverified_version:
        raise OfficeCliBackendError(
            f"OfficeCLI {version!r} is not the validated version "
            f"{SUPPORTED_OFFICECLI_VERSION!r}. Upgrade the project contract first or pass "
            "--allow-unverified-officecli for an explicit local experiment."
        )
    return OfficeCliInfo(binary=resolved, version=version)


def _paragraph(text: str, **props: str) -> dict[str, Any]:
    return {
        "command": "add",
        "parent": "/body",
        "type": "paragraph",
        "props": {"text": text, **props},
    }


def _set_table_cell(table_index: int, row: int, column: int, text: str, **props: str) -> dict[str, Any]:
    return {
        "command": "set",
        "path": f"/body/tbl[{table_index}]/tr[{row}]/tc[{column}]",
        "props": {"text": text, **props},
    }


def compile_report_commands(
    dsl: dict[str, Any],
    facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compile report DSL into the safe OfficeCLI DOM subset used by Mybot."""
    commands: list[dict[str, Any]] = []
    title = replace_fact_placeholders(str(dsl.get("title", "Report")), facts)
    commands.append(_paragraph(title, style="Title"))

    subtitle = dsl.get("subtitle")
    if isinstance(subtitle, str) and subtitle.strip():
        commands.append(
            _paragraph(
                replace_fact_placeholders(subtitle, facts),
                italic="true",
                color="666666",
            )
        )

    table_index = 0
    for section in dsl.get("sections", []):
        if not isinstance(section, dict):
            continue
        section_title = replace_fact_placeholders(str(section.get("title", "")), facts)
        commands.append(_paragraph(section_title, style="Heading1"))
        for block in section.get("blocks", []):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "paragraph":
                text = replace_fact_placeholders(str(block.get("text", "")), facts)
                commands.append(_paragraph(text))
            elif block_type == "bullets":
                for item in block.get("items", []):
                    text = item.get("text") if isinstance(item, dict) else item
                    commands.append(
                        _paragraph(
                            replace_fact_placeholders(str(text), facts),
                            listStyle="bullet",
                        )
                    )
            elif block_type == "metrics":
                items = [item for item in block.get("items", []) if isinstance(item, dict)]
                table_index += 1
                commands.append(
                    {
                        "command": "add",
                        "parent": "/body",
                        "type": "table",
                        "props": {
                            "rows": str(len(items) + 1),
                            "cols": "2",
                            "style": "TableGrid",
                            "layout": "autofit",
                        },
                    }
                )
                commands.extend(
                    [
                        _set_table_cell(table_index, 1, 1, "Metric", bold="true", shd="D9EAF7"),
                        _set_table_cell(table_index, 1, 2, "Value", bold="true", shd="D9EAF7"),
                    ]
                )
                for row, item in enumerate(items, start=2):
                    label = str(item.get("label", item.get("fact_ref", "")))
                    value = render_text_value({"fact_ref": item.get("fact_ref")}, facts)
                    commands.append(_set_table_cell(table_index, row, 1, label))
                    commands.append(_set_table_cell(table_index, row, 2, value, bold="true"))
            elif block_type == "table":
                headers = block.get("headers", [])
                rows = block.get("rows", [])
                if not isinstance(headers, list) or not headers:
                    continue
                source_rows = [row for row in rows if isinstance(row, list)]
                table_index += 1
                commands.append(
                    {
                        "command": "add",
                        "parent": "/body",
                        "type": "table",
                        "props": {
                            "rows": str(len(source_rows) + 1),
                            "cols": str(len(headers)),
                            "style": "TableGrid",
                            "layout": "autofit",
                        },
                    }
                )
                for column, header in enumerate(headers, start=1):
                    commands.append(
                        _set_table_cell(
                            table_index,
                            1,
                            column,
                            str(header),
                            bold="true",
                            shd="D9EAF7",
                        )
                    )
                for row_index, source_row in enumerate(source_rows, start=2):
                    for column, value in enumerate(source_row[: len(headers)], start=1):
                        commands.append(
                            _set_table_cell(
                                table_index,
                                row_index,
                                column,
                                render_text_value(value, facts),
                            )
                        )
    return commands


def _slide_shape(slide: int, name: str, text: str, **props: str) -> dict[str, Any]:
    return {
        "command": "add",
        "parent": f"/slide[{slide}]",
        "type": "shape",
        "props": {
            "name": name,
            "text": text,
            "geometry": "rect",
            "fill": "FFFFFF",
            "opacity": "0",
            "lineOpacity": "0",
            **props,
        },
    }


def compile_slide_commands(
    dsl: dict[str, Any],
    facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compile slide DSL into a bounded OfficeCLI shape vocabulary."""
    commands: list[dict[str, Any]] = []
    slides = dsl.get("slides", [])
    for slide_index, payload in enumerate(slides, start=1):
        if not isinstance(payload, dict):
            continue
        commands.append({"command": "add", "parent": "/", "type": "slide", "props": {}})

        title = replace_fact_placeholders(str(payload.get("title", "")), facts)
        commands.append(
            _slide_shape(
                slide_index,
                "mybot-title",
                title,
                x="1.1cm",
                y="0.65cm",
                width="23.2cm",
                height="1.5cm",
                size="28",
                bold="true",
                color="1F2937",
                valign="middle",
            )
        )

        subtitle = payload.get("subtitle")
        if isinstance(subtitle, str) and subtitle.strip():
            commands.append(
                _slide_shape(
                    slide_index,
                    "mybot-subtitle",
                    replace_fact_placeholders(subtitle, facts),
                    x="1.2cm",
                    y="2.1cm",
                    width="22.8cm",
                    height="0.8cm",
                    size="13",
                    color="667085",
                    valign="middle",
                )
            )

        metrics = [item for item in payload.get("metrics", []) if isinstance(item, dict)][:3]
        for metric_index, metric in enumerate(metrics):
            left = 1.4 + metric_index * 7.9
            label = str(metric.get("label", metric.get("fact_ref", "")))
            value = render_text_value({"fact_ref": metric.get("fact_ref")}, facts)
            commands.append(
                {
                    "command": "add",
                    "parent": f"/slide[{slide_index}]",
                    "type": "shape",
                    "props": {
                        "name": f"mybot-metric-{metric_index + 1}",
                        "geometry": "roundRect",
                        "text": f"{label}\n{value}",
                        "x": f"{left:.1f}cm",
                        "y": "3.6cm",
                        "width": "7.0cm",
                        "height": "3.1cm",
                        "size": "20",
                        "bold": "true",
                        "color": "1F2937",
                        "fill": "E8F0FE",
                        "opacity": "1",
                        "line": "486082",
                        "lineWidth": "1.2",
                        "lineOpacity": "0.8",
                        "align": "center",
                        "valign": "middle",
                    },
                }
            )

        bullets = payload.get("bullets", [])
        if isinstance(bullets, list) and bullets:
            bullet_lines: list[str] = []
            for item in bullets:
                text = item.get("text") if isinstance(item, dict) else item
                bullet_lines.append(f"• {replace_fact_placeholders(str(text), facts)}")
            commands.append(
                _slide_shape(
                    slide_index,
                    "mybot-bullets",
                    "\n".join(bullet_lines),
                    x="1.5cm",
                    y="7.5cm" if metrics else "3.5cm",
                    width="22.0cm",
                    height="9.5cm" if metrics else "12.0cm",
                    size="18",
                    color="344054",
                    valign="top",
                    margin="0.2cm",
                )
            )

        notes = payload.get("speaker_notes")
        if isinstance(notes, str) and notes.strip():
            commands.append(
                {
                    "command": "add",
                    "parent": f"/slide[{slide_index}]",
                    "type": "notes",
                    "props": {"text": replace_fact_placeholders(notes, facts)},
                }
            )
    return commands


def compile_commands(
    kind: Literal["docx", "pptx"],
    *,
    dsl_path: Path,
    facts_path: Path,
) -> list[dict[str, Any]]:
    dsl = read_json(dsl_path)
    facts = load_facts(facts_path)
    if kind == "docx":
        commands = compile_report_commands(dsl, facts)
    else:
        commands = compile_slide_commands(dsl, facts)

    allowed = set(OFFICECLI_CONTRACT["allowed_batch_operations"])
    disallowed = sorted(
        {
            str(command.get("command"))
            for command in commands
            if command.get("command") not in allowed
        }
    )
    if disallowed:
        raise OfficeCliBackendError(
            f"OfficeCLI compiler emitted operation(s) outside the contract: {disallowed}"
        )
    return commands


class _OfficeCliRunner:
    def __init__(self, info: OfficeCliInfo, *, timeout_seconds: int = 90) -> None:
        self.info = info
        self.timeout_seconds = timeout_seconds
        self._home = tempfile.TemporaryDirectory(prefix="mybot-officecli-")
        runtime_dir = os.environ.get(OFFICECLI_DIR_ENV) or str(get_officecli_runtime_dir())
        runtime_environment = OFFICECLI_CONTRACT.get("runtime_environment", {})
        contract_env = (
            {str(key): str(value) for key, value in runtime_environment.items()}
            if isinstance(runtime_environment, dict)
            else {}
        )
        self.env = {
            **os.environ,
            **contract_env,
            "HOME": self._home.name,
            OFFICECLI_DIR_ENV: runtime_dir,
        }

    def close(self) -> None:
        self._home.cleanup()

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)

    @staticmethod
    def _completed_output_exists(path: Path | None) -> bool:
        if path is None or not path.is_file() or path.stat().st_size == 0:
            return False
        if path.suffix.lower() == ".png":
            with path.open("rb") as handle:
                return handle.read(8) == b"\x89PNG\r\n\x1a\n"
        return True

    def run(
        self,
        args: list[str],
        *,
        accept_failure: bool = False,
        timeout_seconds: int | None = None,
        completed_output_path: Path | None = None,
    ) -> dict[str, Any]:
        # Some OfficeCLI view commands start a browser helper. A descendant may keep
        # inherited stdout/stderr pipes open after the CLI itself exits, which makes
        # subprocess.run(capture_output=True) wait until timeout despite a completed
        # screenshot. Regular temporary files avoid that pipe-lifetime coupling.
        with (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file,
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file,
        ):
            process = subprocess.Popen(
                [self.info.binary, *args, "--json"],
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
                env=self.env,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            timeout = timeout_seconds or self.timeout_seconds
            timed_out_after_output = False
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate_process_tree(process)
                if not self._completed_output_exists(completed_output_path):
                    stderr_file.seek(0)
                    detail = stderr_file.read().strip()
                    suffix = f": {detail}" if detail else ""
                    raise OfficeCliBackendError(
                        f"OfficeCLI {args[0]!r} timed out after {timeout}s{suffix}"
                    ) from None
                returncode = 124
                timed_out_after_output = True
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
        if timed_out_after_output:
            payload = {
                "success": True,
                "data": str(completed_output_path),
                "message": "Output completed; terminated a lingering OfficeCLI child process.",
            }
            return {
                "argv": args,
                "exit_code": returncode,
                "stdout": payload,
                "stderr": stderr.strip(),
                "timed_out_after_output": True,
            }
        try:
            payload = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError as exc:
            if returncode == 0 and self._completed_output_exists(completed_output_path):
                payload = {
                    "success": True,
                    "data": str(completed_output_path),
                    "message": stdout.strip() or str(completed_output_path),
                }
                return {
                    "argv": args,
                    "exit_code": returncode,
                    "stdout": payload,
                    "stderr": stderr.strip(),
                    "unstructured_output": True,
                }
            raise OfficeCliBackendError(
                f"OfficeCLI returned invalid JSON for {args[0]!r}: {stdout[:500]!r}"
            ) from exc

        success = returncode == 0 and payload.get("success") is not False
        if not success and not accept_failure:
            detail = payload.get("message") or stderr.strip() or stdout.strip()
            raise OfficeCliBackendError(
                f"OfficeCLI {args[0]!r} failed with exit {returncode}: {detail}"
            )
        serialized = json.dumps(payload, ensure_ascii=False)
        if "WARNING: UNSUPPORTED" in serialized:
            raise OfficeCliBackendError(
                f"OfficeCLI {args[0]!r} accepted the batch with unsupported properties; "
                "update the DSL compiler instead of silently degrading the document."
            )
        return {
            "argv": args,
            "exit_code": returncode,
            "stdout": payload,
            "stderr": stderr.strip(),
        }


def render_with_officecli(
    kind: Literal["docx", "pptx"],
    *,
    dsl_path: Path,
    facts_path: Path,
    output_path: Path,
    binary: str | None = None,
    allow_unverified_version: bool = False,
    batch_output_path: Path | None = None,
    validation_output_path: Path | None = None,
    preview_dir: Path | None = None,
) -> dict[str, Any]:
    """Render one artifact and persist the reproducibility sidecars."""
    info = get_officecli_info(binary, allow_unverified_version=allow_unverified_version)
    commands = compile_commands(kind, dsl_path=dsl_path, facts_path=facts_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    batch_path = batch_output_path or output_path.with_suffix(f".{kind}.officecli-batch.json")
    validation_path = validation_output_path or output_path.with_suffix(
        f".{kind}.officecli-validation.json"
    )
    run_path = output_path.with_suffix(f".{kind}.officecli-run.json")
    write_json(batch_path, commands)

    runner = _OfficeCliRunner(info)
    events: list[dict[str, Any]] = []
    try:
        events.append(runner.run(["create", str(output_path), "--force"]))
        events.append(
            runner.run(
                [
                    "batch",
                    str(output_path),
                    "--input",
                    str(batch_path),
                    "--stop-on-error",
                ]
            )
        )
        events.append(runner.run(["close", str(output_path)]))
        validation = runner.run(["validate", str(output_path)], accept_failure=True)
        events.append(validation)
        write_json(validation_path, validation)
        if validation["exit_code"] != 0 or validation["stdout"].get("success") is False:
            raise OfficeCliBackendError(
                f"OfficeCLI generated {output_path.name}, but OpenXML validation failed; "
                f"see {validation_path}"
            )

        previews: list[str] = []
        if preview_dir is not None:
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_path = preview_dir / f"{output_path.stem}.png"
            screenshot = runner.run(
                ["view", str(output_path), "screenshot", "-o", str(preview_path)],
                timeout_seconds=30,
                completed_output_path=preview_path,
            )
            events.append(screenshot)
            previews = [str(path) for path in sorted(preview_dir.glob(f"{output_path.stem}*.png"))]

        metadata = {
            "schema_version": 1,
            "engine": "officecli",
            "engine_version": info.version,
            "binary": info.binary,
            "kind": kind,
            "output": str(output_path),
            "batch": str(batch_path),
            "batch_sha256": _json_sha256(commands),
            "validation": str(validation_path),
            "previews": previews,
            "events": events,
        }
        write_json(run_path, metadata)
        return metadata
    finally:
        try:
            runner.run(["close", str(output_path)], accept_failure=True)
        except (OfficeCliBackendError, subprocess.SubprocessError):
            pass
        runner.close()
