"""Redacted Langfuse projection for the Mybot evaluation center."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock, Thread
from time import monotonic
from typing import Any

from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.runtime.langfuse import LangfuseRuntime


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _score_value(score: Any) -> float | bool | str | None:
    value = getattr(score, "value", None)
    if value is None and isinstance(score, dict):
        value = score.get("value")
    if isinstance(value, (float, int, bool, str)):
        return value
    return None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _usage_number(details: Any, *names: str) -> int:
    for name in names:
        value = _number(_field(details, name))
        if value is not None:
            return max(0, int(value))
    return 0


def _trace_metrics(runtime: LangfuseRuntime, trace_id: str) -> dict[str, Any]:
    """Return usage and timing only; never project observation input/output."""
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    generation_count = 0
    latency_seconds = 0.0
    ttft_seconds = 0.0
    cursor: str | None = None
    while True:
        page = runtime.client.api.observations.get_many(
            trace_id=trace_id,
            type="GENERATION",
            fields="core,usage,metrics",
            limit=1000,
            cursor=cursor,
        )
        for observation in page.data:
            generation_count += 1
            usage_details = _field(observation, "usage_details")
            input_tokens = _usage_number(usage_details, "input", "input_tokens", "prompt", "prompt_tokens")
            output_tokens = _usage_number(usage_details, "output", "output_tokens", "completion", "completion_tokens")
            total_tokens = _usage_number(usage_details, "total", "total_tokens") or input_tokens + output_tokens
            usage_totals["input_tokens"] += input_tokens
            usage_totals["output_tokens"] += output_tokens
            usage_totals["total_tokens"] += total_tokens
            usage_totals["cached_input_tokens"] += _usage_number(
                usage_details,
                "cache_read_input_tokens",
                "cached_input_tokens",
                "cache_read_tokens",
            )
            usage_totals["cache_creation_input_tokens"] += _usage_number(
                usage_details,
                "cache_creation_input_tokens",
                "cache_creation_tokens",
            )
            latency = _number(_field(observation, "latency"))
            ttft = _number(_field(observation, "time_to_first_token"))
            if latency is not None:
                latency_seconds += max(0.0, float(latency))
            if ttft is not None:
                ttft_seconds += max(0.0, float(ttft))
        cursor = _field(_field(page, "meta"), "cursor")
        if not cursor:
            break

    has_usage = usage_totals["total_tokens"] > 0 or any(
        usage_totals[name] > 0 for name in ("input_tokens", "output_tokens")
    )
    return {
        "usage": usage_totals if has_usage else None,
        "metrics": {
            "generation_count": generation_count,
            "latency_seconds": round(latency_seconds, 3),
            "ttft_seconds": round(ttft_seconds, 3),
        },
    }


def _add_usage(target: dict[str, int], usage: dict[str, Any] | None) -> None:
    if not isinstance(usage, dict):
        return
    for name in target:
        value = _number(usage.get(name))
        if value is not None:
            target[name] += max(0, int(value))


class LangfuseEvaluationReader:
    """Read only Mybot Dataset Runs; never returns trace input/output content."""

    def __init__(self, *, cache_ttl_seconds: float = 15.0) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = Lock()
        self._refreshing: set[int] = set()

    def list_runs(self, *, limit: int = 50) -> dict[str, Any]:
        with self._cache_lock:
            cached = self._cache.get(limit)
            now = monotonic()
            if cached is not None and now - cached[0] < self._cache_ttl_seconds:
                return deepcopy(cached[1])
            should_refresh = limit not in self._refreshing
            if should_refresh:
                self._refreshing.add(limit)
            payload = (
                deepcopy(cached[1])
                if cached is not None
                else {
                    "available": False,
                    "error": "Langfuse history is loading",
                    "runs": [],
                }
            )
            payload["stale"] = cached is not None
            payload["refreshing"] = True

        if should_refresh:
            Thread(
                target=self._refresh_cache,
                args=(limit,),
                name="nanobot-langfuse-evaluations",
                daemon=True,
            ).start()
        return payload

    def _refresh_cache(self, limit: int) -> None:
        try:
            result = self._list_runs_uncached(limit=limit)
        except Exception as exc:
            result = {"available": False, "error": str(exc)[:300], "runs": []}
        with self._cache_lock:
            cached = self._cache.get(limit)
            if result.get("available") or cached is None:
                payload = deepcopy(result)
            else:
                payload = deepcopy(cached[1])
                payload["stale"] = True
                payload["refresh_error"] = result.get("error")
            payload["refreshing"] = False
            self._cache[limit] = (monotonic(), payload)
            self._refreshing.discard(limit)

    def _list_runs_uncached(self, *, limit: int) -> dict[str, Any]:
        config = resolve_config_env_vars(load_config()).observability.langfuse
        if not config.enabled or not config.resolved_public_key() or not config.resolved_secret_key():
            return {"available": False, "error": "Langfuse is not configured", "runs": []}
        runtime: LangfuseRuntime | None = None
        try:
            runtime = LangfuseRuntime(config)
            if not runtime.client.auth_check():
                return {"available": False, "error": "Langfuse authentication failed", "runs": []}
            project_id = runtime.client._get_project_id()
            datasets_page = runtime.client.api.datasets.list(page=1, limit=100)
            datasets = [
                dataset for dataset in datasets_page.data
                if str(getattr(dataset, "name", "")).startswith("mybot-")
            ]
            rows: list[dict[str, Any]] = []
            for dataset in datasets:
                if len(rows) >= limit:
                    break
                dataset_name = str(dataset.name)
                response = runtime.client.get_dataset_runs(
                    dataset_name=dataset_name,
                    page=1,
                    limit=min(20, limit - len(rows)),
                )
                for run in response.data:
                    metadata = dict(getattr(run, "metadata", None) or {})
                    run_id = str(run.id)
                    score_payload: dict[str, list[float | bool | str | None]] = {}
                    completed_items = 0
                    failed_items = 0
                    item_rows: list[dict[str, Any]] = []
                    run_usage = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "cached_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                    run_generation_count = 0
                    run_latency_seconds = 0.0
                    run_ttft_seconds = 0.0
                    cases_with_usage = 0
                    try:
                        experiment = runtime.client.api.experiments.list(
                            from_start_time=datetime(2020, 1, 1, tzinfo=timezone.utc),
                            id=run_id,
                            limit=2,
                        ).data[0]
                        cursor: str | None = None
                        while True:
                            page = runtime.client.api.experiments.list_items(
                                from_start_time=experiment.start_time,
                                experiment_id=run_id,
                                limit=50,
                                score_limit=50,
                                cursor=cursor,
                            )
                            for item in page.data:
                                level = str(getattr(item, "level", "")).upper()
                                ended = getattr(item, "end_time", None) is not None
                                completed_items += int(ended)
                                failed_items += int(level.endswith("ERROR"))
                                scores = getattr(item, "scores", None) or []
                                item_score_map: dict[str, Any] = {}
                                for score in scores:
                                    name = str(getattr(score, "name", "score"))
                                    value = _score_value(score)
                                    score_payload.setdefault(name, []).append(value)
                                    item_score_map[name] = value
                                trace_id = getattr(item, "trace_id", None)
                                trace_metrics: dict[str, Any] = {}
                                if trace_id:
                                    try:
                                        trace_metrics = _trace_metrics(runtime, str(trace_id))
                                    except Exception:
                                        trace_metrics = {}
                                item_usage = trace_metrics.get("usage")
                                item_performance = trace_metrics.get("metrics")
                                _add_usage(run_usage, item_usage)
                                if item_usage is not None:
                                    cases_with_usage += 1
                                if isinstance(item_performance, dict):
                                    run_generation_count += int(item_performance.get("generation_count") or 0)
                                    run_latency_seconds += float(item_performance.get("latency_seconds") or 0)
                                    run_ttft_seconds += float(item_performance.get("ttft_seconds") or 0)
                                item_rows.append({
                                    "case_id": str(getattr(item, "experiment_item_id", None) or getattr(item, "id", "")),
                                    "status": "failed" if level.endswith("ERROR") else "completed" if ended else "running",
                                    "scores": item_score_map,
                                    "usage": item_usage,
                                    "metrics": item_performance,
                                    "trace_url": (
                                        f"{runtime.base_url}/project/{project_id}/traces/{trace_id}"
                                        if project_id and trace_id else None
                                    ),
                                })
                            cursor = page.meta.cursor
                            if not cursor:
                                break
                    except Exception:
                        item_rows = []
                    aggregate_scores: dict[str, Any] = {}
                    for name, values in score_payload.items():
                        numeric = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
                        aggregate_scores[name] = (
                            sum(numeric) / len(numeric) if numeric else values[-1] if values else None
                        )
                    has_run_usage = cases_with_usage > 0
                    dataset_id = _field(run, "dataset_id") or _field(run, "datasetId")
                    deep_link = (
                        f"{runtime.base_url}/project/{project_id}/datasets/{dataset_id}/runs/{run_id}"
                        if project_id and dataset_id else None
                    )
                    rows.append({
                        "source": "langfuse",
                        "job_id": None,
                        "dataset_run_id": run_id,
                        "dataset_name": dataset_name,
                        "name": str(run.name),
                        "status": "failed" if failed_items else "completed" if completed_items else "pending",
                        "profile": metadata.get("profile"),
                        "benchmark": metadata.get("benchmark"),
                        "skill": metadata.get("skill"),
                        "model_preset": metadata.get("model_preset"),
                        "evaluation_source": metadata.get("evaluation_source"),
                        "required_score": metadata.get("required_remote_score"),
                        "item_count": len(item_rows),
                        "completed_items": completed_items,
                        "failed_items": failed_items,
                        "aggregate_scores": aggregate_scores,
                        "usage": run_usage if has_run_usage else None,
                        "metrics": {
                            "generation_count": run_generation_count,
                            "cases_with_usage": cases_with_usage,
                            "latency_seconds": round(run_latency_seconds, 3),
                            "ttft_seconds": round(run_ttft_seconds, 3),
                        },
                        "review_status": "pending" if metadata.get("annotation_queue_name") else "not_required",
                        "annotation_queue_name": metadata.get("annotation_queue_name"),
                        "langfuse_url": deep_link,
                        "created_at": _plain(_field(run, "created_at") or _field(run, "createdAt")),
                        "updated_at": _plain(_field(run, "updated_at") or _field(run, "updatedAt")),
                        "cases": item_rows,
                    })
            rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            return {"available": True, "error": None, "runs": rows[:limit]}
        except Exception as exc:
            return {"available": False, "error": str(exc)[:300], "runs": []}
        finally:
            if runtime is not None:
                runtime.shutdown()
