"""Classify evaluation failures without exposing full benchmark logs."""

from __future__ import annotations

import re
from typing import Any


def _last_matching(lines: list[str], patterns: tuple[str, ...]) -> str | None:
    for line in reversed(lines):
        lowered = line.lower()
        if any(pattern in lowered for pattern in patterns):
            return line.strip()[:500]
    return None


def _signal(
    lines: list[str],
    *,
    category: str,
    label: str,
    patterns: tuple[str, ...],
    summary: str,
) -> dict[str, Any] | None:
    matches = [line for line in lines if any(pattern in line.lower() for pattern in patterns)]
    if not matches:
        return None
    return {
        "category": category,
        "label": label,
        "summary": summary,
        "count": len(matches),
    }


def classify_evaluation_failure(
    output_tail: list[str] | None,
    error: str | None,
) -> dict[str, Any]:
    """Return a stable primary cause plus useful concurrent failure signals."""
    lines = [str(line) for line in (output_tail or []) if str(line).strip()]
    if error and (not lines or lines[-1].strip() != error.strip()):
        lines.append(error)
    lowered = "\n".join(lines).lower()

    evaluator_detail = _last_matching(
        lines,
        ("evaluator failed:", "unexpected keyword argument", "official evaluator"),
    )
    queue_detail = _last_matching(
        lines,
        ("maximum number of annotation queues", "annotation queue limit"),
    )
    queue_missing_detail = _last_matching(
        lines,
        ("annotation queue not found", "langfusenotfounderror"),
    )
    score_detail = _last_matching(
        lines,
        ("langfuse score readback failed", "score ingestion", "missing official_score"),
    )
    credential_detail = _last_matching(
        lines,
        ("api key", "authentication failed", "missing credential", "unauthorized"),
    )
    relay_detail = _last_matching(
        lines,
        ("service temporarily unavailable", "llm request failed after", "error code: 503"),
    )
    timeout_detail = _last_matching(
        lines,
        ("request timed out", "read timeout", "connect timeout", "timed out"),
    )
    connection_detail = _last_matching(
        lines,
        ("connection reset", "connection refused", "name or service not known", "network is unreachable"),
    )
    rate_limit_detail = _last_matching(lines, ("error code: 429", "rate limit", "too many requests"))

    if evaluator_detail:
        primary = {
            "category": "evaluator_error",
            "label": "Evaluator code error",
            "summary": "The evaluator callback failed, so required benchmark scores were not produced.",
            "detail": evaluator_detail,
            "retryable": True,
        }
    elif queue_missing_detail:
        primary = {
            "category": "langfuse_queue_missing",
            "label": "Langfuse queue missing",
            "summary": "The configured Langfuse annotation queue no longer exists.",
            "detail": queue_missing_detail,
            "retryable": True,
        }
    elif queue_detail:
        primary = {
            "category": "langfuse_queue_limit",
            "label": "Langfuse queue limit",
            "summary": "Langfuse rejected creation of another annotation queue.",
            "detail": queue_detail,
            "retryable": True,
        }
    elif credential_detail:
        primary = {
            "category": "configuration_error",
            "label": "Configuration or credentials",
            "summary": "A required service credential or authentication check failed.",
            "detail": credential_detail,
            "retryable": False,
        }
    elif rate_limit_detail:
        primary = {
            "category": "rate_limited",
            "label": "Service rate limited",
            "summary": "An upstream service rejected requests with HTTP 429.",
            "detail": rate_limit_detail,
            "retryable": True,
        }
    elif relay_detail:
        code = re.search(r"(?:error code:|http)\s*(5\d\d)", lowered)
        status = code.group(1) if code else "503"
        primary = {
            "category": "model_relay_unavailable",
            "label": f"Model relay HTTP {status}",
            "summary": "The Luna model relay was temporarily unavailable after retries.",
            "detail": relay_detail,
            "retryable": True,
        }
    elif timeout_detail:
        primary = {
            "category": "network_timeout",
            "label": "Network timeout",
            "summary": "A network request exceeded its timeout.",
            "detail": timeout_detail,
            "retryable": True,
        }
    elif connection_detail:
        primary = {
            "category": "network_connection",
            "label": "Network connection interrupted",
            "summary": "A network connection could not be established or was interrupted.",
            "detail": connection_detail,
            "retryable": True,
        }
    elif score_detail:
        primary = {
            "category": "langfuse_score_missing",
            "label": "Langfuse scores missing",
            "summary": "Required scores were not readable from Langfuse before the deadline.",
            "detail": score_detail,
            "retryable": True,
        }
    else:
        primary = {
            "category": "unknown",
            "label": "Other evaluation error",
            "summary": "The benchmark process exited unsuccessfully.",
            "detail": (error or (lines[-1] if lines else "No error output was captured."))[:500],
            "retryable": True,
        }

    signals = [
        _signal(
            lines,
            category="model_relay_unavailable",
            label="Model relay HTTP 503",
            patterns=("service temporarily unavailable", "error code: 503"),
            summary="The model relay returned a temporary service error.",
        ),
        _signal(
            lines,
            category="network_timeout",
            label="Network timeout",
            patterns=("request timed out", "read timeout", "connect timeout"),
            summary="One or more requests timed out.",
        ),
        _signal(
            lines,
            category="langfuse_score_missing",
            label="Langfuse scores missing",
            patterns=("langfuse score readback failed", "missing official_score"),
            summary="Required scores were absent at readback.",
        ),
    ]
    primary["signals"] = [
        signal for signal in signals
        if signal is not None and signal["category"] != primary["category"]
    ]
    return primary
