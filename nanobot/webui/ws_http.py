"""HTTP API handler extracted from WebSocketChannel.

Handles all non-WebSocket HTTP routes: bootstrap, sessions, settings,
media, commands, sidebar state, static file serving, and token management.

Also houses shared HTTP utility functions used by both this module and
``websocket.py`` to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.command.builtin import builtin_command_palette
from nanobot.evaluations.catalog import (
    TERMINAL_JOB_STATUSES,
    EvaluationCatalog,
    EvaluationRequest,
)
from nanobot.evaluations.jobs import EvaluationJobService
from nanobot.evaluations.results import LangfuseEvaluationReader
from nanobot.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanobot.webui.file_preview import (
    WebUIFilePreviewError,
    file_preview_payload,
    session_plan_preview_files,
)
from nanobot.webui.gateway_tokens import GatewayTokenStore, token_response_payload
from nanobot.webui.http_utils import (
    case_insensitive_header as _case_insensitive_header,
)
from nanobot.webui.http_utils import (
    host_for_url as _host_for_url,
)
from nanobot.webui.http_utils import (
    http_error as _http_error,
)
from nanobot.webui.http_utils import (
    http_json_response as _http_json_response,
)
from nanobot.webui.http_utils import (
    http_response as _http_response,
)
from nanobot.webui.http_utils import (
    is_localhost as _is_localhost,
)
from nanobot.webui.http_utils import (
    issue_route_secret_matches as _issue_route_secret_matches,
)
from nanobot.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from nanobot.webui.http_utils import (
    parse_query as _parse_query,
)
from nanobot.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from nanobot.webui.http_utils import (
    query_first as _query_first,
)
from nanobot.webui.http_utils import (
    safe_host_header as _safe_host_header,
)
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.session_automations import session_automations_payload
from nanobot.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from nanobot.webui.skills_api import webui_skill_detail_payload, webui_skills_payload
from nanobot.webui.thread_disk import delete_webui_thread
from nanobot.webui.transcript import build_webui_thread_response
from nanobot.webui.workspaces import (
    WebUIWorkspaceController,
    WorkspaceDirectoryError,
    browse_workspace_directories,
)

_ACTIVE_EVALUATION_STATUSES = frozenset({
    "queued",
    "preflight",
    "preparing",
    "estimating",
    "running",
    "remote_scoring",
})
_EVALUATION_REMOTE_DELETE_TIMEOUT_SECONDS = 8.0


def _evaluation_model_runs(
    job: dict[str, Any],
    linked_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    action = str(job.get("action") or request.get("action") or "")
    if action != "run":
        return []
    model_presets = [str(value) for value in request.get("model_presets") or [] if value]
    if not model_presets:
        return []
    cases = job.get("cases") if isinstance(job.get("cases"), list) else []
    total_cases = int(job.get("total_cases") or 0)
    per_model_total = total_cases // len(model_presets) if total_cases else 0
    active = str(job.get("status") or "") in _ACTIVE_EVALUATION_STATUSES
    rows: list[dict[str, Any]] = []
    for model_preset in model_presets:
        model_cases = [case for case in cases if case.get("model_preset") == model_preset]
        completed = sum(1 for case in model_cases if case.get("status") == "completed")
        failed = sum(1 for case in model_cases if case.get("status") == "failed")
        running = sum(1 for case in model_cases if case.get("status") == "running")
        terminal = completed + failed
        linked = next(
            (row for row in linked_runs if row.get("model_preset") == model_preset),
            None,
        )
        if terminal >= per_model_total > 0:
            status = "failed" if failed else "completed"
        elif running or terminal:
            status = "running" if active else str(job.get("status") or "interrupted")
        else:
            status = "queued" if active else str(job.get("status") or "interrupted")
        rows.append({
            "job_id": job.get("job_id"),
            "model_preset": model_preset,
            "status": status,
            "total_cases": per_model_total,
            "completed_cases": terminal,
            "successful_cases": completed,
            "failed_cases": failed,
            "remaining_cases": max(0, per_model_total - terminal),
            "dataset_run_id": linked.get("dataset_run_id") if linked else None,
            "langfuse_url": linked.get("langfuse_url") if linked else None,
            "aggregate_scores": dict(linked.get("aggregate_scores") or {}) if linked else {},
            "usage": linked.get("usage") if linked else None,
            "metrics": linked.get("metrics") if linked else None,
            "review_status": linked.get("review_status") if linked else "pending",
        })
    return rows

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.cron.service import CronService
    from nanobot.evaluations.skill_evolution import SkillEvolutionService
    from nanobot.session.manager import SessionManager


def _decode_api_key(raw_key: str) -> str | None:
    from urllib.parse import unquote

    key = unquote(raw_key)
    _api_key_re = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
    if _api_key_re.match(key) is None:
        return None
    return key


def _default_model_name_from_config() -> str | None:
    try:
        from nanobot.config.loader import load_config
        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str:
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config() or ""


# ---------------------------------------------------------------------------
# GatewayHTTPHandler
# ---------------------------------------------------------------------------


class GatewayHTTPHandler:
    """Handles all HTTP routes served alongside the WebSocket endpoint.

    Routes HTTP requests and delegates stateful work to explicit gateway
    services owned by the composition layer.
    """

    def __init__(
        self,
        *,
        config: Any,  # WebSocketConfig
        session_manager: SessionManager | None,
        static_dist_path: Path | None,
        runtime_model_name: Callable[[], str | None] | None,
        runtime_surface: str,
        runtime_capabilities_overrides: dict[str, Any] | None,
        bus: MessageBus,
        tokens: GatewayTokenStore,
        media: WebUIMediaGateway,
        workspaces: WebUIWorkspaceController,
        skills_workspace_path: Path,
        disabled_skills: set[str] | None = None,
        cron_service: CronService | None = None,
        evaluation_catalog: EvaluationCatalog,
        evaluations: EvaluationJobService,
        evaluation_results: LangfuseEvaluationReader,
        skill_evolution: SkillEvolutionService,
        log: Any = logger,
    ) -> None:
        self.config = config
        self.session_manager = session_manager
        self.static_dist_path = static_dist_path
        self.runtime_model_name = runtime_model_name
        self.bus = bus
        self.tokens = tokens
        self.media = media
        self.workspaces = workspaces
        self.skills_workspace_path = skills_workspace_path
        self.disabled_skills = disabled_skills or set()
        self.cron_service = cron_service
        self.evaluation_catalog = evaluation_catalog
        self.evaluations = evaluations
        self.evaluation_results = evaluation_results
        self.skill_evolution = skill_evolution
        self._log = log
        self._runtime_surface = runtime_surface
        self._evaluation_delete_tasks: set[asyncio.Task[None]] = set()

        from nanobot.webui.settings_api import runtime_capabilities as _rc
        from nanobot.webui.settings_routes import WebUISettingsRouter

        self._capabilities = _rc(runtime_surface, runtime_capabilities_overrides or {})
        self.settings_routes = WebUISettingsRouter(
            bus=bus,
            logger=self._log,
            check_api_token=self.check_api_token,
            parse_query=_parse_query,
            json_response=_http_json_response,
            error_response=_http_error,
            runtime_surface=runtime_surface,
            runtime_capabilities=self._capabilities,
        )

    def workspace_controls_available(self, connection: Any) -> bool:
        return self._runtime_surface == "native" or _is_localhost(connection)

    # -- Token management ---------------------------------------------------

    def check_api_token(self, request: WsRequest) -> bool:
        return self.tokens.check_api_token(request)

    # -- Main dispatch ------------------------------------------------------

    async def dispatch(self, connection: Any, request: WsRequest) -> Any | None:
        """Route an HTTP request. Returns Response or None."""
        got, _ = _parse_request_path(request.path)

        # Token issue endpoint
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue(connection, request)

        # Bootstrap
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # Settings routes (delegated)
        response = await self.settings_routes.dispatch(request, got)
        if response is not None:
            return response

        # Session routes
        response = await self._dispatch_trace_route(request, got)
        if response is not None:
            return response

        response = self._dispatch_session_routes(request, got)
        if response is not None:
            return response

        # Media routes
        response = self._dispatch_media_routes(request, got)
        if response is not None:
            return response

        # Evaluation routes can query Langfuse and therefore run off-loop.
        response = await self._dispatch_evaluation_routes(request, got)
        if response is not None:
            return response

        response = await self._dispatch_skill_evolution_routes(request, got)
        if response is not None:
            return response

        # Misc routes
        response = self._dispatch_misc_routes(connection, request, got)
        if response is not None:
            return response

        # API 404 (never serve SPA for /api/ routes)
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # Static SPA serving
        if self.static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    async def _dispatch_evaluation_routes(
        self,
        request: WsRequest,
        got: str,
    ) -> Response | None:
        if not got.startswith("/api/evaluations"):
            return None
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if got == "/api/evaluations/catalog":
            from nanobot.config.loader import load_config

            skills = webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=set(load_config().agents.defaults.disabled_skills),
            ).get("skills", [])
            return _http_json_response(self.evaluation_catalog.payload(skills))
        if got == "/api/evaluations/readiness":
            try:
                evaluation_request = self._evaluation_request_from_query(request.path)
                payload = await asyncio.to_thread(
                    self.evaluation_catalog.preflight,
                    evaluation_request,
                )
            except ValueError as exc:
                return _http_json_response(
                    {"ready": False, "blockers": [str(exc)], "warnings": [], "checks": {}, "estimate": {}},
                    status=400,
                )
            return _http_json_response(payload.payload())
        if got == "/api/evaluations/runs":
            jobs = self.evaluations.list()
            known_run_ids = {
                str(run_id)
                for job in jobs
                for run_id in job.get("dataset_run_ids", [])
                if run_id
            }
            known = await asyncio.to_thread(
                self.evaluation_results.list_runs_by_ids,
                known_run_ids,
            )
            remote = (
                known
                if known_run_ids
                else await asyncio.to_thread(self.evaluation_results.list_runs)
            )
            remote_by_id = {
                str(row.get("dataset_run_id")): row
                for row in [*(remote.get("runs", [])), *(known.get("runs", []))]
                if row.get("dataset_run_id")
            }
            local = []
            for job in jobs:
                linked_ids = {str(run_id) for run_id in job.get("dataset_run_ids", [])}
                resume_token = str(job.get("resume_token") or job.get("job_id") or "")
                stable_suffix = f"-job-{resume_token}" if resume_token else ""
                linked_runs = [
                    linked
                    for run_id, linked in remote_by_id.items()
                    if run_id in linked_ids
                    or (stable_suffix and str(linked.get("name") or "").endswith(stable_suffix))
                ]
                if str(job.get("status") or "") in _ACTIVE_EVALUATION_STATUSES:
                    for linked in linked_runs:
                        linked["status"] = "running"
                enriched = {**job, "source": "mybot"}
                linked_scores: dict[str, Any] = {}
                linked_usage: dict[str, int] = {}
                linked_metrics: dict[str, float | int] = {}
                for linked in linked_runs:
                    prefix = "/".join(filter(None, [
                        str(linked.get("benchmark") or ""),
                        str(linked.get("skill") or ""),
                    ]))
                    for name, value in (linked.get("aggregate_scores") or {}).items():
                        linked_scores[f"{prefix}/{name}" if prefix else str(name)] = value
                    for name, value in (linked.get("usage") or {}).items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            linked_usage[name] = linked_usage.get(name, 0) + int(value)
                    for name, value in (linked.get("metrics") or {}).items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            linked_metrics[name] = linked_metrics.get(name, 0) + value
                if not enriched.get("aggregate_scores") and linked_scores:
                    enriched["aggregate_scores"] = linked_scores
                if linked_usage:
                    enriched["usage"] = linked_usage
                if linked_metrics:
                    enriched["metrics"] = linked_metrics
                enriched["model_runs"] = _evaluation_model_runs(enriched, linked_runs)
                local.append(enriched)
            return _http_json_response({
                "jobs": local,
                "langfuse": {
                    **remote,
                    "available": bool(remote_by_id) or bool(remote.get("available")),
                    "runs": list(remote_by_id.values()),
                },
            })
        delete_match = re.match(r"^/api/evaluations/runs/([A-Za-z0-9_-]+)/delete$", got)
        if delete_match:
            run_id = delete_match.group(1)
            local = self.evaluations.get(run_id)
            if local is not None and str(local.get("status") or "") not in TERMINAL_JOB_STATUSES:
                return _http_error(409, "only terminal evaluation jobs can be deleted")
            task = asyncio.create_task(
                self._delete_evaluation_history(run_id, local),
                name=f"evaluation-delete-{run_id}",
            )
            self._evaluation_delete_tasks.add(task)
            task.add_done_callback(self._evaluation_delete_tasks.discard)
            self._log.info("Evaluation history deletion scheduled: {}", run_id)
            return _http_json_response({"deleted": True, "scheduled": True})
        match = re.match(r"^/api/evaluations/runs/([A-Za-z0-9_-]+)(/cases)?$", got)
        if match:
            run_id = match.group(1)
            local = self.evaluations.get(run_id)
            if local is not None:
                if match.group(2):
                    remote = await asyncio.to_thread(self.evaluation_results.list_runs)
                    remote_by_id = {
                        str(row.get("dataset_run_id")): row
                        for row in remote.get("runs", [])
                        if row.get("dataset_run_id")
                    }
                    remote_cases: dict[tuple[str, str, str, str], dict[str, Any]] = {}
                    linked_ids = {str(item) for item in local.get("dataset_run_ids", [])}
                    resume_token = str(local.get("resume_token") or local.get("job_id") or "")
                    stable_suffix = f"-job-{resume_token}" if resume_token else ""
                    linked_runs = [
                        linked
                        for dataset_run_id, linked in remote_by_id.items()
                        if dataset_run_id in linked_ids
                        or (
                            stable_suffix
                            and str(linked.get("name") or "").endswith(stable_suffix)
                        )
                    ]
                    for linked in linked_runs:
                        benchmark = str(linked.get("benchmark") or "")
                        skill = str(linked.get("skill") or "")
                        model_preset = str(linked.get("model_preset") or "")
                        for case in linked.get("cases", []):
                            key = (
                                benchmark,
                                skill,
                                str(case.get("model_preset") or model_preset),
                                str(case.get("case_id") or ""),
                            )
                            remote_cases[key] = case
                    cases = []
                    for case in self.evaluations.cases(run_id):
                        key = (
                            str(case.get("benchmark") or ""),
                            str(case.get("skill") or ""),
                            str(case.get("model_preset") or ""),
                            str(case.get("case_id") or ""),
                        )
                        linked_case = remote_cases.get(key)
                        if (
                            linked_case
                            and str(local.get("status") or "") in _ACTIVE_EVALUATION_STATUSES
                            and linked_case.get("status") == "failed"
                        ):
                            linked_case = {
                                **linked_case,
                                "status": "pending",
                                "score_status": "pending",
                                "scores": {},
                            }
                        cases.append({**case, **linked_case} if linked_case else case)
                    return _http_json_response({"cases": cases})
                return _http_json_response(
                    local
                )
            remote = await asyncio.to_thread(self.evaluation_results.list_runs)
            row = next(
                (
                    item
                    for item in remote.get("runs", [])
                    if item.get("dataset_run_id") == run_id
                ),
                None,
            )
            if row is None:
                return _http_error(404, "evaluation run not found")
            if match.group(2):
                return _http_json_response({"cases": row.get("cases", [])})
            return _http_json_response(row)
        return _http_error(404, "evaluation route not found")

    async def _dispatch_skill_evolution_routes(
        self,
        request: WsRequest,
        got: str,
    ) -> Response | None:
        if not got.startswith("/api/skill-evolution"):
            return None
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        try:
            if got == "/api/skill-evolution/tasks":
                return _http_json_response({"tasks": self.skill_evolution.list()})
            if got == "/api/skill-evolution/bad-cases":
                run_id = _query_first(query, "run_id") or ""
                threshold = float(_query_first(query, "threshold") or "0.6")
                return _http_json_response(
                    await asyncio.to_thread(self.skill_evolution.bad_cases, run_id, threshold)
                )
            if got in {"/api/skill-evolution/analyze", "/api/skill-evolution/generate"}:
                if request.method != "POST":
                    return _http_error(405, "POST required")
                run_id = _query_first(query, "run_id") or ""
                threshold = float(_query_first(query, "threshold") or "0.6")
                source_model_preset = _query_first(query, "source_model_preset") or ""
                optimizer_preset = _query_first(query, "optimizer_preset") or ""
                raw_cases = _query_first(query, "case_ids") or "[]"
                case_ids = json.loads(raw_cases)
                if not isinstance(case_ids, list):
                    raise ValueError("case_ids must be an array")
                task = self.skill_evolution.start_analysis(
                    run_id,
                    [str(value) for value in case_ids],
                    source_model_preset,
                    optimizer_preset,
                    threshold,
                )
                return _http_json_response(task)
            activity_match = re.match(
                r"^/api/skill-evolution/tasks/([A-Za-z0-9_-]+)/activities$",
                got,
            )
            if activity_match:
                after = int(_query_first(query, "after") or "0")
                return _http_json_response(
                    self.skill_evolution.activities(activity_match.group(1), after)
                )
            task_match = re.match(r"^/api/skill-evolution/tasks/([A-Za-z0-9_-]+)$", got)
            if task_match:
                task = self.skill_evolution.get(task_match.group(1))
                return _http_json_response(task) if task else _http_error(404, "task not found")
            action_match = re.match(
                r"^/api/skill-evolution/tasks/([A-Za-z0-9_-]+)/(evolve|reanalyze|revise|cancel|test|apply|switch-back)$",
                got,
            )
            if action_match:
                if request.method != "POST":
                    return _http_error(405, "POST required")
                task_id, action = action_match.groups()
                revision_id = _query_first(query, "revision_id") or "r1"
                raw_findings = _query_first(query, "finding_ids") or "[]"
                finding_ids = json.loads(raw_findings)
                if not isinstance(finding_ids, list):
                    raise ValueError("finding_ids must be an array")
                if action == "evolve":
                    task = self.skill_evolution.start_evolution(
                        task_id,
                        [str(value) for value in finding_ids],
                        analysis_id=_query_first(query, "analysis_id"),
                        analysis_digest=_query_first(query, "analysis_digest"),
                    )
                elif action == "reanalyze":
                    task = self.skill_evolution.reanalyze(task_id)
                elif action == "revise":
                    task = self.skill_evolution.revise(
                        task_id,
                        [str(value) for value in finding_ids] or None,
                    )
                elif action == "cancel":
                    task = self.skill_evolution.cancel(task_id)
                elif action == "test":
                    task = self.skill_evolution.start_test(task_id, revision_id)
                elif action == "apply":
                    task = await asyncio.to_thread(
                        self.skill_evolution.apply, task_id, revision_id
                    )
                else:
                    task = await asyncio.to_thread(self.skill_evolution.switch_back, task_id)
                if action in {"apply", "switch-back"}:
                    from nanobot.agent.skills import request_skills_reload

                    task["runtime_refresh"] = await request_skills_reload(self.bus)
                return _http_json_response(task)
        except KeyError:
            return _http_error(404, "Skill evolution resource not found")
        except (ValueError, json.JSONDecodeError) as exc:
            return _http_error(400, str(exc))
        except Exception as exc:
            self._log.exception("Skill evolution route failed: {}", got)
            return _http_error(500, str(exc)[:300])
        return _http_error(404, "Skill evolution route not found")

    async def _delete_evaluation_history(
        self,
        run_id: str,
        local: dict[str, Any] | None,
    ) -> None:
        linked_ids = {str(item) for item in (local or {}).get("dataset_run_ids", [])}
        resume_token = str(
            (local or {}).get("resume_token") or (local or {}).get("job_id") or ""
        )
        stable_suffix = f"-job-{resume_token}" if resume_token else ""

        local_deleted = local is None
        if local is not None:
            try:
                local_deleted = await asyncio.to_thread(self.evaluations.delete, run_id)
            except Exception:
                self._log.exception("Local evaluation history deletion failed: {}", run_id)

        if local is None:
            linked_ids.add(run_id)
        try:
            if stable_suffix:
                remote = await asyncio.to_thread(self.evaluation_results.list_runs)
                linked_ids.update(
                    str(item.get("dataset_run_id"))
                    for item in remote.get("runs", [])
                    if item.get("dataset_run_id")
                    and str(item.get("name") or "").endswith(stable_suffix)
                )
            remote_result = await asyncio.to_thread(
                self.evaluation_results.delete_runs,
                sorted(linked_ids),
            )
            self._log.info(
                "Evaluation history deletion finished: {} local_deleted={} "
                "remote_deleted={} remote_missing={}",
                run_id,
                local_deleted,
                int(remote_result.get("deleted") or 0),
                len(remote_result.get("missing", [])),
            )
        except Exception:
            self._log.exception(
                "Langfuse evaluation history deletion failed in background: {}",
                run_id,
            )

    @staticmethod
    def _evaluation_request_from_query(path: str) -> EvaluationRequest:
        query = _parse_query(path)

        def csv(name: str) -> list[str] | None:
            raw = _query_first(query, name)
            return raw.split(",") if raw else None

        raw_samples = _query_first(query, "benchmark_samples")
        payload: dict[str, Any] = {
            "suite_id": _query_first(query, "suite_id") or "office",
            "profile": _query_first(query, "profile") or "office-smoke",
            "action": _query_first(query, "action") or "run",
            "allow_licensed_content": _query_first(query, "allow_licensed_content") == "true",
        }
        for query_name, payload_name in (
            ("benchmarks", "benchmarks"),
            ("skills", "skills"),
            ("model_presets", "model_presets"),
            ("runtime_profiles", "runtime_profiles"),
        ):
            values = csv(query_name)
            if values is not None:
                payload[payload_name] = values
        if raw_samples:
            try:
                payload["benchmark_samples"] = json.loads(raw_samples)
            except json.JSONDecodeError as exc:
                raise ValueError("benchmark_samples must be valid JSON") from exc
        return EvaluationRequest.from_payload(payload)

    # -- Token issue --------------------------------------------------------

    def _handle_token_issue(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self._log.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        if not self.tokens.can_issue():
            self._log.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self.tokens.issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = self.tokens.issue_token(self.config.token_ttl_s)
        return _http_json_response(token_response_payload(token_value, self.config.token_ttl_s))

    # -- Bootstrap ----------------------------------------------------------

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not _is_localhost(connection):
            return _http_error(403, "bootstrap is localhost-only")

        if not self.tokens.can_issue(include_api_token=True):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = self.tokens.issue_token(self.config.token_ttl_s, api_token=True)

        ws_url = self._bootstrap_ws_url(request)
        expected_path = _normalize_config_path(self.config.path)
        return _http_json_response(
            {
                "token": token,
                "ws_path": expected_path,
                "ws_url": ws_url,
                "expires_in": self.config.token_ttl_s,
                "model_name": _resolve_bootstrap_model_name(self.runtime_model_name),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._capabilities,
            }
        )

    def _bootstrap_ws_url(self, request: Any) -> str:
        headers = getattr(request, "headers", {}) or {}
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)
        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certfile.strip())
        scheme = "wss" if secure else "ws"
        expected_path = _normalize_config_path(self.config.path)
        return f"{scheme}://{host}{expected_path}"

    # -- Session routes -----------------------------------------------------

    async def _dispatch_trace_route(self, request: WsRequest, got: str) -> Response | None:
        match = re.match(r"^/api/sessions/([^/]+)/trace$", got)
        if match is None:
            return None
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(match.group(1))
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        turn_id = _query_first(_parse_query(request.path), "turn_id")
        if not isinstance(turn_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", turn_id) is None:
            return _http_error(400, "invalid turn id")
        scope = self.workspaces.scope_for_session_key(decoded_key)
        try:
            from nanobot.runtime.trace_reader import read_turn_trace

            payload = await asyncio.to_thread(
                read_turn_trace,
                scope.project_path,
                decoded_key,
                turn_id,
            )
        except Exception as exc:
            self._log.warning("trace read failed for {}: {}", decoded_key, exc)
            return _http_error(502, "trace source unavailable")
        return _http_json_response(payload)

    def _dispatch_session_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/file-preview$", got)
        if m:
            return self._handle_file_preview(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/automations$", got)
        if m:
            return self._handle_session_automations(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        return None

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        sessions = self.session_manager.list_sessions()
        from nanobot.session.webui_turns import websocket_turn_wall_started_at

        cleaned = []
        for s in sessions:
            key = s.get("key")
            if not (isinstance(key, str) and key.startswith("websocket:")):
                continue
            row = {k: v for k, v in s.items() if k != "path"}
            chat_id = key.split(":", 1)[1]
            started_at = websocket_turn_wall_started_at(chat_id)
            if started_at is not None:
                row["run_started_at"] = started_at
            scope = self.workspaces.scope_for_session_key(key)
            row["workspace_scope"] = scope.payload()
            cleaned.append(row)
        return _http_json_response({"sessions": cleaned})

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self.session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        self.media.augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        scope = self.workspaces.scope_for_session_key(decoded_key)
        session_messages: list[dict[str, Any]] | None = None
        if self.session_manager is not None:
            session_data = self.session_manager.read_session_file(decoded_key)
            raw_messages = session_data.get("messages") if isinstance(session_data, dict) else None
            if isinstance(raw_messages, list):
                session_messages = [m for m in raw_messages if isinstance(m, dict)]
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self.media.augment_transcript_media,
            augment_assistant_media=self.media.augment_transcript_media,
            augment_assistant_text=lambda text: self.media.rewrite_local_markdown_images(
                text,
                workspace_path=scope.project_path,
            ),
            session_messages=session_messages,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        data["workspace_scope"] = scope.payload()
        return _http_json_response(data)

    def _handle_file_preview(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        path = _query_first(_parse_query(request.path), "path")
        session_data = (
            self.session_manager.read_session_file(decoded_key)
            if self.session_manager is not None
            else None
        )
        try:
            payload = file_preview_payload(
                path,
                scope=self.workspaces.scope_for_session_key(decoded_key),
                trusted_files=session_plan_preview_files(session_data),
            )
        except WebUIFilePreviewError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_session_automations(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        return _http_json_response(
            session_automations_payload(self.cron_service, decoded_key)
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        deleted = self.session_manager.delete_session(decoded_key)
        delete_webui_thread(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    # -- Media routes -------------------------------------------------------

    def _dispatch_media_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2), request)
        return None

    def _handle_media_fetch(
        self, sig: str, payload: str, request: WsRequest | None = None
    ) -> Response:
        return self.media.serve_signed_media(
            sig,
            payload,
            request=request,
        )

    # -- Misc routes --------------------------------------------------------

    def _dispatch_misc_routes(
        self, connection: Any, request: WsRequest, got: str
    ) -> Response | None:
        if got == "/api/sessions":
            return self._handle_sessions_list(request)
        if got == "/api/commands":
            return self._handle_commands(request)
        if got == "/api/workspaces":
            return self._handle_workspaces(connection, request)
        if got == "/api/workspaces/directories":
            return self._handle_workspace_directories(connection, request)
        if got == "/api/webui/skills":
            return self._handle_webui_skills(request)
        m = re.match(r"^/api/webui/skills/([^/]+)$", got)
        if m:
            return self._handle_webui_skill_detail(request, m.group(1))
        if got == "/api/webui/sidebar-state":
            return self._handle_webui_sidebar_state(request)
        if got == "/api/webui/sidebar-state/update":
            return self._handle_webui_sidebar_state_update(request)
        return None

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_workspaces(self, connection: Any, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            self.workspaces.payload(
                controls_available=self.workspace_controls_available(connection)
            )
        )

    def _handle_workspace_directories(self, connection: Any, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if not self.workspace_controls_available(connection):
            return _http_error(403, "workspace controls are localhost-only")
        path = _query_first(_parse_query(request.path), "path")
        try:
            payload = browse_workspace_directories(
                path,
                default_workspace=self.workspaces.default_scope().project_path,
            )
        except WorkspaceDirectoryError as e:
            return _http_error(e.status, str(e))
        return _http_json_response(payload)

    def _handle_webui_skills(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        from nanobot.config.loader import load_config

        return _http_json_response(
            webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=set(load_config().agents.defaults.disabled_skills),
            )
        )

    def _handle_webui_skill_detail(self, request: WsRequest, raw_name: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        from nanobot.config.loader import load_config

        name = unquote(raw_name)
        if not name or "/" in name or "\\" in name:
            return _http_error(400, "invalid skill name")
        payload = webui_skill_detail_payload(
            self.skills_workspace_path,
            name,
            disabled_skills=set(load_config().agents.defaults.disabled_skills),
        )
        if payload is None:
            return _http_error(404, "skill not found")
        return _http_json_response(payload)

    def _handle_webui_sidebar_state(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(read_webui_sidebar_state())

    def _handle_webui_sidebar_state_update(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_state = _query_first(query, "state")
        if raw_state is None:
            return _http_error(400, "missing state")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError:
            return _http_error(400, "state must be JSON")
        if not isinstance(decoded, dict):
            return _http_error(400, "state must be an object")
        try:
            state = write_webui_sidebar_state(decoded)
        except ValueError as e:
            return _http_error(400, str(e))
        except OSError:
            self._log.exception("failed to write webui sidebar state")
            return _http_error(500, "failed to write sidebar state")
        return _http_json_response(state)

    # -- Static file serving ------------------------------------------------

    def _serve_static(self, request_path: str) -> Response | None:
        assert self.static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self.static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self.static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            index = self.static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self._log.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )

def _is_websocket_channel_session_key(key: str) -> bool:
    return key.startswith("websocket:")
