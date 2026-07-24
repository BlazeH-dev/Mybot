"""Langfuse-backed public Office benchmark commands."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.runtime.langfuse import LangfuseRuntime

benchmark_app = typer.Typer(help="Prepare, estimate, run, and export Office benchmarks")
console = Console()
_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_PATH = _ROOT / "benchmarks" / "office" / "profiles.json"
_CONSTRAINTS_PATH = _ROOT / "benchmarks" / "office" / "constraints.txt"
_PROFILES = frozenset({"ci", "office-smoke", "office-release"})
_BENCHMARKS = ("ocb", "officebench", "presentbench")
_SKILLS = ("officecli", "office-python")
_PRESENTBENCH_SAMPLES = frozenset({60, 119, 238})
_CLOUD_READBACK_ATTEMPTS = 30
_CLOUD_READBACK_INTERVAL_SEC = 2
_WORKSPACES: dict[str, Path] = {}
_WORKSPACES_LOCK = threading.Lock()


class BenchmarkError(RuntimeError):
    pass


def _manifest() -> dict[str, Any]:
    return json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))


def _validate_profile(profile: str) -> None:
    if profile not in _PROFILES:
        raise BenchmarkError(f"unknown profile {profile!r}; choose one of {sorted(_PROFILES)}")


def _select_values(values: list[str] | None, allowed: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not values:
        return allowed
    invalid = sorted(set(values) - set(allowed))
    if invalid:
        raise BenchmarkError(f"unknown {label}: {invalid}; choose from {list(allowed)}")
    return tuple(dict.fromkeys(values))


def _cache_root(cache_dir: Path | None) -> Path:
    return (cache_dir or Path(os.environ.get(
        "NANOBOT_BENCHMARK_CACHE",
        Path.home() / ".cache" / "nanobot" / "benchmarks",
    ))).expanduser().resolve()


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise BenchmarkError(f"command failed ({' '.join(command)}): {detail[-1200:]}")
    return completed.stdout.strip()


def _clone_at_revision(name: str, spec: dict[str, Any], root: Path) -> Path:
    target = root / "sources" / name
    if not (target / ".git").is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", spec["url"], str(target)])
    _run(["git", "fetch", "--depth=1", "origin", spec["revision"]], cwd=target)
    _run(["git", "checkout", "--detach", "--force", spec["revision"]], cwd=target)
    actual = _run(["git", "rev-parse", "HEAD"], cwd=target)
    if actual != spec["revision"]:
        raise BenchmarkError(f"{name} revision mismatch: expected {spec['revision']}, got {actual}")
    license_path = target / "LICENSE"
    digest = hashlib.sha256(license_path.read_bytes()).hexdigest()
    if digest != spec["license_sha256"]:
        raise BenchmarkError(f"{name} LICENSE digest mismatch: {digest}")
    return target


def _ensure_benchmark_venv(root: Path) -> Path:
    venv = root / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        _run([sys.executable, "-m", "venv", str(venv)])
    constraints_copy = root / "constraints.txt"
    shutil.copyfile(_CONSTRAINTS_PATH, constraints_copy)
    return python


def _benchmark_python_env() -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_ROOT) + (os.pathsep + current if current else "")
    return env


def _download_hf_snapshot(
    python: Path,
    spec: dict[str, Any],
    target: Path,
    allow_patterns: list[str],
) -> Path:
    script = (
        "import sys; from huggingface_hub import snapshot_download; "
        "print(snapshot_download(repo_id=sys.argv[1], repo_type='dataset', "
        "revision=sys.argv[2], local_dir=sys.argv[3], allow_patterns=sys.argv[4:]))"
    )
    target.mkdir(parents=True, exist_ok=True)
    dataset_id = spec.get("dataset_id")
    if not dataset_id:
        raise BenchmarkError("benchmark profile is missing a pinned HuggingFace dataset_id")
    output = _run(
        [str(python), "-c", script, dataset_id, spec["dataset_revision"], str(target), *allow_patterns],
        env=_benchmark_python_env(),
    )
    return Path(output.splitlines()[-1]).resolve()


def _materialize_manifest(
    python: Path,
    adapter: str,
    source: Path,
    target: Path,
    *,
    case_ids: list[int] | None = None,
    cases: list[str] | None = None,
) -> None:
    function = {
        "ocb": "materialize_ocb",
        "officebench": "materialize_officebench",
        "presentbench": "materialize_presentbench",
    }[adapter]
    kwargs: dict[str, Any] = {}
    if adapter == "ocb":
        kwargs["case_ids"] = case_ids
    else:
        kwargs["cases"] = cases
    script = (
        "import json, sys; from nanobot.benchmark_adapters import "
        + function
        + "; args=sys.argv; "
        + function
        + "(args[1], args[2], **json.loads(args[3]))"
    )
    _run(
        [str(python), "-c", script, str(source), str(target), json.dumps(kwargs)],
        env=_benchmark_python_env(),
    )


def _probe_soffice(path: Path | None, expected_version: str | None) -> dict[str, Any]:
    if path is None or not expected_version:
        raise BenchmarkError(
            "office profiles require --soffice and --soffice-version with the exact `soffice --version` output"
        )
    executable = path.expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BenchmarkError(f"LibreOffice executable is unavailable: {executable}")
    actual = _run([str(executable), "--version"]).strip()
    if actual != expected_version.strip():
        raise BenchmarkError(
            f"LibreOffice version mismatch: expected {expected_version!r}, got {actual!r}"
        )
    return {"path": str(executable), "version": actual}


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _prepared_path(root: Path, profile: str) -> Path:
    return root / f"{profile}.prepared.json"


def _case_manifest_digests(root: Path, profile: str) -> dict[str, str]:
    return {
        benchmark: hashlib.sha256((root / f"{profile}-{benchmark}.jsonl").read_bytes()).hexdigest()
        for benchmark in ("ocb", "officebench", "presentbench")
    }


def _load_prepared(root: Path, profile: str) -> dict[str, Any]:
    path = _prepared_path(root, profile)
    if not path.is_file():
        raise BenchmarkError(f"profile is not prepared: run `nanobot benchmark prepare --profile {profile}`")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload.pop("fingerprint", None)
    actual = _fingerprint(payload)
    payload["fingerprint"] = expected
    if expected != actual:
        raise BenchmarkError(f"prepared profile fingerprint mismatch: {path}")
    manifest_root = Path(payload["case_manifest_root"])
    expected_manifests = payload.get("case_manifest_sha256")
    if not isinstance(expected_manifests, dict):
        raise BenchmarkError(f"prepared profile lacks case manifest digests: re-run prepare for {profile}")
    try:
        actual_manifests = _case_manifest_digests(manifest_root, profile)
    except FileNotFoundError as exc:
        raise BenchmarkError(f"prepared case manifest is unavailable: {exc.filename}") from exc
    if expected_manifests != actual_manifests:
        raise BenchmarkError(f"prepared case manifests changed: re-run prepare for {profile}")
    return payload


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise BenchmarkError(f"missing materialized case manifest: {path}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _missing_ocb_references(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({
        Path(path).name
        for row in rows
        for path, digest in zip(
            row["input"].get("reference_paths", []),
            row["input"].get("reference_sha256", []),
            strict=True,
        )
        if not digest
    })


def _download_ocb_references(
    python: Path,
    source_root: Path,
    data_root: Path,
    rows: list[dict[str, Any]],
) -> None:
    missing = _missing_ocb_references(rows)
    if not missing:
        return
    _run([
        str(python),
        str(source_root / "download_and_convert_files.py"),
        "--manifest",
        str(data_root / "data" / "ocb_source_urls.parquet"),
        "--output-dir",
        str(data_root / "reference_files"),
        "--filename",
        ",".join(missing),
        "--delay",
        "0",
    ])


def _item_field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _case_id(item: Any) -> str:
    metadata = _item_field(item, "metadata", {}) or {}
    if isinstance(metadata, dict) and metadata.get("case_id") is not None:
        return str(metadata["case_id"])
    raw_input = _item_field(item, "input", {})
    if isinstance(raw_input, dict) and raw_input.get("case_id") is not None:
        return str(raw_input["case_id"])
    raise BenchmarkError("Langfuse Dataset item is missing metadata.case_id")


def _case_manifest_map(
    prepared: dict[str, Any],
    profile: str,
    *,
    presentbench_sample: int,
) -> dict[str, list[dict[str, Any]]]:
    case_root = Path(prepared.get("case_manifest_root") or "")
    items = {
        benchmark: _read_rows(case_root / f"{profile}-{benchmark}.jsonl")
        for benchmark in ("ocb", "officebench", "presentbench")
    }
    if profile == "office-smoke":
        expected = {name: len(cases) for name, cases in _manifest()["smoke_cases"].items()}
        for benchmark, rows in items.items():
            if len(rows) != expected[benchmark]:
                raise BenchmarkError(
                    f"{benchmark} smoke manifest has {len(rows)} items; expected {expected[benchmark]}"
                )
    elif profile == "office-release":
        expected = {"ocb": 1018, "officebench": 93}
        for benchmark, count in expected.items():
            if len(items[benchmark]) != count:
                raise BenchmarkError(
                    f"{benchmark} release manifest has {len(items[benchmark])} items; expected {count}"
                )
        if len(items["presentbench"]) < presentbench_sample:
            raise BenchmarkError(
                f"PresentBench manifest has {len(items['presentbench'])} items; need {presentbench_sample}"
            )
        items["presentbench"] = items["presentbench"][:presentbench_sample]
    return items


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise BenchmarkError(f"benchmark fixture directory is unavailable: {source}")
    shutil.copytree(source, target)


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("._")
    return normalized or "case"


def _stage_case_workspace(
    *,
    benchmark: str,
    source: dict[str, Any],
    run_root: Path,
    skill: str,
) -> Path:
    case_id = str(source["metadata"]["case_id"])
    workspace = run_root / benchmark / skill / _safe_component(case_id)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    if benchmark == "officebench":
        config_path = Path(source["input"]["source_config"]).resolve()
        task_root = config_path.parents[1]
        task_id, subtask = case_id.split("/", 1)
        mirror_task = workspace / "tasks" / task_id
        _copy_tree(task_root / "testbed", mirror_task / "outputs" / subtask / "mybot" / "testbed")
        reference = task_root / "reference"
        if reference.is_dir():
            _copy_tree(reference, mirror_task / "reference")
        return mirror_task / "outputs" / subtask / "mybot" / "testbed"
    workspace.mkdir(parents=True, exist_ok=True)
    paths = source["input"].get("reference_paths") or source["input"].get("material_paths") or []
    destination = workspace / ("reference_files" if benchmark == "ocb" else "materials")
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise BenchmarkError(f"benchmark material is unavailable: {path}")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination / path.name)
    return workspace


def _cloud_smoke(runtime: LangfuseRuntime) -> dict[str, str]:
    """Prove write, flush, API readback and an actionable Japan Cloud deep link."""
    trace_id: str | None = None
    with runtime.observation(
        name="mybot.benchmark.cloud_smoke",
        as_type="span",
        input={"probe": "metadata-only"},
        metadata={"mybot.smoke": True},
    ) as observation:
        if observation.observation is None:
            raise BenchmarkError("Langfuse observation could not be started")
        trace_id = runtime.client.get_current_trace_id()
    if not trace_id:
        raise BenchmarkError("Langfuse observation did not produce a trace id")
    runtime.flush(strict=True)
    last_error: Exception | None = None
    for attempt in range(_CLOUD_READBACK_ATTEMPTS):
        try:
            runtime.client.api.trace.get(trace_id)
            project_id = runtime.client._get_project_id()
            if not project_id:
                raise BenchmarkError("Langfuse project id could not be resolved")
            deep_link = (
                f"{runtime.base_url}/project/{project_id}/traces/{trace_id}"
            )
            return {"trace_id": trace_id, "deep_link": deep_link}
        except Exception as exc:
            last_error = exc
            if attempt + 1 < _CLOUD_READBACK_ATTEMPTS:
                time.sleep(_CLOUD_READBACK_INTERVAL_SEC)
    raise BenchmarkError(f"Langfuse trace readback failed after flush: {last_error}")


def _ensure_dataset(client: Any, name: str, metadata: dict[str, Any]) -> Any:
    try:
        return client.get_dataset(name)
    except Exception:
        try:
            return client.create_dataset(
                name=name,
                description=f"Immutable Mybot public benchmark adapter: {name}",
                metadata=metadata,
            )
        except Exception:
            return client.get_dataset(name)


def _dataset_row_payload(
    row: dict[str, Any],
    *,
    allow_licensed_content: bool,
) -> tuple[dict[str, Any], Any]:
    raw_input = dict(row["input"])
    raw_expected = row.get("expected_output")
    prompt = str(raw_input.pop("prompt", ""))
    raw_input.pop("reference_paths", None)
    raw_input.pop("material_paths", None)
    raw_input.pop("source_config", None)
    raw_input["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if allow_licensed_content:
        raw_input["prompt"] = prompt
        return raw_input, raw_expected
    return raw_input, {
        "expected_output_sha256": hashlib.sha256(
            json.dumps(raw_expected, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "content_withheld": True,
    }


def _upload_rows_to_dataset(
    runtime: LangfuseRuntime,
    *,
    name: str,
    rows: list[dict[str, Any]],
    metadata: dict[str, Any],
    allow_licensed_content: bool,
) -> str:
    dataset = _ensure_dataset(runtime.client, name, metadata)
    existing = {str(item.id) for item in getattr(dataset, "items", [])}
    for row in rows:
        case_id = str(row["metadata"]["case_id"])
        item_id = hashlib.sha256(f"{name}:{case_id}".encode()).hexdigest()[:32]
        if item_id in existing:
            continue
        raw_input, raw_expected = _dataset_row_payload(
            row,
            allow_licensed_content=allow_licensed_content,
        )
        runtime.client.create_dataset_item(
            dataset_name=name,
            id=item_id,
            input=raw_input,
            expected_output=raw_expected,
            metadata={
                **row["metadata"],
                "profile": metadata["profile"],
                "content_uploaded": str(allow_licensed_content).lower(),
            },
        )
    return name


def _upload_prepared_datasets(
    runtime: LangfuseRuntime,
    prepared: dict[str, Any],
    profile: str,
    case_manifest_root: Path,
    *,
    allow_licensed_content: bool,
) -> dict[str, str]:
    """Create immutable Dataset items; Langfuse remains the Dataset truth source."""
    manifest = _manifest()
    dataset_names: dict[str, str] = {}
    for benchmark in ("ocb", "officebench", "presentbench"):
        revision = prepared["repositories"][benchmark]["revision"]
        visibility = "licensed" if allow_licensed_content else "redacted"
        name = f"mybot-{benchmark}-{revision[:12]}-{profile}-{visibility}-v1"
        metadata = {
            "schema_version": "1",
            "profile": profile,
            "benchmark": benchmark,
            "code_revision": revision,
            "dataset_revision": prepared["repositories"][benchmark].get("dataset_revision", "code-pinned"),
            "evaluation_source": (
                "officebench_official" if benchmark == "officebench" else "langfuse_terra"
            ),
            "agent_model": manifest["models"]["agent"],
            "judge_model": manifest["models"]["judge"],
            "license_reviewed": str(allow_licensed_content).lower(),
            "prepared_fingerprint": prepared["asset_fingerprint"],
        }
        manifest_path = case_manifest_root / f"{profile}-{benchmark}.jsonl"
        rows = _read_rows(manifest_path)
        _upload_rows_to_dataset(
            runtime,
            name=name,
            rows=rows,
            metadata=metadata,
            allow_licensed_content=allow_licensed_content,
        )
        dataset_names[benchmark] = name
    return dataset_names


def _langfuse_runtime(*, require_capture_content: bool = False) -> LangfuseRuntime:
    config = resolve_config_env_vars(load_config()).observability.langfuse
    if not config.enabled:
        raise BenchmarkError("office benchmark requires observability.langfuse.enabled=true")
    if require_capture_content and not config.capture_content:
        raise BenchmarkError(
            "Dataset preparation needs observability.langfuse.captureContent=true after license/data review"
        )
    try:
        runtime = LangfuseRuntime(config)
        if not runtime.client.auth_check():
            runtime.shutdown()
            raise BenchmarkError("Langfuse Japan Cloud authentication failed")
        return runtime
    except BenchmarkError:
        raise
    except Exception as exc:
        raise BenchmarkError(f"Langfuse Japan Cloud preflight failed: {exc}") from exc


def _git_metadata() -> dict[str, str]:
    sha = _run(["git", "rev-parse", "HEAD"], cwd=_ROOT)
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=_ROOT))
    return {"git_sha": sha, "git_dirty": str(dirty).lower()}


@benchmark_app.command("prepare")
def prepare(
    profile: str = typer.Option(..., "--profile"),
    cache_dir: Path | None = typer.Option(None, "--cache-dir"),
    soffice: Path | None = typer.Option(None, "--soffice"),
    soffice_version: str | None = typer.Option(None, "--soffice-version"),
    install: bool = typer.Option(True, "--install/--no-install", help="Install the pinned benchmark venv"),
    allow_licensed_content: bool = typer.Option(
        False,
        "--allow-licensed-content",
        help="Upload benchmark prompts/rubrics after operator license review",
    ),
) -> None:
    """Validate pinned assets/licenses and prepare the external benchmark environment."""
    try:
        _validate_profile(profile)
        if profile == "ci":
            console.print("[green]CI profile is ready: offline deterministic tests need no assets.[/green]")
            return
        root = _cache_root(cache_dir)
        root.mkdir(parents=True, exist_ok=True)
        manifest = _manifest()
        sources = {
            name: str(_clone_at_revision(name, spec, root))
            for name, spec in manifest["repositories"].items()
        }
        python = _ensure_benchmark_venv(root)
        if install:
            _run([str(python), "-m", "pip", "install", "-r", str(root / "constraints.txt")])
        libreoffice = _probe_soffice(soffice, soffice_version)
        dataset_sources: dict[str, str] = {}
        ocb_spec = manifest["repositories"]["ocb"]
        present_spec = manifest["repositories"]["presentbench"]
        ocb_data = _download_hf_snapshot(
            python,
            ocb_spec,
            root / "datasets" / "ocb",
            [
                "data/ocb_qna_data.parquet",
                "data/ocb_source_urls.parquet",
                "README.md",
                "NOTICES.md",
            ],
        )
        present_data = _download_hf_snapshot(
            python,
            present_spec,
            root / "datasets" / "presentbench",
            [
                "README.md",
                *[f"{case}/**" for case in manifest["smoke_cases"]["presentbench"]],
            ],
        )
        if profile == "office-release":
            present_data = _download_hf_snapshot(python, present_spec, root / "datasets" / "presentbench", ["**"])
        dataset_sources.update({"ocb": str(ocb_data), "presentbench": str(present_data)})
        case_manifest_root = root / "cases"
        ocb_case_ids = (
            [int(case_id) for case_id in manifest["smoke_cases"]["ocb"]]
            if profile == "office-smoke"
            else None
        )
        _materialize_manifest(
            python,
            "ocb",
            ocb_data,
            case_manifest_root / f"{profile}-ocb.jsonl",
            case_ids=ocb_case_ids,
        )
        ocb_rows = _read_rows(case_manifest_root / f"{profile}-ocb.jsonl")
        if allow_licensed_content:
            _download_hf_snapshot(
                python,
                ocb_spec,
                ocb_data,
                [
                    f"reference_files/{Path(path).name}"
                    for row in ocb_rows
                    for path in row["input"].get("reference_paths", [])
                ],
            )
            _download_ocb_references(
                python,
                Path(sources["ocb"]),
                ocb_data,
                ocb_rows,
            )
            _materialize_manifest(
                python,
                "ocb",
                ocb_data,
                case_manifest_root / f"{profile}-ocb.jsonl",
                case_ids=ocb_case_ids,
            )
            missing_ocb = _missing_ocb_references(
                _read_rows(case_manifest_root / f"{profile}-ocb.jsonl")
            )
            if missing_ocb:
                raise BenchmarkError(
                    "OCB reference assets remain unavailable after the pinned official downloader: "
                    + ", ".join(missing_ocb)
                    + ". Configure the upstream conversion credentials or place the official converted "
                    "files in the external OCB reference_files cache, then rerun prepare."
                )
        _materialize_manifest(
            python,
            "officebench",
            Path(sources["officebench"]),
            case_manifest_root / f"{profile}-officebench.jsonl",
            cases=(
                manifest["smoke_cases"]["officebench"]
                if profile == "office-smoke"
                else None
            ),
        )
        _materialize_manifest(
            python,
            "presentbench",
            present_data,
            case_manifest_root / f"{profile}-presentbench.jsonl",
            cases=(
                manifest["smoke_cases"]["presentbench"]
                if profile == "office-smoke"
                else None
            ),
        )
        prepared = {
            "schema_version": 2,
            "profile": profile,
            "repositories": manifest["repositories"],
            "sources": sources,
            "data_sources": dataset_sources,
            "case_manifest_root": str(case_manifest_root),
            "datasets": {},
            "licensed_content_uploaded": allow_licensed_content,
            "benchmark_python": str(python),
            "constraints_sha256": hashlib.sha256(_CONSTRAINTS_PATH.read_bytes()).hexdigest(),
            "libreoffice": libreoffice,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        prepared["case_manifest_sha256"] = _case_manifest_digests(case_manifest_root, profile)
        prepared["asset_fingerprint"] = _fingerprint(prepared)
        runtime = _langfuse_runtime()
        try:
            prepared["datasets"] = _upload_prepared_datasets(
                runtime,
                prepared,
                profile,
                case_manifest_root,
                allow_licensed_content=allow_licensed_content,
            )
            prepared["cloud_smoke"] = _cloud_smoke(runtime)
        finally:
            runtime.shutdown()
        prepared["fingerprint"] = _fingerprint(prepared)
        path = _prepared_path(root, profile)
        path.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Prepared {profile}[/green]  {path}")
        console.print("[yellow]Raw files stay in external cache; Scores, Runs and Annotation Queue stay in Langfuse.[/yellow]")
    except BenchmarkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def estimate_payload(profile: str, presentbench_sample: int, manifest: dict[str, Any]) -> dict[str, Any]:
    if profile == "office-smoke":
        counts = {name: len(items) for name, items in manifest["smoke_cases"].items()}
    else:
        counts = {"ocb": 1018, "officebench": 93, "presentbench": presentbench_sample}
    runs = sum(counts.values()) * len(manifest["skills"])
    token_estimate = manifest["estimate_tokens_per_case"]
    judged = (counts["ocb"] + counts["presentbench"]) * len(manifest["skills"])
    estimated_tokens = {
        "agent_input": runs * token_estimate["agent_input"],
        "agent_output": runs * token_estimate["agent_output"],
        "judge_input": judged * token_estimate["judge_input"],
        "judge_output": judged * token_estimate["judge_output"],
    }
    estimated_tokens["total"] = sum(estimated_tokens.values())
    return {
        "profile": profile,
        "case_counts": counts,
        "skill_runs": runs,
        "judge_runs": judged,
        "estimated_tokens": estimated_tokens,
    }


def _validate_presentbench_sample(profile: str, sample: int) -> None:
    if profile == "office-release" and sample not in _PRESENTBENCH_SAMPLES:
        raise BenchmarkError(
            "office-release PresentBench sample must be one of 60 (25%), 119 (50%), or 238 (full)"
        )


@benchmark_app.command("estimate")
def estimate(
    profile: str = typer.Option(..., "--profile"),
    model_preset: str = typer.Option("gpt-5-6-luna", "--model-preset"),
    presentbench_sample: int = typer.Option(238, "--presentbench-sample", min=1, max=238),
) -> None:
    """Print a pre-run token estimate without calling any model."""
    try:
        _validate_profile(profile)
        if profile == "ci":
            console.print(json.dumps({"profile": "ci", "estimated_tokens": {"total": 0}}, indent=2))
            return
        _validate_presentbench_sample(profile, presentbench_sample)
        manifest = _manifest()
        if model_preset != manifest["models"]["agent"]:
            raise BenchmarkError(f"Office comparison is fixed to {manifest['models']['agent']}")
        payload = estimate_payload(profile, presentbench_sample, manifest)
        console.print_json(data=payload)
    except BenchmarkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _load_case_items(
    prepared: dict[str, Any],
    profile: str,
    *,
    presentbench_sample: int,
) -> dict[str, list[dict[str, Any]]]:
    """Load already-prepared case manifests without downloading during run."""
    return _case_manifest_map(
        prepared,
        profile,
        presentbench_sample=presentbench_sample,
    )


def _validate_case_assets(items: dict[str, list[dict[str, Any]]]) -> None:
    missing = sorted({
        Path(path).name
        for rows in items.values()
        for row in rows
        for field in ("reference_paths", "material_paths")
        for path in row["input"].get(field, [])
        if not Path(path).is_file()
    })
    if missing:
        raise BenchmarkError(
            "prepared benchmark assets are unavailable: "
            + ", ".join(missing)
            + "; re-run prepare before any model call"
        )


async def _run_agent_item(
    *,
    source: dict[str, Any],
    benchmark: str,
    skill: str,
    model_preset: str,
    run_root: Path,
) -> dict[str, Any]:
    from nanobot.nanobot import Nanobot

    prompt = str(source["input"]["prompt"])
    case_id = str(source["metadata"]["case_id"])
    workspace = _stage_case_workspace(
        benchmark=benchmark,
        source=source,
        run_root=run_root,
        skill=skill,
    )
    async with Nanobot.from_config(workspace=workspace) as bot:
        bot._loop.set_model_preset(model_preset, publish_update=False)
        result = await bot.run(
            prompt,
            session_key=f"benchmark:{skill}:{case_id}",
            metadata={
                "selected_skills": [skill],
                "benchmark_model_preset": model_preset,
                "benchmark": benchmark,
                "benchmark_case_id": case_id,
            },
        )
    workspace_token = uuid.uuid4().hex
    with _WORKSPACES_LOCK:
        _WORKSPACES[workspace_token] = workspace
    return {
        "content": result.content,
        "tools_used": result.tools_used,
        "case_id": case_id,
        "skill": skill,
        "workspace_token": workspace_token,
    }


def _presence_evaluator(*, output: Any, **kwargs: Any):
    from langfuse.experiment import Evaluation

    present = bool(
        isinstance(output, dict)
        and output.get("workspace_token")
        and output.get("case_id")
    )
    return Evaluation(name="output_present", value=present, data_type="BOOLEAN")


def _officebench_evaluator(
    *,
    output: Any,
    benchmark_python: Path,
    source_root: Path,
):
    from langfuse.experiment import Evaluation

    token = output.get("workspace_token") if isinstance(output, dict) else None
    case_id = output.get("case_id") if isinstance(output, dict) else None
    with _WORKSPACES_LOCK:
        workspace = _WORKSPACES.get(str(token)) if token else None
    if workspace is None or not case_id:
        return [
            Evaluation(
                name="official_score",
                value=0.0,
                comment="official evaluator did not receive a staged workspace",
                data_type="NUMERIC",
            ),
            Evaluation(
                name="official_evaluator_ok",
                value=False,
                comment="missing workspace token",
                data_type="BOOLEAN",
            ),
        ]
    script = """
import json
import sys
from nanobot.benchmark_adapters import evaluate_officebench

try:
    passed = evaluate_officebench(sys.argv[1], sys.argv[2], sys.argv[3])
    print("__MYBOT_RESULT__" + json.dumps({"passed": bool(passed), "ok": True}))
except Exception as exc:
    print("__MYBOT_RESULT__" + json.dumps({"passed": False, "ok": False, "error": str(exc)}))
"""
    try:
        raw = _run(
            [str(benchmark_python), "-c", script, str(source_root), str(case_id), str(workspace)],
            env=_benchmark_python_env(),
        )
        marker = next(
            line[len("__MYBOT_RESULT__") :]
            for line in raw.splitlines()
            if line.startswith("__MYBOT_RESULT__")
        )
        result = json.loads(marker)
    except Exception as exc:
        result = {"passed": False, "ok": False, "error": str(exc)}
    return [
        Evaluation(
            name="official_score",
            value=1.0 if result.get("passed") else 0.0,
            comment=("passed" if result.get("passed") else "failed"),
            data_type="NUMERIC",
        ),
        Evaluation(
            name="official_evaluator_ok",
            value=bool(result.get("ok")),
            comment=str(result.get("error") or "official evaluator completed")[:500],
            data_type="BOOLEAN",
        ),
    ]


def _get_score_config(client: Any, name: str) -> Any:
    response = client.api.score_configs.get(page=1, limit=100)
    for config in response.data:
        if config.name == name and not config.is_archived:
            return config
    from langfuse.api.commons.types.score_config_data_type import ScoreConfigDataType

    return client.api.score_configs.create(
        name=name,
        data_type=ScoreConfigDataType.NUMERIC,
        min_value=0,
        max_value=1,
        description="Mybot benchmark human audit score; 0-1 after reviewer inspection",
    )


def _get_annotation_queue(client: Any, name: str) -> Any:
    response = client.api.annotation_queues.list_queues(page=1, limit=100)
    for queue in response.data:
        if queue.name == name:
            return queue
    score_config = _get_score_config(client, "mybot-human-review")
    return client.api.annotation_queues.create_queue(
        name=name,
        score_config_ids=[score_config.id],
        description="Mybot benchmark audit queue; reviewer completes it in Langfuse Japan Cloud",
    )


def _enqueue_review_items(
    runtime: LangfuseRuntime,
    *,
    result: Any,
    profile: str,
    benchmark: str,
    skill: str,
) -> tuple[str, int]:
    from langfuse.api.annotation_queues.types.annotation_queue_object_type import (
        AnnotationQueueObjectType,
    )

    queue = _get_annotation_queue(
        runtime.client,
        f"mybot-{profile}-{benchmark}-{skill}-review",
    )
    trace_ids = [item.trace_id for item in result.item_results if item.trace_id]
    if profile == "office-smoke":
        selected = trace_ids
    else:
        selected = sorted(
            trace_ids,
            key=lambda trace_id: hashlib.sha256(str(trace_id).encode()).hexdigest(),
        )[: max(1, math.ceil(len(trace_ids) * 0.05))]
    existing = {
        item.object_id
        for item in runtime.client.api.annotation_queues.list_queue_items(
            queue.id,
            page=1,
            limit=100,
        ).data
    }
    added = 0
    for trace_id in selected:
        if trace_id in existing:
            continue
        runtime.client.api.annotation_queues.create_queue_item(
            queue.id,
            object_id=trace_id,
            object_type=AnnotationQueueObjectType.TRACE,
        )
        added += 1
    return queue.id, added


@benchmark_app.command("run")
def run(
    profile: str = typer.Option(..., "--profile"),
    model_preset: str = typer.Option("gpt-5-6-luna", "--model-preset"),
    cache_dir: Path | None = typer.Option(None, "--cache-dir"),
    presentbench_sample: int = typer.Option(238, "--presentbench-sample", min=1, max=238),
    benchmarks: list[str] | None = typer.Option(None, "--benchmark"),
    skills: list[str] | None = typer.Option(None, "--skill"),
    parent_run_id: str | None = typer.Option(None, "--parent-run-id"),
) -> None:
    """Run offline CI gates or thinly invoke Langfuse Experiment Runner."""
    try:
        _validate_profile(profile)
        if profile == "ci":
            command = [
                sys.executable,
                "-m",
                "pytest",
                "tests/runtime/test_replay_trace_eval.py",
                "tests/runtime/test_langfuse_observability.py",
                "tests/skills/test_office_python.py",
                "tests/skills/test_officecli_runtime.py",
                "tests/cli/test_benchmark_contract.py",
                "-q",
            ]
            console.print(_run(command, cwd=_ROOT))
            return
        manifest = _manifest()
        _validate_presentbench_sample(profile, presentbench_sample)
        selected_benchmarks = _select_values(benchmarks, _BENCHMARKS, "benchmark")
        selected_skills = _select_values(skills, _SKILLS, "Skill")
        if parent_run_id and (len(selected_benchmarks) != 1 or len(selected_skills) != 1):
            raise BenchmarkError(
                "--parent-run-id requires exactly one --benchmark and one --skill"
            )
        if model_preset != manifest["models"]["agent"]:
            raise BenchmarkError(f"Office comparison is fixed to {manifest['models']['agent']}")
        root = _cache_root(cache_dir)
        prepared = _load_prepared(root, profile)
        if not prepared.get("licensed_content_uploaded"):
            raise BenchmarkError(
                "prepared Dataset items with withheld content cannot run an Agent; re-run prepare "
                "with --allow-licensed-content after license review"
            )
        items = _load_case_items(
            prepared,
            profile,
            presentbench_sample=presentbench_sample,
        )
        selected_items = {
            name: values
            for name, values in items.items()
            if name in selected_benchmarks
        }
        _validate_case_assets(selected_items)
        missing = [name for name, values in selected_items.items() if not values]
        if missing:
            raise BenchmarkError(
                "prepared case manifests are incomplete for " + ", ".join(missing)
                + "; complete licensed asset preparation before model calls"
            )
        runtime = _langfuse_runtime(require_capture_content=True)
        metadata = {
            **_git_metadata(),
            "profile": profile,
            "model_preset": model_preset,
            "prepared_fingerprint": prepared["fingerprint"],
            "presentbench_sample": str(presentbench_sample),
            "annotation_review_policy": "smoke=100%; release=stable 5% sample",
            "benchmark_filter": list(selected_benchmarks),
            "skill_filter": list(selected_skills),
        }
        if parent_run_id:
            metadata["parent_run_id"] = parent_run_id
        run_root = root / "runs" / profile / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        try:
            for benchmark, benchmark_items in items.items():
                if benchmark not in selected_benchmarks:
                    continue
                dataset_name = prepared["datasets"][benchmark]
                if benchmark == "presentbench" and profile == "office-release" and presentbench_sample != 238:
                    dataset_name = f"{dataset_name}-n{presentbench_sample}"
                    _upload_rows_to_dataset(
                        runtime,
                        name=dataset_name,
                        rows=benchmark_items,
                        metadata={**metadata, "profile": profile, "benchmark": benchmark},
                        allow_licensed_content=True,
                    )
                dataset = runtime.client.get_dataset(dataset_name)
                source_by_case = {
                    str(source["metadata"]["case_id"]): source
                    for source in benchmark_items
                }
                for skill in selected_skills:
                    def task(*, item, _skill=skill, _benchmark=benchmark):
                        case_id = _case_id(item)
                        try:
                            source = source_by_case[case_id]
                        except KeyError as exc:
                            raise BenchmarkError(
                                f"Dataset {dataset_name} item {case_id} is not in the prepared manifest"
                            ) from exc
                        return _run_agent_item(
                            source=source,
                            benchmark=_benchmark,
                            skill=_skill,
                            model_preset=model_preset,
                            run_root=run_root,
                        )

                    evaluators = [_presence_evaluator]
                    if benchmark == "officebench":
                        evaluators.append(
                            lambda **kwargs: _officebench_evaluator(
                                **kwargs,
                                benchmark_python=Path(prepared["benchmark_python"]),
                                source_root=Path(prepared["sources"]["officebench"]),
                            )
                        )
                    result = dataset.run_experiment(
                        name=f"mybot-{profile}-{benchmark}-{skill}",
                        description=(
                            "Mybot public Office comparison; OfficeBench official evaluator and "
                            "Langfuse Terra Judge scores are the evaluation source"
                        ),
                        task=task,
                        evaluators=evaluators,
                        max_concurrency=2,
                        metadata={
                            **metadata,
                            "benchmark": benchmark,
                            "skill": skill,
                            "dataset_name": dataset_name,
                            "evaluation_source": (
                                "officebench_official"
                                if benchmark == "officebench"
                                else "langfuse_terra"
                            ),
                            "required_remote_score": (
                                "official_score"
                                if benchmark == "officebench"
                                else "mybot_score"
                            ),
                            "annotation_queue_name": f"mybot-{profile}-{benchmark}-{skill}-review",
                        },
                    )
                    queue_id, added = _enqueue_review_items(
                        runtime,
                        result=result,
                        profile=profile,
                        benchmark=benchmark,
                        skill=skill,
                    )
                    console.print(
                        f"[green]{result.run_name}[/green] "
                        f"dataset_run={result.dataset_run_id} "
                        f"review_queue={queue_id} (+{added}) "
                        f"{result.dataset_run_url or ''}"
                    )
            runtime.flush(strict=True)
        finally:
            runtime.shutdown()
    except BenchmarkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    return value


def _experiment_items(client: Any, experiment: Any) -> list[Any]:
    items: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        response = client.api.experiments.list_items(
            from_start_time=experiment.start_time,
            experiment_id=experiment.id,
            limit=100,
            score_limit=100,
            cursor=cursor,
        )
        items.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return items
        if cursor in seen:
            raise BenchmarkError("Langfuse experiment pagination returned a repeated cursor")
        seen.add(cursor)


def _annotation_queues(client: Any) -> list[Any]:
    queues: list[Any] = []
    page = 1
    while True:
        response = client.api.annotation_queues.list_queues(page=page, limit=100)
        queues.extend(response.data)
        if page >= response.meta.total_pages:
            return queues
        page += 1


def _annotation_queue_items(client: Any, queue_id: str) -> list[Any]:
    items: list[Any] = []
    page = 1
    while True:
        response = client.api.annotation_queues.list_queue_items(
            queue_id,
            page=page,
            limit=100,
        )
        items.extend(response.data)
        if page >= response.meta.total_pages:
            return items
        page += 1


def _score_map(item: Any) -> dict[str, Any]:
    return {str(score.name): score for score in (item.scores or [])}


def export_run(client: Any, dataset_run: str) -> dict[str, Any]:
    """Fetch one completed experiment and enforce score/review completeness."""
    response = client.api.experiments.list(
        from_start_time=datetime(1970, 1, 1, tzinfo=timezone.utc),
        id=dataset_run,
        limit=2,
    )
    experiments = list(response.data)
    if len(experiments) != 1:
        raise BenchmarkError(f"Langfuse Dataset Run not found or ambiguous: {dataset_run}")
    experiment = experiments[0]
    items = _experiment_items(client, experiment)
    if len(items) != experiment.item_count:
        raise BenchmarkError(f"run incomplete: expected {experiment.item_count} items, got {len(items)}")
    if any(item.end_time is None or str(item.level).upper().endswith("ERROR") for item in items):
        raise BenchmarkError("run contains incomplete/error items")
    metadata = experiment.metadata or {}
    profile = str(metadata.get("profile") or "")
    benchmark = str(metadata.get("benchmark") or "")
    required_score = str(
        metadata.get("required_remote_score")
        or ("official_score" if benchmark == "officebench" else "mybot_score")
    )
    for item in items:
        scores = _score_map(item)
        if "output_present" not in scores or not bool(scores["output_present"].value):
            raise BenchmarkError("every run item must pass output_present before export")
        if required_score not in scores:
            raise BenchmarkError(
                f"every {benchmark} item needs {required_score}; Langfuse evaluator may still be pending"
            )
        if benchmark == "officebench" and (
            "official_evaluator_ok" not in scores
            or not bool(scores["official_evaluator_ok"].value)
        ):
            raise BenchmarkError("OfficeBench official evaluator had an infrastructure error")
    queue_name = str(metadata.get("annotation_queue_name") or "")
    queue = next((item for item in _annotation_queues(client) if item.name == queue_name), None)
    if queue is None:
        raise BenchmarkError(f"Annotation Queue not found: {queue_name or '(missing metadata)'}")
    reviewed_object_ids = {
        str(queue_item.object_id)
        for queue_item in _annotation_queue_items(client, queue.id)
        if str(queue_item.status).upper().endswith("COMPLETED")
    }
    required = len(items) if profile == "office-smoke" else max(1, math.ceil(len(items) * 0.05))
    reviewed_items = [item for item in items if item.trace_id in reviewed_object_ids]
    reviewed = len(reviewed_items)
    if reviewed < required:
        raise BenchmarkError(f"Annotation Queue review incomplete: {reviewed}/{required}")
    if any("mybot-human-review" not in _score_map(item) for item in reviewed_items):
        raise BenchmarkError("completed Annotation Queue items must include mybot-human-review scores")
    project_id = client._get_project_id()
    base_url = str(getattr(client, "_base_url", "")).rstrip("/")
    if not project_id or not base_url or not experiment.dataset_id:
        raise BenchmarkError("cannot construct a verified Langfuse Dataset Run deep link")
    deep_link = (
        f"{base_url}/project/{project_id}/datasets/{experiment.dataset_id}/runs/{experiment.id}"
    )
    return {
        "schema_version": 1,
        "dataset_run_id": experiment.id,
        "name": experiment.name,
        "start_time": experiment.start_time.isoformat(),
        "end_time": experiment.end_time.isoformat(),
        "item_count": experiment.item_count,
        "benchmark": benchmark,
        "evaluation_source": metadata.get("evaluation_source"),
        "required_score": required_score,
        "metadata": metadata,
        "scores": _to_plain(experiment.scores or []),
        "score_names": sorted({name for item in items for name in _score_map(item)}),
        "reviewed_items": reviewed,
        "required_reviewed_items": required,
        "annotation_queue_id": queue.id,
        "annotation_queue_name": queue.name,
        "deep_link": deep_link,
    }


def _update_readme_benchmark_block(payload: dict[str, Any], readme_path: Path) -> None:
    begin = "<!-- benchmark-results:begin -->"
    end = "<!-- benchmark-results:end -->"
    content = readme_path.read_text(encoding="utf-8")
    start = content.find(begin)
    finish = content.find(end)
    if start < 0 or finish < 0 or finish <= start:
        raise BenchmarkError(f"README benchmark result markers are missing: {readme_path}")
    block = (
        f"{begin}\n"
        "### Benchmark 结果\n\n"
        f"- Benchmark: `{payload['benchmark']}`\n"
        f"- Evaluation: `{payload['evaluation_source']}` / `{payload['required_score']}`\n"
        f"- Dataset Run items: {payload['item_count']}\n"
        f"- Annotation Queue: {payload['reviewed_items']}/{payload['required_reviewed_items']} reviewed\n"
        f"- Langfuse: {payload['deep_link']}\n"
        "\n"
        "该区块只由 `nanobot benchmark export --dataset-run <id>` 更新。不同 benchmark 不合成总分。\n"
        f"{end}"
    )
    readme_path.write_text(
        content[:start] + block + content[finish + len(end) :],
        encoding="utf-8",
    )


@benchmark_app.command("export")
def export(
    dataset_run: str = typer.Option(..., "--dataset-run"),
    output_dir: Path = typer.Option(_ROOT / "benchmarks" / "exports", "--output-dir"),
    readme_path: Path = typer.Option(_ROOT / "README.md", "--readme"),
) -> None:
    """Export a de-identified snapshot only after run and annotation gates pass."""
    try:
        runtime = _langfuse_runtime()
        try:
            payload = export_run(runtime.client, dataset_run)
        finally:
            runtime.shutdown()
        target_dir = output_dir.expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        json_path = target_dir / f"{dataset_run}.json"
        md_path = target_dir / f"{dataset_run}.md"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(
            f"# Langfuse Dataset Run `{dataset_run}`\n\n"
            f"- Benchmark: {payload['benchmark']}\n"
            f"- Evaluation: {payload['evaluation_source']} / {payload['required_score']}\n"
            f"- Items: {payload['item_count']}\n"
            f"- Reviewed: {payload['reviewed_items']}/{payload['required_reviewed_items']}\n"
            f"- Started: {payload['start_time']}\n"
            f"- Ended: {payload['end_time']}\n"
            f"- Langfuse: {payload['deep_link']}\n",
            encoding="utf-8",
        )
        _update_readme_benchmark_block(payload, readme_path.expanduser().resolve())
        console.print(f"[green]Exported[/green] {json_path}")
    except BenchmarkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
