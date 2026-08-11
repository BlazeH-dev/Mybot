"""Trusted evaluation-suite discovery and preflight contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.evaluations.credentials import adobe_pdf_services_available

ROOT = Path(__file__).resolve().parents[2]
OFFICE_PROFILE_PATH = ROOT / "benchmarks" / "office" / "profiles.json"
OFFICE_SUITE_MANIFEST_PATH = ROOT / "benchmarks" / "suites" / "office" / "manifest.yaml"
DEFAULT_CACHE = Path.home() / ".cache" / "nanobot" / "benchmarks"
OFFICE_BENCHMARKS = ("ocb",)
OFFICE_PROFILES = ("ci", "office-smoke", "office-release")
OFFICE_BENCHMARK_SAMPLES = {
    "ocb": (255, 509, 1018),
}
DEFAULT_OFFICE_BENCHMARK_SAMPLES = {
    benchmark: samples[-1] for benchmark, samples in OFFICE_BENCHMARK_SAMPLES.items()
}
TERMINAL_JOB_STATUSES = frozenset(
    {"awaiting_review", "completed", "failed", "cancelled", "interrupted"}
)


@dataclass(frozen=True)
class EvaluationRequest:
    suite_id: str = "office"
    profile: str = "office-smoke"
    action: str = "run"
    benchmarks: tuple[str, ...] = OFFICE_BENCHMARKS
    skills: tuple[str, ...] = ("officecli",)
    model_presets: tuple[str, ...] = ("gpt-5-6-luna", "deepseek-v4-flash")
    runtime_profiles: tuple[str, ...] = ("default",)
    benchmark_samples: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_OFFICE_BENCHMARK_SAMPLES)
    )
    allow_licensed_content: bool = False

    def __post_init__(self) -> None:
        samples = dict(DEFAULT_OFFICE_BENCHMARK_SAMPLES)
        samples.update(self.benchmark_samples)
        object.__setattr__(self, "benchmark_samples", samples)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EvaluationRequest:
        def strings(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = payload.get(name)
            if raw is None:
                return default
            if not isinstance(raw, list):
                raise ValueError(f"{name} must be an array")
            values = tuple(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
            return values

        samples = dict(DEFAULT_OFFICE_BENCHMARK_SAMPLES)
        raw_samples = payload.get("benchmark_samples")
        if raw_samples is not None:
            if not isinstance(raw_samples, dict):
                raise ValueError("benchmark_samples must be an object")
            invalid = sorted(set(raw_samples) - set(OFFICE_BENCHMARKS))
            if invalid:
                raise ValueError(f"unknown benchmark samples: {invalid}")
            for benchmark, value in raw_samples.items():
                if not isinstance(value, int):
                    raise ValueError(f"benchmark_samples.{benchmark} must be an integer")
                samples[benchmark] = value
        return cls(
            suite_id=str(payload.get("suite_id") or "office").strip(),
            profile=str(payload.get("profile") or "office-smoke").strip(),
            action=str(payload.get("action") or "run").strip(),
            benchmarks=strings("benchmarks", OFFICE_BENCHMARKS),
            skills=strings("skills", ("officecli",)),
            model_presets=strings("model_presets", ("gpt-5-6-luna", "deepseek-v4-flash")),
            runtime_profiles=strings("runtime_profiles", ("default",)),
            benchmark_samples=samples,
            allow_licensed_content=payload.get("allow_licensed_content") is True,
        )

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreflightResult:
    ready: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    checks: dict[str, Any] = field(default_factory=dict)
    estimate: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class EvaluationSuiteAdapter(Protocol):
    suite_id: str

    def catalog(self, available_skills: list[dict[str, Any]]) -> dict[str, Any]: ...

    def preflight(self, request: EvaluationRequest) -> PreflightResult: ...

    def command(self, request: EvaluationRequest) -> list[str]: ...


def benchmark_cache_root() -> Path:
    return Path(os.environ.get("NANOBOT_BENCHMARK_CACHE", DEFAULT_CACHE)).expanduser().resolve()


def _office_manifest() -> dict[str, Any]:
    return json.loads(OFFICE_PROFILE_PATH.read_text(encoding="utf-8"))


def _office_suite_manifest() -> dict[str, Any]:
    # JSON is valid YAML. Keeping the trusted manifest in this subset avoids a
    # runtime YAML dependency while preserving the documented suite contract.
    return json.loads(OFFICE_SUITE_MANIFEST_PATH.read_text(encoding="utf-8"))


def _soffice_probe(prepared: dict[str, Any] | None) -> dict[str, Any]:
    configured = prepared.get("libreoffice") if isinstance(prepared, dict) else None
    candidates = [
        Path(str(configured.get("path"))) if isinstance(configured, dict) and configured.get("path") else None,
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        Path(shutil.which("soffice") or "") if shutil.which("soffice") else None,
    ]
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return {"available": True, "path": str(candidate), "version": result.stdout.strip()}
    return {"available": False, "path": None, "version": None}


class OfficeEvaluationAdapter:
    suite_id = "office"

    def catalog(self, available_skills: list[dict[str, Any]]) -> dict[str, Any]:
        manifest = _office_manifest()
        suite = _office_suite_manifest()
        agent_models = manifest["models"]["agent"]
        if isinstance(agent_models, str):
            agent_models = [agent_models]
        known = {str(skill["name"]): skill for skill in available_skills if skill.get("name")}
        skills: list[dict[str, Any]] = []
        for name in manifest["skills"]:
            skill = known.get(name)
            available = bool(skill and skill.get("available", False))
            skills.append({
                "id": name,
                "label": name,
                "available": available,
                "compatible": True,
                "reason": None if available else "Skill is not installed or available",
            })
        return {
            "id": self.suite_id,
            "version": suite["version"],
            "label": suite["name"],
            "description": suite["description"],
            "profiles": suite["profiles"],
            "benchmarks": suite["benchmarks"],
            "skills": sorted(skills, key=lambda item: item["label"]),
            "model_presets": [{"id": model, "label": model} for model in agent_models],
            "runtime_profiles": [{"id": "default", "label": "Default runtime"}],
            "benchmark_samples": {
                benchmark: list(samples)
                for benchmark, samples in OFFICE_BENCHMARK_SAMPLES.items()
            },
            "extension": {
                "manifest": "benchmarks/suites/<suite-id>/manifest.yaml",
                "adapter": "benchmarks/suites/<suite-id>/adapter.py",
            },
        }

    def _validate(self, request: EvaluationRequest) -> None:
        manifest = _office_manifest()
        agent_models = manifest["models"]["agent"]
        if isinstance(agent_models, str):
            agent_models = [agent_models]
        if request.suite_id != self.suite_id:
            raise ValueError(f"unknown evaluation suite: {request.suite_id}")
        if request.action not in {"run", "prepare"}:
            raise ValueError("action must be run or prepare")
        if request.profile not in OFFICE_PROFILES:
            raise ValueError(f"unknown profile: {request.profile}")
        invalid_benchmarks = sorted(set(request.benchmarks) - set(OFFICE_BENCHMARKS))
        if invalid_benchmarks:
            raise ValueError(f"unknown benchmarks: {invalid_benchmarks}")
        invalid_skills = sorted(set(request.skills) - set(manifest["skills"]))
        if request.profile != "ci" and not request.skills:
            raise ValueError("select at least one Office Skill")
        if invalid_skills:
            raise ValueError(f"unsupported Office Skills: {invalid_skills}")
        invalid_models = sorted(set(request.model_presets) - set(agent_models))
        if request.profile != "ci" and not request.model_presets:
            raise ValueError("select at least one model preset")
        if invalid_models:
            raise ValueError(f"unsupported Office model presets: {invalid_models}")
        if request.runtime_profiles != ("default",):
            raise ValueError("Office suite currently supports only the default Runtime profile")
        if request.profile == "office-release":
            for benchmark in OFFICE_BENCHMARKS:
                if request.benchmark_samples[benchmark] not in OFFICE_BENCHMARK_SAMPLES[benchmark]:
                    allowed = ", ".join(str(value) for value in OFFICE_BENCHMARK_SAMPLES[benchmark])
                    raise ValueError(f"{benchmark} release sample must be one of {allowed}")

    def estimate(self, request: EvaluationRequest) -> dict[str, Any]:
        if request.profile == "ci":
            return {"profile": "ci", "case_counts": {}, "skill_runs": 0, "judge_runs": 0, "estimated_tokens": {"total": 0}}
        manifest = _office_manifest()
        counts = (
            {name: len(items) for name, items in manifest["smoke_cases"].items()}
            if request.profile == "office-smoke"
            else dict(request.benchmark_samples)
        )
        selected_counts = {name: count for name, count in counts.items() if name in request.benchmarks}
        runs = sum(selected_counts.values()) * len(request.skills) * len(request.model_presets)
        judged = selected_counts.get("ocb", 0) * len(request.skills) * len(request.model_presets)
        per_case = manifest["estimate_tokens_per_case"]
        tokens = {
            "agent_input": runs * per_case["agent_input"],
            "agent_output": runs * per_case["agent_output"],
            "judge_input": judged * per_case["judge_input"],
            "judge_output": judged * per_case["judge_output"],
        }
        tokens["total"] = sum(tokens.values())
        return {
            "profile": request.profile,
            "case_counts": selected_counts,
            "skill_runs": runs,
            "model_runs": runs,
            "judge_runs": judged,
            "estimated_tokens": tokens,
        }

    def preflight(self, request: EvaluationRequest) -> PreflightResult:
        self._validate(request)
        config = resolve_config_env_vars(load_config())
        estimate = self.estimate(request)
        if request.profile == "ci":
            return PreflightResult(
                ready=True,
                checks={"offline": True, "estimated_tokens": 0},
                estimate=estimate,
            )

        prepared_path = benchmark_cache_root() / f"{request.profile}.prepared.json"
        prepared: dict[str, Any] | None = None
        if prepared_path.is_file():
            try:
                prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prepared = None
        langfuse = config.observability.langfuse
        soffice = _soffice_probe(prepared)
        provider_ready: dict[str, bool] = {}
        for preset_name in request.model_presets:
            preset = config.resolve_preset(preset_name)
            provider = getattr(config.providers, preset.provider, None)
            provider_ready[preset_name] = bool(
                provider
                and provider.api_key
                and (provider.api_base or preset.provider == "deepseek")
            )
        checks = {
            "prepared": prepared is not None,
            "prepared_path": str(prepared_path),
            "licensed_content_uploaded": bool(prepared and prepared.get("licensed_content_uploaded")),
            "langfuse_enabled": bool(langfuse.enabled),
            "langfuse_configured": bool(langfuse.resolved_public_key() and langfuse.resolved_secret_key()),
            "langfuse_base_url": langfuse.resolved_base_url(),
            "capture_content": bool(langfuse.capture_content),
            "model_provider_configured": all(provider_ready.values()),
            "model_provider_status": provider_ready,
            "libreoffice": soffice,
            "adobe_credentials": adobe_pdf_services_available(),
        }
        blockers: list[str] = []
        warnings: list[str] = []
        if request.action == "run" and prepared is None:
            blockers.append("Profile has not been prepared")
        if not langfuse.enabled or not checks["langfuse_configured"]:
            blockers.append("Langfuse Japan Cloud is not configured")
        if langfuse.resolved_base_url() != "https://jp.cloud.langfuse.com":
            blockers.append("Langfuse base URL must use Japan Cloud")
        if request.action == "run" and not checks["model_provider_configured"]:
            blockers.append("one or more selected model providers are not configured")
        if not soffice["available"]:
            blockers.append("Stable LibreOffice is unavailable")
        if request.action == "run":
            if prepared is not None and not checks["licensed_content_uploaded"]:
                blockers.append("Prepared Dataset contains redacted content; run licensed prepare")
            if not langfuse.capture_content:
                blockers.append("captureContent must be enabled for a licensed benchmark run")
        if request.action == "prepare" and request.allow_licensed_content:
            if not langfuse.capture_content:
                blockers.append("captureContent must be enabled before licensed prepare")
            if not checks["adobe_credentials"]:
                warnings.append("Adobe PDF Services credentials are not available; OCB PDF assets may block prepare")
        if request.profile == "office-release":
            warnings.append("Release evaluates a large licensed Dataset and requires explicit token confirmation")
        return PreflightResult(
            ready=not blockers,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            checks=checks,
            estimate=estimate,
        )

    def command(self, request: EvaluationRequest) -> list[str]:
        self._validate(request)
        command = ["benchmark", request.action, "--profile", request.profile]
        if request.action == "prepare" and request.profile != "ci":
            readiness = self.preflight(request)
            soffice = readiness.checks.get("libreoffice", {})
            if soffice.get("path") and soffice.get("version"):
                command.extend(["--soffice", soffice["path"], "--soffice-version", soffice["version"]])
            if request.allow_licensed_content:
                command.append("--allow-licensed-content")
            return command
        if request.action == "run" and request.profile != "ci":
            for model_preset in request.model_presets:
                command.extend(["--model-preset", model_preset])
            for benchmark, option in (
                ("ocb", "--ocb-sample"),
            ):
                command.extend([option, str(request.benchmark_samples[benchmark])])
            for benchmark in request.benchmarks:
                command.extend(["--benchmark", benchmark])
            for skill in request.skills:
                command.extend(["--skill", skill])
        return command


class EvaluationCatalog:
    def __init__(self) -> None:
        self._adapters: dict[str, EvaluationSuiteAdapter] = {"office": OfficeEvaluationAdapter()}

    def adapter(self, suite_id: str) -> EvaluationSuiteAdapter:
        try:
            return self._adapters[suite_id]
        except KeyError as exc:
            raise ValueError(f"unknown evaluation suite: {suite_id}") from exc

    def payload(self, available_skills: list[dict[str, Any]]) -> dict[str, Any]:
        return {"schema_version": 1, "suites": [adapter.catalog(available_skills) for adapter in self._adapters.values()]}

    def preflight(self, request: EvaluationRequest) -> PreflightResult:
        return self.adapter(request.suite_id).preflight(request)

    def command(self, request: EvaluationRequest) -> list[str]:
        return self.adapter(request.suite_id).command(request)
