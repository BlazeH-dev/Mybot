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
import textwrap
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.evaluations.credentials import adobe_pdf_services_env
from nanobot.runtime.langfuse import LangfuseFlushTimeoutError, LangfuseRuntime

benchmark_app = typer.Typer(help="Prepare, estimate, run, and export Office benchmarks")
console = Console()
_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_PATH = _ROOT / "benchmarks" / "office" / "profiles.json"
_CONSTRAINTS_PATH = _ROOT / "benchmarks" / "office" / "constraints.txt"
_PROFILES = frozenset({"ci", "office-smoke", "office-release"})
_BENCHMARK_SAMPLES = {
    "ocb": (211,),
}
_DEFAULT_BENCHMARK_SAMPLES = {
    benchmark: samples[-1] for benchmark, samples in _BENCHMARK_SAMPLES.items()
}
_RELEASE_SOURCE_SAMPLE = 255
_RELEASE_EXCLUDED_OCB_CASE_IDS = frozenset({
    "0", "4", "7", "35", "37", "39", "40", "41", "54", "57", "75", "80", "100", "475",
    "476", "477", "481", "512", "515", "520", "521", "531", "586", "593",
    "597", "599", "600", "640", "643", "647", "820", "833", "834", "860",
    "862", "863", "868", "875", "876", "899", "901", "922", "936", "978",
})
_RELEASE_SAMPLE_SEED = "mybot-office-release-v1"
_RELEASE_SAMPLE_STRATEGY = "deterministic-stratified-v1"
_RELEASE_SAMPLE_DATASET_TAG = "strat-v1"
_CLOUD_READBACK_ATTEMPTS = 30
_CLOUD_READBACK_INTERVAL_SEC = 2
_SCORE_READBACK_ATTEMPTS = 30
_SCORE_READBACK_INTERVAL_SEC = 1
_HF_DOWNLOAD_ATTEMPTS = 3
_HF_DOWNLOAD_RETRY_INTERVAL_SEC = 2
_HF_CONNECT_TIMEOUT_SEC = 10
_HF_DOWNLOAD_TIMEOUT_SEC = 120
_HF_DOWNLOAD_PROCESS_TIMEOUT_SEC = 7_200
_HF_MIRROR_ENDPOINT = "https://hf-mirror.com"
_ADOBE_CONVERSION_ATTEMPTS = 3
_ADOBE_CONVERSION_RETRY_INTERVAL_SEC = 2
_ADOBE_CONNECT_TIMEOUT_MS = 30_000
_ADOBE_READ_TIMEOUT_MS = 120_000
_PROGRESS_LOCK = threading.Lock()


class BenchmarkError(RuntimeError):
    pass


def _emit_evaluation_progress(event: str, **fields: Any) -> None:
    """Append redacted execution metadata for the WebUI evaluation worker."""
    raw_path = os.environ.get("NANOBOT_EVALUATION_PROGRESS_LOG", "").strip()
    if not raw_path:
        return
    payload = {"event": event, "at": datetime.now(timezone.utc).isoformat(), **fields}
    path = Path(raw_path).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _PROGRESS_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        # Progress is best-effort and must never change benchmark results.
        return


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
    timeout_seconds: float | None = None,
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stderr or exc.stdout or ""
        detail = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
        suffix = f": {detail[-1200:]}" if detail.strip() else ""
        raise BenchmarkError(
            f"command timed out after {timeout_seconds:g}s ({' '.join(command)}){suffix}"
        ) from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise BenchmarkError(f"command failed ({' '.join(command)}): {detail[-1200:]}")
    return completed.stdout.strip()


def _clone_at_revision(name: str, spec: dict[str, Any], root: Path) -> Path:
    target = root / "sources" / name
    license_path = target / "LICENSE"
    if (target / ".git").is_dir() and license_path.is_file():
        actual = _run(["git", "rev-parse", "HEAD"], cwd=target)
        dirty = bool(_run(["git", "status", "--porcelain"], cwd=target))
        digest = hashlib.sha256(license_path.read_bytes()).hexdigest()
        if actual == spec["revision"] and not dirty and digest == spec["license_sha256"]:
            return target
    if not (target / ".git").is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", spec["url"], str(target)])
    _run(["git", "fetch", "--depth=1", "origin", spec["revision"]], cwd=target)
    _run(["git", "checkout", "--detach", "--force", spec["revision"]], cwd=target)
    actual = _run(["git", "rev-parse", "HEAD"], cwd=target)
    if actual != spec["revision"]:
        raise BenchmarkError(f"{name} revision mismatch: expected {spec['revision']}, got {actual}")
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
        "revision=sys.argv[2], local_dir=sys.argv[3], allow_patterns=sys.argv[4:], "
        "max_workers=1))"
    )
    target.mkdir(parents=True, exist_ok=True)
    dataset_id = spec.get("dataset_id")
    if not dataset_id:
        raise BenchmarkError("benchmark profile is missing a pinned HuggingFace dataset_id")
    env = _benchmark_python_env()
    # Xet responses are intermittently truncated on this machine; the regular
    # Hugging Face HTTP path is slower but resumable and content-equivalent.
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_HUB_ETAG_TIMEOUT"] = str(_HF_CONNECT_TIMEOUT_SEC)
    env["HF_HUB_DOWNLOAD_TIMEOUT"] = str(_HF_DOWNLOAD_TIMEOUT_SEC)
    patterns = list(dict.fromkeys(allow_patterns))
    command = [
        str(python),
        "-c",
        script,
        dataset_id,
        spec["dataset_revision"],
        str(target),
        *patterns,
    ]
    configured_endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    endpoints = [configured_endpoint] if configured_endpoint else [
        "https://huggingface.co",
        _HF_MIRROR_ENDPOINT,
    ]
    last_error: BenchmarkError | None = None
    total_attempts = len(endpoints) * _HF_DOWNLOAD_ATTEMPTS
    attempt_number = 0
    for endpoint in endpoints:
        endpoint_env = dict(env)
        endpoint_env["HF_ENDPOINT"] = endpoint
        for endpoint_attempt in range(1, _HF_DOWNLOAD_ATTEMPTS + 1):
            attempt_number += 1
            label = (
                f"Download {dataset_id} via {endpoint} "
                f"({attempt_number}/{total_attempts})"
            )
            _emit_evaluation_progress(
                "prepare_stage",
                stage="download_dataset",
                label=label,
                endpoint=endpoint,
                attempt=endpoint_attempt,
                total_attempts=_HF_DOWNLOAD_ATTEMPTS,
            )
            console.print(f"[cyan]{label}[/cyan]")
            try:
                output = _run(
                    command,
                    env=endpoint_env,
                    timeout_seconds=_HF_DOWNLOAD_PROCESS_TIMEOUT_SEC,
                )
                return Path(output.splitlines()[-1]).resolve()
            except BenchmarkError as exc:
                last_error = exc
                if endpoint_attempt < _HF_DOWNLOAD_ATTEMPTS:
                    time.sleep(_HF_DOWNLOAD_RETRY_INTERVAL_SEC)
    raise BenchmarkError(
        f"Hugging Face dataset download failed after {total_attempts} resumable attempts "
        f"across {', '.join(endpoints)}"
    ) from last_error


def _materialize_manifest(
    python: Path,
    adapter: str,
    source: Path,
    target: Path,
    *,
    case_ids: list[int] | None = None,
) -> None:
    function = {
        "ocb": "materialize_ocb",
    }[adapter]
    kwargs: dict[str, Any] = {}
    kwargs["case_ids"] = case_ids
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


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
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
        for benchmark in ("ocb",)
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
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


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
    wrapper = textwrap.dedent(f"""
        import runpy
        import socket
        import sys
        from io import BytesIO
        from urllib.parse import urlsplit

        import requests as standard_requests
        from adobe.pdfservices.operation.config.client_config import ClientConfig
        from adobe.pdfservices.operation.pdf_services import PDFServices
        from curl_cffi import requests as browser_requests
        from curl_cffi.const import CurlHttpVersion
        from pypdf import PdfReader, PdfWriter

        original_getaddrinfo = socket.getaddrinfo
        original_standard_get = standard_requests.get
        socket.getaddrinfo = lambda *args, **kwargs: sorted(
            original_getaddrinfo(*args, **kwargs),
            key=lambda item: item[0] != socket.AF_INET,
        )

        def compatible_get(*args, **kwargs):
            url = str(args[0] if args else kwargs.get("url", ""))
            expected_html = urlsplit(url).path.lower().endswith((".htm", ".html"))
            try:
                response = original_standard_get(*args, **kwargs)
            except standard_requests.exceptions.SSLError:
                response = None
            except standard_requests.exceptions.RequestException:
                raise
            if response is not None:
                content_type = response.headers.get("content-type", "").lower()
                blocked = response.status_code in {{
                    403, 408, 425, 429, 444, 500, 502, 503, 504,
                }}
                blocked = blocked or (
                    response.status_code == 200
                    and "text/html" in content_type
                    and not expected_html
                )
                if not blocked:
                    return response
                response.close()
            strategies = (
                ("chrome", None),
                ("safari", None),
                ("firefox", None),
                ("chrome", CurlHttpVersion.V1_1),
            )
            last_response = None
            last_error = None
            for index, (impersonate, http_version) in enumerate(strategies):
                options = dict(kwargs)
                options["impersonate"] = impersonate
                if http_version is not None:
                    options["http_version"] = http_version
                try:
                    response = browser_requests.get(*args, **options)
                except Exception as exc:
                    last_error = exc
                    continue
                last_response = response
                content_type = response.headers.get("content-type", "").lower()
                blocked = response.status_code in {{
                    403, 408, 425, 429, 444, 500, 502, 503, 504,
                }}
                blocked = blocked or (
                    response.status_code == 200
                    and "text/html" in content_type
                    and not expected_html
                )
                if not blocked:
                    return response
                if index < len(strategies) - 1:
                    response.close()
            if last_response is not None:
                return last_response
            if last_error is not None:
                raise last_error
            return standard_requests.sessions.Session().get(*args, **kwargs)

        standard_requests.get = compatible_get
        original_init = PDFServices.__init__
        original_upload = PDFServices.upload
        PDFServices.__init__ = lambda self, credentials, *, client_config=None: original_init(
            self,
            credentials,
            client_config=client_config or ClientConfig(
                connect_timeout={_ADOBE_CONNECT_TIMEOUT_MS},
                read_timeout={_ADOBE_READ_TIMEOUT_MS},
            ),
        )

        def compatible_upload(self, input_stream, mime_type):
            sanitized = input_stream
            try:
                reader = PdfReader(BytesIO(input_stream))
                if reader.is_encrypted and reader.decrypt(""):
                    writer = PdfWriter()
                    writer.clone_document_from_reader(reader)
                    output = BytesIO()
                    writer.write(output)
                    sanitized = output.getvalue()
            except Exception:
                pass
            return original_upload(self, sanitized, mime_type)

        PDFServices.upload = compatible_upload
        script_path = sys.argv[1]
        sys.argv = sys.argv[1:]
        runpy.run_path(script_path, run_name="__main__")
    """)
    output_root = data_root / "reference_files"
    for attempt in range(1, _ADOBE_CONVERSION_ATTEMPTS + 1):
        remaining = [
            name
            for name in missing
            if not (output_root / name).is_file() or (output_root / name).stat().st_size <= 1024
        ]
        if not remaining:
            return
        _emit_evaluation_progress(
            "prepare_stage",
            stage="references",
            label=(
                "Convert licensed Office references with Adobe "
                f"({attempt}/{_ADOBE_CONVERSION_ATTEMPTS})"
            ),
        )
        for filename in remaining:
            try:
                _run(
                    [
                        str(python),
                        "-c",
                        wrapper,
                        str(source_root / "download_and_convert_files.py"),
                        "--manifest",
                        str(data_root / "data" / "ocb_source_urls.parquet"),
                        "--output-dir",
                        str(output_root),
                        "--filename",
                        filename,
                        "--delay",
                        "0",
                    ],
                    env=adobe_pdf_services_env(),
                    timeout_seconds=(_ADOBE_READ_TIMEOUT_MS / 1000) * 2,
                )
            except BenchmarkError:
                # A native downloader failure must not prevent other cached assets from progressing.
                continue
        if attempt < _ADOBE_CONVERSION_ATTEMPTS:
            time.sleep(_ADOBE_CONVERSION_RETRY_INTERVAL_SEC)


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


def _sample_stratum(benchmark: str, row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    raw_input = row.get("input") if isinstance(row.get("input"), dict) else {}
    if benchmark == "ocb":
        return "|".join((
            str(raw_input.get("format") or "unknown"),
            str(metadata.get("track") or "unknown"),
        ))
    raise BenchmarkError(f"unsupported release sampling benchmark: {benchmark}")


def _sample_hash(benchmark: str, row: dict[str, Any]) -> str:
    return hashlib.sha256(
        f"{_RELEASE_SAMPLE_SEED}\0{benchmark}\0{_case_id(row)}".encode("utf-8")
    ).hexdigest()


def _deterministic_stratified_rows(
    benchmark: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build one reproducible, proportionally interleaved order for nested samples."""
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[_sample_stratum(benchmark, row)].append(row)

    ranked: list[tuple[Fraction, str, str, dict[str, Any]]] = []
    for stratum, values in strata.items():
        ordered = sorted(values, key=lambda row: (_sample_hash(benchmark, row), _case_id(row)))
        size = len(ordered)
        for index, row in enumerate(ordered):
            # Midpoints proportionally interleave large and small strata. Taking
            # any prefix therefore approximates the full stratum distribution.
            position = Fraction((2 * index) + 1, 2 * size)
            ranked.append((position, _sample_hash(benchmark, row), stratum, row))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked]


def _release_usable_ocb_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_rows = _deterministic_stratified_rows("ocb", rows)[:_RELEASE_SOURCE_SAMPLE]
    usable = [
        row
        for row in source_rows
        if _case_id(row) not in _RELEASE_EXCLUDED_OCB_CASE_IDS
    ]
    expected = _DEFAULT_BENCHMARK_SAMPLES["ocb"]
    if len(usable) != expected:
        raise BenchmarkError(
            f"office-release usable OCB subset has {len(usable)} items; expected {expected}"
        )
    return usable


def _release_dataset_name(base_name: str, benchmark: str, sample: int) -> str:
    if sample == _DEFAULT_BENCHMARK_SAMPLES[benchmark]:
        return base_name
    return f"{base_name}-{_RELEASE_SAMPLE_DATASET_TAG}-n{sample}"


def _case_manifest_map(
    prepared: dict[str, Any],
    profile: str,
    *,
    benchmark_samples: dict[str, int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    case_root = Path(prepared.get("case_manifest_root") or "")
    items = {
        benchmark: _read_rows(case_root / f"{profile}-{benchmark}.jsonl")
        for benchmark in ("ocb",)
    }
    if profile == "office-smoke":
        expected = {name: len(cases) for name, cases in _manifest()["smoke_cases"].items()}
        for benchmark, rows in items.items():
            if len(rows) != expected[benchmark]:
                raise BenchmarkError(
                    f"{benchmark} smoke manifest has {len(rows)} items; expected {expected[benchmark]}"
                )
    elif profile == "office-release":
        samples = dict(_DEFAULT_BENCHMARK_SAMPLES)
        if benchmark_samples is not None:
            samples.update(benchmark_samples)
        for benchmark, count in _DEFAULT_BENCHMARK_SAMPLES.items():
            if len(items[benchmark]) < count:
                raise BenchmarkError(
                    f"{benchmark} release manifest has {len(items[benchmark])} items; expected at least {count}"
                )
        for benchmark, sample in samples.items():
            if len(items[benchmark]) < sample:
                raise BenchmarkError(
                    f"{benchmark} manifest has {len(items[benchmark])} items; need {sample}"
                )
            if benchmark == "ocb" and sample == _DEFAULT_BENCHMARK_SAMPLES["ocb"]:
                items[benchmark] = _release_usable_ocb_rows(items[benchmark])
            else:
                items[benchmark] = _deterministic_stratified_rows(
                    benchmark,
                    items[benchmark],
                )[:sample]
    return items


def _ensure_experiment_complete(
    result: Any,
    pending_items: list[Any],
    *,
    case_by_item_id: dict[str, str],
    task_failures: list[tuple[str, str]],
) -> None:
    """Turn Langfuse's exception-isolating runner contract into a Job failure.

    The SDK intentionally filters exceptions out of ``item_results``. That is
    useful for interactive experiments, but a benchmark Job must fail closed
    when even one requested Case did not produce a result.
    """
    returned_ids = {
        str(getattr(getattr(item_result, "item", None), "id", ""))
        for item_result in getattr(result, "item_results", [])
    }
    expected_ids = [str(getattr(item, "id", "")) for item in pending_items]
    missing_case_ids = [
        case_by_item_id.get(item_id, item_id)
        for item_id in expected_ids
        if item_id not in returned_ids
    ]
    if not task_failures and not missing_case_ids and len(returned_ids) == len(expected_ids):
        return
    details = [f"{case_id}: {error}" for case_id, error in task_failures]
    if missing_case_ids:
        details.append("missing results: " + ", ".join(missing_case_ids))
    raise BenchmarkError(
        "Langfuse experiment did not complete all requested Cases "
        f"({len(returned_ids)}/{len(expected_ids)}): "
        + "; ".join(details)
    )


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("._")
    return normalized or "case"


def _benchmark_skill_guidance(skill: str, workspace: Path) -> str:
    """Give benchmark Agents authoritative paths for the selected Skill.

    Case workspaces intentionally contain only immutable benchmark inputs.  The
    selected builtin Skill is loaded from the source checkout, so a model must
    not infer that ``scripts/...`` exists relative to the isolated workspace.
    """
    workspace_skill_root = workspace / "skills" / skill
    skill_root = (
        workspace_skill_root.resolve()
        if (workspace_skill_root / "SKILL.md").is_file()
        else (_ROOT / "nanobot" / "skills" / skill).resolve()
    )
    scripts_dir_name = "Scripts" if os.name == "nt" else "bin"
    lines = [
        "Benchmark runtime paths are authoritative for this isolated Case:",
        f"- Case workspace (inputs/artifacts only): {workspace.resolve()}",
        f"- Selected Skill root: {skill_root}",
        "- Do not search /Users, $HOME, /, or parent directories for these paths.",
        "- Do not use recursive find commands to rediscover a path listed here.",
    ]
    if skill in {"officecli", "officecli-evolved"}:
        launcher = (Path(sys.prefix) / scripts_dir_name / "officecli").resolve()
        lines.extend([
            f"- OfficeCLI launcher: {launcher}",
            f"- OfficeCLI Skill backend: {skill_root / 'scripts' / 'officecli_backend.py'}",
            f"- OfficeCLI runtime contract: {skill_root / 'references' / 'officecli-runtime.json'}",
            "- Invoke the launcher directly; do not search for or install another officecli binary.",
        ])
    return "\n".join(lines)


def _stage_case_workspace(
    *,
    benchmark: str,
    source: dict[str, Any],
    run_root: Path,
    skill: str,
    model_preset: str,
    skill_override_dir: Path | None = None,
) -> Path:
    case_id = str(source["metadata"]["case_id"])
    workspace = run_root / benchmark / skill / _safe_component(model_preset) / _safe_component(case_id)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    # A resumed item may have staged files from an earlier model/evaluator
    # failure.  Start the retry from the immutable benchmark fixture instead
    # of letting copytree fail on (or reuse) the old output directory.
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    paths = source["input"].get("reference_paths") or source["input"].get("material_paths") or []
    destination = workspace / "reference_files"
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise BenchmarkError(f"benchmark material is unavailable: {path}")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination / path.name)
    if skill_override_dir is not None:
        skill_destination = workspace / "skills" / skill
        skill_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_override_dir, skill_destination)
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
    for benchmark in ("ocb",):
        revision = prepared["repositories"][benchmark]["revision"]
        visibility = "licensed" if allow_licensed_content else "redacted"
        name = f"mybot-{benchmark}-{revision[:12]}-{profile}-{visibility}-v1"
        metadata = {
            "schema_version": "1",
            "profile": profile,
            "benchmark": benchmark,
            "code_revision": revision,
            "dataset_revision": prepared["repositories"][benchmark].get("dataset_revision", "code-pinned"),
            "evaluation_source": "langfuse_terra",
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
        _emit_evaluation_progress(
            "prepare_stage", stage="source", label="Validate pinned benchmark source"
        )
        sources = {
            name: str(_clone_at_revision(name, spec, root))
            for name, spec in manifest["repositories"].items()
        }
        python = _ensure_benchmark_venv(root)
        if install:
            _emit_evaluation_progress(
                "prepare_stage", stage="dependencies", label="Install benchmark dependencies"
            )
            _run([str(python), "-m", "pip", "install", "-r", str(root / "constraints.txt")])
        libreoffice = _probe_soffice(soffice, soffice_version)
        dataset_sources: dict[str, str] = {}
        ocb_spec = manifest["repositories"]["ocb"]
        _emit_evaluation_progress(
            "prepare_stage", stage="dataset", label="Download pinned OCB metadata"
        )
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
        dataset_sources["ocb"] = str(ocb_data)
        case_manifest_root = root / "cases"
        ocb_case_ids = (
            [int(case_id) for case_id in manifest["smoke_cases"]["ocb"]]
            if profile == "office-smoke"
            else None
        )
        _emit_evaluation_progress(
            "prepare_stage", stage="manifest", label="Materialize fixed smoke cases"
        )
        _materialize_manifest(
            python,
            "ocb",
            ocb_data,
            case_manifest_root / f"{profile}-ocb.jsonl",
            case_ids=ocb_case_ids,
        )
        ocb_manifest_path = case_manifest_root / f"{profile}-ocb.jsonl"
        ocb_rows = _read_rows(ocb_manifest_path)
        if profile == "office-release":
            ocb_rows = _release_usable_ocb_rows(ocb_rows)
            _write_rows(ocb_manifest_path, ocb_rows)
        if allow_licensed_content:
            _emit_evaluation_progress(
                "prepare_stage", stage="references", label="Download licensed Office references"
            )
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
                ocb_manifest_path,
                case_ids=ocb_case_ids,
            )
            if profile == "office-release":
                _write_rows(
                    ocb_manifest_path,
                    _release_usable_ocb_rows(_read_rows(ocb_manifest_path)),
                )
            missing_ocb = _missing_ocb_references(
                _read_rows(ocb_manifest_path)
            )
            if missing_ocb:
                raise BenchmarkError(
                    "OCB reference assets remain unavailable after the pinned official downloader: "
                    + ", ".join(missing_ocb)
                    + ". Configure the upstream conversion credentials or place the official converted "
                    "files in the external OCB reference_files cache, then rerun prepare."
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
            "release_source_sample": _RELEASE_SOURCE_SAMPLE if profile == "office-release" else None,
            "release_excluded_case_ids": (
                sorted(_RELEASE_EXCLUDED_OCB_CASE_IDS, key=int)
                if profile == "office-release"
                else []
            ),
            "release_usable_case_count": (
                _DEFAULT_BENCHMARK_SAMPLES["ocb"] if profile == "office-release" else None
            ),
            "constraints_sha256": hashlib.sha256(_CONSTRAINTS_PATH.read_bytes()).hexdigest(),
            "libreoffice": libreoffice,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        prepared["case_manifest_sha256"] = _case_manifest_digests(case_manifest_root, profile)
        prepared["asset_fingerprint"] = _fingerprint(prepared)
        runtime = _langfuse_runtime()
        try:
            _emit_evaluation_progress(
                "prepare_stage", stage="langfuse_dataset", label="Upload prepared Langfuse Dataset"
            )
            prepared["datasets"] = _upload_prepared_datasets(
                runtime,
                prepared,
                profile,
                case_manifest_root,
                allow_licensed_content=allow_licensed_content,
            )
            _emit_evaluation_progress(
                "prepare_stage", stage="cloud_smoke", label="Verify Langfuse Cloud write and readback"
            )
            prepared["cloud_smoke"] = _cloud_smoke(runtime)
        finally:
            runtime.shutdown()
        _emit_evaluation_progress(
            "prepare_stage", stage="finalize", label="Write prepared profile fingerprint"
        )
        prepared["fingerprint"] = _fingerprint(prepared)
        path = _prepared_path(root, profile)
        path.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Prepared {profile}[/green]  {path}")
        console.print("[yellow]Raw files stay in external cache; Scores, Runs and Annotation Queue stay in Langfuse.[/yellow]")
    except BenchmarkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def estimate_payload(
    profile: str,
    manifest: dict[str, Any],
    benchmark_samples: dict[str, int] | None = None,
) -> dict[str, Any]:
    if profile == "office-smoke":
        counts = {name: len(items) for name, items in manifest["smoke_cases"].items()}
    else:
        counts = dict(_DEFAULT_BENCHMARK_SAMPLES)
        if benchmark_samples is not None:
            counts.update(benchmark_samples)
    runs = sum(counts.values()) * len(manifest["skills"])
    token_estimate = manifest["estimate_tokens_per_case"]
    judged = counts["ocb"] * len(manifest["skills"])
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


def _validate_benchmark_samples(profile: str, samples: dict[str, int]) -> None:
    if profile != "office-release":
        return
    for benchmark, allowed in _BENCHMARK_SAMPLES.items():
        sample = samples[benchmark]
        if sample not in allowed:
            formatted = ", ".join(str(value) for value in allowed)
            raise BenchmarkError(f"office-release {benchmark} sample must be one of {formatted}")


@benchmark_app.command("estimate")
def estimate(
    profile: str = typer.Option(..., "--profile"),
    model_presets: list[str] | None = typer.Option(None, "--model-preset"),
    ocb_sample: int = typer.Option(211, "--ocb-sample", min=1, max=1018),
) -> None:
    """Print a pre-run token estimate without calling any model."""
    try:
        _validate_profile(profile)
        if profile == "ci":
            console.print(json.dumps({"profile": "ci", "estimated_tokens": {"total": 0}}, indent=2))
            return
        benchmark_samples = {
            "ocb": ocb_sample,
        }
        _validate_benchmark_samples(profile, benchmark_samples)
        manifest = _manifest()
        configured_models = manifest["models"]["agent"]
        if isinstance(configured_models, str):
            configured_models = [configured_models]
        selected_models = tuple(model_presets or configured_models)
        invalid_models = sorted(set(selected_models) - set(configured_models))
        if invalid_models:
            raise BenchmarkError(f"unsupported Office model presets: {invalid_models}")
        payload = estimate_payload(profile, manifest, benchmark_samples)
        model_count = len(selected_models)
        payload["skill_runs"] *= model_count
        payload["model_runs"] = payload["skill_runs"]
        payload["judge_runs"] *= model_count
        for name in ("agent_input", "agent_output", "judge_input", "judge_output", "total"):
            payload["estimated_tokens"][name] *= model_count
        console.print_json(data=payload)
    except BenchmarkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _load_case_items(
    prepared: dict[str, Any],
    profile: str,
    *,
    benchmark_samples: dict[str, int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load already-prepared case manifests without downloading during run."""
    return _case_manifest_map(
        prepared,
        profile,
        benchmark_samples=benchmark_samples,
    )


def _validate_case_assets(items: dict[str, list[dict[str, Any]]]) -> None:
    missing = sorted({
        Path(path).name
        for rows in items.values()
        for row in rows
        for field in ("reference_paths",)
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
    force_rerun: bool = False,
    skill_override_dir: Path | None = None,
) -> dict[str, Any]:
    from nanobot.nanobot import Nanobot

    prompt = str(source["input"]["prompt"])
    case_id = str(source["metadata"]["case_id"])
    _emit_evaluation_progress(
        "case_started",
        benchmark=benchmark,
        skill=skill,
        model_preset=model_preset,
        case_id=case_id,
        variant=f"{benchmark}/{skill}/{model_preset}",
    )
    status = "failed"
    cached: dict[str, Any] | None = None
    try:
        if not force_rerun:
            cached = _load_case_result(
                run_root=run_root,
                benchmark=benchmark,
                skill=skill,
                case_id=case_id,
                model_preset=model_preset,
                source=source,
            )
        if cached is not None:
            workspace = Path(str(cached["workspace"])).expanduser().resolve()
            status = "completed"
            return {
                "content": cached.get("content", ""),
                "tools_used": cached.get("tools_used", []),
                "case_id": case_id,
                "skill": skill,
                "workspace_ready": True,
                "checkpoint_reused": True,
            }
        workspace = _stage_case_workspace(
            benchmark=benchmark,
            source=source,
            run_root=run_root,
            skill=skill,
            model_preset=model_preset,
            skill_override_dir=skill_override_dir,
        )
        guided_prompt = (
            _benchmark_skill_guidance(skill, workspace)
            + "\n\nCase request:\n"
            + prompt
        )
        async with Nanobot.from_config(workspace=workspace) as bot:
            bot._loop.set_model_preset(model_preset, publish_update=False)
            result = await bot.run(
                guided_prompt,
                session_key=f"benchmark:{skill}:{model_preset}:{case_id}",
                metadata={
                    "selected_skills": [skill],
                    "benchmark_model_preset": model_preset,
                    "benchmark": benchmark,
                    "benchmark_case_id": case_id,
                },
            )
        if result.stop_reason == "error" or result.error:
            detail = str(result.error or result.content or "unknown model error")[:500]
            raise BenchmarkError(
                f"model execution failed for {benchmark}/{skill}/{case_id}: {detail}"
            )
        _write_case_result(
            run_root=run_root,
            benchmark=benchmark,
            skill=skill,
            case_id=case_id,
            model_preset=model_preset,
            source=source,
            workspace=workspace,
            content=result.content,
            tools_used=result.tools_used,
            stop_reason=result.stop_reason,
        )
        status = "completed"
        return {
            "content": result.content,
            "tools_used": result.tools_used,
            "case_id": case_id,
            "skill": skill,
            "workspace_ready": True,
        }
    finally:
        _emit_evaluation_progress(
            "case_completed",
            benchmark=benchmark,
            skill=skill,
            model_preset=model_preset,
            case_id=case_id,
            variant=f"{benchmark}/{skill}/{model_preset}",
            status=status,
            source="local" if cached is not None else None,
        )


def _case_result_path(
    run_root: Path,
    benchmark: str,
    skill: str,
    case_id: str,
    model_preset: str,
) -> Path:
    digest = hashlib.sha256(
        f"{benchmark}\0{skill}\0{model_preset}\0{case_id}".encode()
    ).hexdigest()
    return run_root / "case-results" / benchmark / skill / _safe_component(model_preset) / f"{digest}.json"


def _case_source_sha256(source: dict[str, Any]) -> str:
    encoded = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_case_result(
    *,
    run_root: Path,
    benchmark: str,
    skill: str,
    case_id: str,
    model_preset: str,
    source: dict[str, Any],
) -> dict[str, Any] | None:
    path = _case_result_path(run_root, benchmark, skill, case_id, model_preset)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") not in {2, 3}:
        return None
    if payload.get("stop_reason") == "error" or (
        payload.get("schema_version") == 2 and _legacy_checkpoint_has_model_error(payload)
    ):
        return None
    expected = {
        "benchmark": benchmark,
        "skill": skill,
        "case_id": case_id,
        "model_preset": model_preset,
        "source_sha256": _case_source_sha256(source),
    }
    if any(payload.get(name) != value for name, value in expected.items()):
        return None
    workspace = Path(str(payload.get("workspace") or "")).expanduser()
    if not workspace.is_dir():
        return None
    return payload


def _legacy_checkpoint_has_model_error(payload: dict[str, Any]) -> bool:
    """Reject v2 checkpoints that persisted a provider error as model output."""
    content = str(payload.get("content") or "").strip().lower()
    if content.startswith("error calling llm:"):
        return True
    return content.startswith("error:") and any(
        marker in content
        for marker in ("api_error", "service temporarily unavailable", "request timed out")
    )


def _write_case_result(
    *,
    run_root: Path,
    benchmark: str,
    skill: str,
    case_id: str,
    model_preset: str,
    source: dict[str, Any],
    workspace: Path,
    content: str,
    tools_used: list[str],
    stop_reason: str | None = "completed",
) -> None:
    path = _case_result_path(run_root, benchmark, skill, case_id, model_preset)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": 3,
        "benchmark": benchmark,
        "skill": skill,
        "case_id": case_id,
        "model_preset": model_preset,
        "source_sha256": _case_source_sha256(source),
        "workspace": str(workspace),
        "content": content,
        "tools_used": tools_used,
        "stop_reason": stop_reason,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)


def _presence_evaluator(*, output: Any, **kwargs: Any):
    from langfuse.experiment import Evaluation

    present = bool(
        isinstance(output, dict)
        and output.get("workspace_ready")
        and output.get("case_id")
    )
    return Evaluation(name="output_present", value=present, data_type="BOOLEAN")


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
    try:
        return client.api.annotation_queues.create_queue(
            name=name,
            score_config_ids=[score_config.id],
            description="Mybot benchmark audit queue; reviewer completes it in Langfuse Japan Cloud",
        )
    except Exception as exc:
        detail = f"{getattr(exc, 'body', '')} {exc}".lower()
        capacity_error = (
            getattr(exc, "status_code", None) == 405
            and "annotation queue" in detail
            and ("maximum number" in detail or "limit" in detail)
        )
        fallback = sorted(
            (
                queue
                for queue in response.data
                if str(getattr(queue, "name", "")).startswith("mybot-")
            ),
            key=lambda queue: str(queue.name),
        )
        if not capacity_error or not fallback:
            raise
        return fallback[0]


def _annotation_queue_not_found(exc: Exception) -> bool:
    detail = f"{getattr(exc, 'body', '')} {exc}".lower()
    return getattr(exc, "status_code", None) == 404 and "annotation queue" in detail


def _resume_run_name(
    profile: str,
    benchmark: str,
    skill: str,
    resume_token: str | None,
    model_preset: str | None = None,
    suffix: str | None = None,
) -> str | None:
    if resume_token is None:
        return None
    model_suffix = f"-{_safe_component(model_preset)}" if model_preset else ""
    extra_suffix = f"-{_safe_component(suffix)}" if suffix else ""
    return f"mybot-{profile}-{benchmark}-{skill}{model_suffix}-job-{resume_token}{extra_suffix}"


def _remote_variant_state(
    runtime: LangfuseRuntime,
    *,
    dataset_name: str,
    run_name: str | None,
) -> dict[str, Any]:
    if run_name is None:
        return {"run_id": None, "run_url": None, "items": {}}
    try:
        run = runtime.client.get_dataset_run(dataset_name=dataset_name, run_name=run_name)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404:
            return {"run_id": None, "run_url": None, "items": {}}
        raise
    experiments = runtime.client.api.experiments.list(
        from_start_time=datetime(2020, 1, 1, tzinfo=timezone.utc),
        id=str(run.id),
        limit=2,
    ).data
    states: dict[str, dict[str, Any]] = {}
    if experiments:
        cursor: str | None = None
        while True:
            page = runtime.client.api.experiments.list_items(
                from_start_time=experiments[0].start_time,
                experiment_id=str(run.id),
                limit=100,
                score_limit=1,
                cursor=cursor,
            )
            for item in page.data:
                if item.end_time is None:
                    continue
                item_id = str(item.experiment_item_id)
                level = str(item.level).upper()
                end_time = item.end_time
                previous = states.get(item_id)
                if previous is not None and previous["end_time"] >= end_time:
                    continue
                states[item_id] = {
                    "status": "failed" if level.endswith("ERROR") else "completed",
                    "trace_id": str(item.trace_id),
                    "end_time": end_time,
                }
            cursor = page.meta.cursor
            if not cursor:
                break
    project_id = runtime.client._get_project_id()
    dataset_id = getattr(run, "dataset_id", None) or getattr(run, "datasetId", None)
    run_url = (
        f"{runtime.base_url}/project/{project_id}/datasets/{dataset_id}/runs/{run.id}"
        if project_id and dataset_id else None
    )
    for state in states.values():
        state.pop("end_time", None)
        trace_id = state.get("trace_id")
        trace_scores = _remote_trace_scores(runtime, str(trace_id))
        state["score_names"] = sorted(trace_scores)
        state["score_values"] = {
            name: getattr(score, "value", None)
            for name, score in trace_scores.items()
        }
        state["trace_url"] = (
            f"{runtime.base_url}/project/{project_id}/traces/{trace_id}"
            if project_id and trace_id else None
        )
    return {"run_id": str(run.id), "run_url": run_url, "items": states}


def _normalized_score_name(name: str) -> str:
    if name == "mybot-ocb-judge-v1":
        return "mybot_score"
    return name


def _remote_trace_scores(runtime: LangfuseRuntime, trace_id: str) -> dict[str, Any]:
    scores: dict[str, Any] = {}
    cursor: str | None = None
    while True:
        page = runtime.client.api.scores_v3.get_many_v3(
            trace_id=trace_id,
            fields="subject",
            limit=100,
            cursor=cursor,
        )
        for score in page.data:
            scores[_normalized_score_name(str(score.name))] = score
        cursor = page.meta.cursor
        if not cursor:
            return scores


def _required_local_scores(benchmark: str) -> set[str]:
    return {"output_present"}


def _remote_item_has_required_scores(benchmark: str, state: dict[str, Any]) -> bool:
    """Only reuse a remote item when its required local scores are valid."""
    names = set(state.get("score_names") or [])
    if not _required_local_scores(benchmark).issubset(names):
        return False
    values = state.get("score_values") or {}
    if not bool(values.get("output_present")):
        return False
    return True


def _wait_for_local_scores(
    runtime: LangfuseRuntime,
    *,
    trace_id: str,
    benchmark: str,
) -> dict[str, Any]:
    required = _required_local_scores(benchmark)
    found: dict[str, Any] = {}
    for attempt in range(_SCORE_READBACK_ATTEMPTS):
        found = _remote_trace_scores(runtime, trace_id)
        if required.issubset(found):
            return found
        if attempt + 1 < _SCORE_READBACK_ATTEMPTS:
            time.sleep(_SCORE_READBACK_INTERVAL_SEC)
    missing = sorted(required - set(found))
    # The Experiment and its Case can already be complete while Langfuse's
    # asynchronous score consumer is still catching up.  Keep the result
    # readable and let the history reader observe the score on a later poll.
    console.print(
        f"[yellow]Langfuse score readback is still pending for trace {trace_id}: "
        f"missing {', '.join(missing)}; continuing[/yellow]"
    )
    return found


def _flush_benchmark_runtime(runtime: LangfuseRuntime) -> None:
    """Flush benchmark telemetry without failing completed Cases on queue lag."""
    try:
        runtime.flush(strict=True)
    except LangfuseFlushTimeoutError as exc:
        detail = str(exc).lower()
        if "score ingestion" not in detail and "media upload" not in detail:
            raise
        if "score ingestion" in detail:
            console.print(
                "[yellow]Langfuse score ingestion is still draining; benchmark Cases are "
                "complete and the score will appear after remote ingestion.[/yellow]"
            )
        else:
            console.print(
                "[yellow]Langfuse media uploads are still draining; benchmark Cases and "
                "their structured outputs are complete, so execution will continue.[/yellow]"
            )


def _recover_run_experiment_media_timeout(
    exc: BaseException,
    *,
    benchmark: str,
    pending_items: list[Any],
    recovered: dict[str, Any],
    task_failures: list[tuple[str, str]],
) -> bool:
    """Accept the SDK's final media flush timeout only after remote completeness readback."""
    if not isinstance(exc, LangfuseFlushTimeoutError):
        return False
    if "media upload queue" not in str(exc).lower() or task_failures:
        return False
    expected_item_ids = {
        str(getattr(item, "id", ""))
        for item in pending_items
        if str(getattr(item, "id", ""))
    }
    remote_items = recovered.get("items") or {}
    return bool(
        recovered.get("run_id")
        and expected_item_ids
        and all(
            item_id in remote_items
            and remote_items[item_id].get("status") == "completed"
            and _remote_item_has_required_scores(benchmark, remote_items[item_id])
            for item_id in expected_item_ids
        )
    )


def _enqueue_review_items(
    runtime: LangfuseRuntime,
    *,
    queue: Any,
    trace_ids: list[str],
    profile: str,
) -> tuple[str, int]:
    from langfuse.api.annotation_queues.types.annotation_queue_object_type import (
        AnnotationQueueObjectType,
    )

    trace_ids = list(dict.fromkeys(trace_id for trace_id in trace_ids if trace_id))
    if profile == "office-smoke":
        selected = trace_ids
    else:
        selected = sorted(
            trace_ids,
            key=lambda trace_id: hashlib.sha256(str(trace_id).encode()).hexdigest(),
        )[: max(1, math.ceil(len(trace_ids) * 0.05))]
    queue_name = str(getattr(queue, "name", "")).strip()
    for attempt in range(2):
        try:
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
        except Exception as exc:
            if attempt or not queue_name or not _annotation_queue_not_found(exc):
                raise
            queue = _get_annotation_queue(runtime.client, queue_name)
    raise AssertionError("annotation queue retry loop exhausted")


@benchmark_app.command("run")
def run(
    profile: str = typer.Option(..., "--profile"),
    model_presets: list[str] | None = typer.Option(None, "--model-preset"),
    cache_dir: Path | None = typer.Option(None, "--cache-dir"),
    ocb_sample: int = typer.Option(211, "--ocb-sample", min=1, max=1018),
    benchmarks: list[str] | None = typer.Option(None, "--benchmark"),
    skills: list[str] | None = typer.Option(None, "--skill"),
    parent_run_id: str | None = typer.Option(None, "--parent-run-id"),
    resume_state: Path | None = typer.Option(None, "--resume-state", hidden=True),
    resume_token: str | None = typer.Option(None, "--resume-token", hidden=True),
    rerun_benchmark: str | None = typer.Option(None, "--rerun-benchmark", hidden=True),
    rerun_skill: str | None = typer.Option(None, "--rerun-skill", hidden=True),
    rerun_model_preset: str | None = typer.Option(None, "--rerun-model-preset", hidden=True),
    rerun_case: str | None = typer.Option(None, "--rerun-case", hidden=True),
    skill_override_dir: Path | None = typer.Option(None, "--skill-override-dir", hidden=True),
    run_name_suffix: str | None = typer.Option(None, "--run-name-suffix", hidden=True),
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
                "tests/skills/test_officecli_runtime.py",
                "tests/cli/test_benchmark_contract.py",
                "-q",
            ]
            console.print(_run(command, cwd=_ROOT))
            return
        manifest = _manifest()
        benchmark_samples = {
            "ocb": ocb_sample,
        }
        _validate_benchmark_samples(profile, benchmark_samples)
        selected_benchmarks = _select_values(
            benchmarks,
            tuple(manifest["repositories"]),
            "benchmark",
        )
        allowed_skills = tuple(manifest["skills"])
        if skill_override_dir is not None:
            skill_override_dir = skill_override_dir.expanduser().resolve()
            if not (skill_override_dir / "SKILL.md").is_file():
                raise BenchmarkError("--skill-override-dir must contain SKILL.md")
            override_name = skill_override_dir.name
            allowed_skills = tuple(dict.fromkeys((*allowed_skills, override_name)))
        selected_skills = _select_values(skills, allowed_skills, "Skill")
        if skill_override_dir is not None and selected_skills != (skill_override_dir.name,):
            raise BenchmarkError("--skill-override-dir requires selecting exactly its directory name")
        configured_models = manifest["models"]["agent"]
        if isinstance(configured_models, str):
            configured_models = [configured_models]
        selected_model_presets = _select_values(
            model_presets,
            tuple(configured_models),
            "model preset",
        )
        if parent_run_id and (len(selected_benchmarks) != 1 or len(selected_skills) != 1):
            raise BenchmarkError(
                "--parent-run-id requires exactly one --benchmark and one --skill"
            )
        if (resume_state is None) != (resume_token is None):
            raise BenchmarkError("--resume-state and --resume-token must be provided together")
        resume_payload: dict[str, Any] = {}
        if resume_state is not None and resume_token is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", resume_token):
                raise BenchmarkError("invalid --resume-token")
            try:
                resume_payload = json.loads(resume_state.expanduser().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BenchmarkError(f"resume checkpoint is unavailable: {exc}") from exc
            checkpoint_token = str(resume_payload.get("resume_token") or resume_payload.get("job_id") or "")
            if checkpoint_token != resume_token:
                raise BenchmarkError("resume checkpoint token does not match --resume-token")
        rerun_values = (rerun_benchmark, rerun_skill, rerun_model_preset, rerun_case)
        if any(rerun_values) and not all(rerun_values):
            raise BenchmarkError(
                "--rerun-benchmark, --rerun-skill, --rerun-model-preset, and --rerun-case must be provided together"
            )
        if all(rerun_values) and resume_state is None:
            raise BenchmarkError("single-Case rerun requires a Job resume checkpoint")
        if rerun_benchmark and rerun_benchmark not in selected_benchmarks:
            raise BenchmarkError("rerun benchmark is not selected by this Job")
        if rerun_skill and rerun_skill not in selected_skills:
            raise BenchmarkError("rerun Skill is not selected by this Job")
        if len(selected_model_presets) > 1:
            for selected_model in selected_model_presets:
                run(
                    profile=profile,
                    model_presets=[selected_model],
                    cache_dir=cache_dir,
                    ocb_sample=ocb_sample,
                    benchmarks=benchmarks,
                    skills=skills,
                    parent_run_id=parent_run_id,
                    resume_state=resume_state,
                    resume_token=resume_token,
                    rerun_benchmark=rerun_benchmark,
                    rerun_skill=rerun_skill,
                    rerun_model_preset=rerun_model_preset,
                    rerun_case=rerun_case,
                    skill_override_dir=skill_override_dir,
                    run_name_suffix=run_name_suffix,
                )
            return
        model_preset = selected_model_presets[0]
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
            benchmark_samples=benchmark_samples,
        )
        selected_items = {
            name: values
            for name, values in items.items()
            if name in selected_benchmarks
        }
        if rerun_benchmark and rerun_case:
            matching = [
                item
                for item in selected_items[rerun_benchmark]
                if str(item["metadata"]["case_id"]) == rerun_case
            ]
            if not matching:
                raise BenchmarkError(
                    f"rerun Case {rerun_benchmark}/{rerun_skill}/{rerun_case} is unavailable"
                )
            selected_items = {rerun_benchmark: matching}
            items = {rerun_benchmark: matching}
            selected_benchmarks = (rerun_benchmark,)
            selected_skills = (str(rerun_skill),)
            selected_model_presets = (str(rerun_model_preset),)
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
            "benchmark_samples": json.dumps(benchmark_samples, sort_keys=True),
            "sample_strategy": (
                _RELEASE_SAMPLE_STRATEGY if profile == "office-release" else "fixed-smoke-cases"
            ),
            "sample_seed": _RELEASE_SAMPLE_SEED if profile == "office-release" else None,
            "annotation_review_policy": "smoke=100%; release=stable 5% sample",
            "benchmark_filter": list(selected_benchmarks),
            "skill_filter": list(selected_skills),
            "resume_token": resume_token,
            "resume_count": str(resume_payload.get("resume_count") or 0),
        }
        _emit_evaluation_progress(
            "run_started",
            profile=profile,
            total_cases=sum(len(values) for values in selected_items.values()) * len(selected_skills),
            total_variants=len(selected_items) * len(selected_skills),
            model_preset=model_preset,
        )
        if parent_run_id:
            metadata["parent_run_id"] = parent_run_id
        run_root = (
            root / "runs" / profile / "jobs" / resume_token
            if resume_token
            else root / "runs" / profile / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        )
        try:
            review_queue = _get_annotation_queue(
                runtime.client,
                f"mybot-{profile}-review",
            )
            for benchmark, benchmark_items in items.items():
                if benchmark not in selected_benchmarks:
                    continue
                dataset_name = prepared["datasets"][benchmark]
                sample = benchmark_samples[benchmark]
                if profile == "office-release" and sample != _DEFAULT_BENCHMARK_SAMPLES[benchmark]:
                    dataset_name = _release_dataset_name(dataset_name, benchmark, sample)
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
                dataset_item_by_case = {_case_id(item): item for item in dataset.items}
                missing_dataset_items = sorted(set(source_by_case) - set(dataset_item_by_case))
                if missing_dataset_items:
                    raise BenchmarkError(
                        f"Dataset {dataset_name} is missing prepared cases: "
                        + ", ".join(missing_dataset_items)
                    )
                for skill in selected_skills:
                    stable_run_name = _resume_run_name(
                        profile,
                        benchmark,
                        skill,
                        resume_token,
                        model_preset,
                        run_name_suffix,
                    )
                    remote = _remote_variant_state(
                        runtime,
                        dataset_name=dataset_name,
                        run_name=stable_run_name,
                    )
                    remote_items = dict(remote["items"])
                    case_by_item_id = {
                        str(item.id): case_id
                        for case_id, item in dataset_item_by_case.items()
                        if case_id in source_by_case
                    }
                    completed_remote_item_ids = {
                        item_id
                        for item_id, state in remote_items.items()
                        if state.get("status") == "completed"
                        and _remote_item_has_required_scores(benchmark, state)
                    }
                    if rerun_case is not None:
                        completed_remote_item_ids.discard(
                            str(dataset_item_by_case[rerun_case].id)
                        )
                    for item_id, state in remote_items.items():
                        case_id = case_by_item_id.get(item_id)
                        if case_id is None:
                            continue
                        reusable = item_id in completed_remote_item_ids
                        _emit_evaluation_progress(
                            "case_reconciled",
                            profile=profile,
                            benchmark=benchmark,
                            skill=skill,
                            model_preset=model_preset,
                            case_id=case_id,
                            variant=f"{benchmark}/{skill}/{model_preset}",
                            status="completed" if reusable else "pending",
                            source="langfuse",
                            score_status="remote" if reusable else "pending",
                            trace_url=state.get("trace_url"),
                            dataset_run_url=remote.get("run_url"),
                        )
                    pending_items = [
                        dataset_item_by_case[case_id]
                        for case_id in source_by_case
                        if str(dataset_item_by_case[case_id].id) not in completed_remote_item_ids
                    ]
                    if rerun_case is not None:
                        checkpoint_path = _case_result_path(
                            run_root,
                            benchmark,
                            skill,
                            rerun_case,
                            model_preset,
                        )
                        checkpoint_path.unlink(missing_ok=True)
                    cached_cases = sum(
                        _load_case_result(
                            run_root=run_root,
                            benchmark=benchmark,
                            skill=skill,
                            case_id=_case_id(item),
                            model_preset=model_preset,
                            source=source_by_case[_case_id(item)],
                        ) is not None
                        for item in pending_items
                    )
                    _emit_evaluation_progress(
                        "variant_started",
                        profile=profile,
                        benchmark=benchmark,
                        skill=skill,
                        variant=f"{benchmark}/{skill}/{model_preset}",
                        case_count=len(benchmark_items),
                        checkpoint_cases=len(completed_remote_item_ids) + cached_cases,
                        pending_cases=len(pending_items),
                        model_pending_cases=len(pending_items) - cached_cases,
                        run_name=stable_run_name,
                    )

                    task_failures: list[tuple[str, str]] = []

                    def task(*, item, _skill=skill, _benchmark=benchmark):
                        case_id = _case_id(item)
                        try:
                            source = source_by_case[case_id]
                        except KeyError as exc:
                            raise BenchmarkError(
                                f"Dataset {dataset_name} item {case_id} is not in the prepared manifest"
                            ) from exc
                        try:
                            return _run_agent_item(
                                source=source,
                                benchmark=_benchmark,
                                skill=_skill,
                                model_preset=model_preset,
                                run_root=run_root,
                                force_rerun=rerun_case is not None,
                                skill_override_dir=skill_override_dir,
                            )
                        except Exception as exc:
                            task_failures.append((case_id, str(exc)[:500]))
                            raise

                    evaluators = [_presence_evaluator]
                    experiment_metadata = {
                        **metadata,
                        "benchmark": benchmark,
                        "skill": skill,
                        "model_preset": model_preset,
                        "dataset_name": dataset_name,
                        "evaluation_source": "langfuse_terra",
                        "required_remote_score": "mybot_score",
                        "annotation_queue_name": str(review_queue.name),
                    }
                    result = None
                    if pending_items:
                        score_ingestion = runtime.synchronous_score_ingestion()
                        try:
                            with score_ingestion:
                                result = runtime.client.run_experiment(
                                    name=f"mybot-{profile}-{benchmark}-{skill}-{_safe_component(model_preset)}",
                                    run_name=stable_run_name,
                                    description=(
                                        "Mybot public Office comparison; Langfuse Terra Judge scores "
                                        "are the evaluation source"
                                    ),
                                    data=pending_items,
                                    task=task,
                                    evaluators=evaluators,
                                    max_concurrency=2,
                                    metadata=experiment_metadata,
                                    _dataset_version=dataset.version,
                                )
                            _ensure_experiment_complete(
                                result,
                                pending_items,
                                case_by_item_id=case_by_item_id,
                                task_failures=task_failures,
                            )
                            score_ingestion.raise_for_errors()
                        except Exception as exc:
                            recovered = _remote_variant_state(
                                runtime,
                                dataset_name=dataset_name,
                                run_name=stable_run_name,
                            )
                            if recovered.get("run_id"):
                                _emit_evaluation_progress(
                                    "variant_run_discovered",
                                    profile=profile,
                                    benchmark=benchmark,
                                    skill=skill,
                                    model_preset=model_preset,
                                    dataset_run_id=recovered["run_id"],
                                    dataset_run_url=recovered.get("run_url"),
                                )
                            score_ingestion.raise_for_errors()
                            if not _recover_run_experiment_media_timeout(
                                exc,
                                benchmark=benchmark,
                                pending_items=pending_items,
                                recovered=recovered,
                                task_failures=task_failures,
                            ):
                                raise
                            remote = recovered
                            remote_items = dict(recovered["items"])
                            completed_remote_item_ids.update(
                                str(getattr(item, "id", "")) for item in pending_items
                            )
                            console.print(
                                "[yellow]Langfuse run_experiment media uploads are still "
                                "draining; remote readback confirms every requested Case and "
                                "required local score, so execution will continue.[/yellow]"
                            )
                    dataset_run_id = (
                        result.dataset_run_id if result is not None else remote.get("run_id")
                    )
                    dataset_run_url = (
                        result.dataset_run_url if result is not None else remote.get("run_url")
                    )
                    _emit_evaluation_progress(
                        "variant_run_discovered",
                        profile=profile,
                        benchmark=benchmark,
                        skill=skill,
                        model_preset=model_preset,
                        dataset_run_id=dataset_run_id,
                        dataset_run_url=dataset_run_url,
                    )
                    trace_ids = [
                        str(state["trace_id"])
                        for state in remote_items.values()
                        if state.get("trace_id")
                    ]
                    pending_score_case_ids: set[str] = set()
                    score_values_by_case: dict[str, dict[str, Any]] = {}
                    if result is not None:
                        for item in result.item_results:
                            if item.trace_id:
                                score_values = _wait_for_local_scores(
                                    runtime,
                                    trace_id=str(item.trace_id),
                                    benchmark=benchmark,
                                )
                                case_id = case_by_item_id.get(str(item.item.id))
                                if case_id is not None:
                                    score_values_by_case[case_id] = score_values
                                if case_id is not None and not _required_local_scores(benchmark).issubset(
                                    score_values
                                ):
                                    pending_score_case_ids.add(case_id)
                        trace_ids.extend(
                            str(item.trace_id)
                            for item in result.item_results
                            if item.trace_id
                        )
                        for item in result.item_results:
                            case_id = case_by_item_id.get(str(item.item.id))
                            if case_id is None:
                                continue
                            trace_id = str(item.trace_id) if item.trace_id else None
                            project_id = runtime.client._get_project_id()
                            _emit_evaluation_progress(
                                "case_reconciled",
                                profile=profile,
                                benchmark=benchmark,
                                skill=skill,
                                model_preset=model_preset,
                                case_id=case_id,
                                variant=f"{benchmark}/{skill}/{model_preset}",
                                status="completed",
                                source="langfuse",
                                score_status=(
                                    "pending" if case_id in pending_score_case_ids else "remote"
                                ),
                                scores=score_values_by_case.get(case_id, {}),
                                trace_url=(
                                    f"{runtime.base_url}/project/{project_id}/traces/{trace_id}"
                                    if project_id and trace_id else None
                                ),
                                dataset_run_url=dataset_run_url,
                            )
                    queue_id, added = _enqueue_review_items(
                        runtime,
                        queue=review_queue,
                        trace_ids=trace_ids,
                        profile=profile,
                    )
                    console.print(
                        f"[green]{stable_run_name or (result.run_name if result else 'resumed')}[/green] "
                        f"dataset_run={dataset_run_id} "
                        f"review_queue={queue_id} (+{added}) "
                        f"{dataset_run_url or ''}"
                    )
                    _emit_evaluation_progress(
                        "variant_completed",
                        profile=profile,
                        benchmark=benchmark,
                        skill=skill,
                        model_preset=model_preset,
                        dataset_run_id=dataset_run_id,
                        dataset_run_url=dataset_run_url,
                        checkpoint_cases=len(completed_remote_item_ids) + cached_cases,
                        executed_cases=len(pending_items),
                    )
            _flush_benchmark_runtime(runtime)
            _emit_evaluation_progress("run_completed", profile=profile)
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


def _score_map(item: Any, client: Any | None = None) -> dict[str, Any]:
    scores = {
        _normalized_score_name(str(score.name)): score
        for score in (item.scores or [])
    }
    trace_id = getattr(item, "trace_id", None)
    if client is None or not trace_id:
        return scores
    cursor: str | None = None
    while True:
        page = client.api.scores_v3.get_many_v3(
            trace_id=str(trace_id),
            fields="subject",
            limit=100,
            cursor=cursor,
        )
        for score in page.data:
            scores[_normalized_score_name(str(score.name))] = score
        cursor = page.meta.cursor
        if not cursor:
            return scores


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
        metadata.get("required_remote_score") or "mybot_score"
    )
    for item in items:
        scores = _score_map(item, client)
        if "output_present" not in scores or not bool(scores["output_present"].value):
            raise BenchmarkError("every run item must pass output_present before export")
        if required_score not in scores:
            raise BenchmarkError(
                f"every {benchmark} item needs {required_score}; Langfuse evaluator may still be pending"
            )
    queue_name = str(metadata.get("annotation_queue_name") or "")
    queues = _annotation_queues(client)
    queue = next((item for item in queues if item.name == queue_name), None)
    required = len(items) if profile == "office-smoke" else max(1, math.ceil(len(items) * 0.05))
    if queue is None:
        trace_ids = {str(item.trace_id) for item in items if item.trace_id}
        candidates: list[tuple[int, Any]] = []
        for candidate in queues:
            if not str(getattr(candidate, "name", "")).startswith("mybot-"):
                continue
            candidate_ids = {
                str(queue_item.object_id)
                for queue_item in _annotation_queue_items(client, candidate.id)
            }
            overlap = len(trace_ids & candidate_ids)
            if overlap >= required:
                candidates.append((overlap, candidate))
        if candidates:
            queue = max(candidates, key=lambda value: (value[0], str(value[1].name)))[1]
    if queue is None:
        raise BenchmarkError(f"Annotation Queue not found: {queue_name or '(missing metadata)'}")
    reviewed_object_ids = {
        str(queue_item.object_id)
        for queue_item in _annotation_queue_items(client, queue.id)
        if str(queue_item.status).upper().endswith("COMPLETED")
    }
    reviewed_items = [item for item in items if item.trace_id in reviewed_object_ids]
    reviewed = len(reviewed_items)
    if reviewed < required:
        raise BenchmarkError(f"Annotation Queue review incomplete: {reviewed}/{required}")
    if any("mybot-human-review" not in _score_map(item, client) for item in reviewed_items):
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
        "score_names": sorted({name for item in items for name in _score_map(item, client)}),
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
