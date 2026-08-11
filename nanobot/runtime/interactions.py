"""Durable, revisioned human interaction requests with deterministic deadlines."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


class InteractionKind(StrEnum):
    QUESTION = "question"
    APPROVAL = "approval"
    PLAN_CONFIRMATION = "plan_confirmation"
    RECOVERY_DECISION = "recovery_decision"
    # Read-only compatibility for requests persisted before post-plan reflection was removed.
    REFLECTION_DECISION = "reflection_decision"


class InteractionStrategy(StrEnum):
    REQUIRED = "required"
    AUTO_RESOLVE = "auto_resolve"
    EXPIRE_AND_DENY = "expire_and_deny"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    CONSUMED = "consumed"


_TERMINAL = frozenset({
    InteractionStatus.CONSUMED,
    InteractionStatus.CANCELLED,
    InteractionStatus.SUPERSEDED,
})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class InteractionRequest:
    request_id: str
    revision: int
    kind: InteractionKind
    task_id: str | None
    turn_id: str | None
    plan_hash: str | None
    step_id: str | None
    child_id: str | None
    continuation: dict[str, Any]
    tool_call_id: str | None
    payload: dict[str, Any]
    questions: list[dict[str, Any]]
    strategy: InteractionStrategy
    created_at: str
    expires_at: str | None
    status: InteractionStatus = InteractionStatus.PENDING
    response: dict[str, Any] | None = None
    resolution: dict[str, Any] | None = None
    resolved_at: str | None = None
    idempotency_keys: list[str] = field(default_factory=list)
    consumed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["strategy"] = self.strategy.value
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "InteractionRequest":
        return cls(
            request_id=str(raw["request_id"]),
            revision=int(raw.get("revision", 1)),
            kind=InteractionKind(raw["kind"]),
            task_id=raw.get("task_id"),
            turn_id=raw.get("turn_id"),
            plan_hash=raw.get("plan_hash"),
            step_id=raw.get("step_id"),
            child_id=raw.get("child_id"),
            continuation=dict(raw.get("continuation") or {}),
            tool_call_id=raw.get("tool_call_id"),
            payload=dict(raw.get("payload") or {}),
            questions=list(raw.get("questions") or []),
            strategy=InteractionStrategy(raw["strategy"]),
            created_at=str(raw["created_at"]),
            expires_at=raw.get("expires_at"),
            status=InteractionStatus(raw.get("status", "pending")),
            response=dict(raw["response"]) if isinstance(raw.get("response"), dict) else None,
            resolution=(
                dict(raw["resolution"])
                if isinstance(raw.get("resolution"), dict)
                else None
            ),
            resolved_at=raw.get("resolved_at"),
            idempotency_keys=[str(item) for item in raw.get("idempotency_keys", [])],
            consumed_at=raw.get("consumed_at"),
        )


class InteractionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InteractionManager:
    """Filesystem-backed interaction state; WebSocket is only a projection."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.root = self.workspace / ".nanobot-runtime" / "interactions"
        self._now = now
        self._lock = threading.RLock()

    def create(
        self,
        *,
        kind: InteractionKind,
        strategy: InteractionStrategy,
        task_id: str | None = None,
        turn_id: str | None = None,
        plan_hash: str | None = None,
        step_id: str | None = None,
        child_id: str | None = None,
        continuation: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        payload: dict[str, Any] | None = None,
        questions: list[dict[str, Any]] | None = None,
        expires_at: str | None = None,
        request_id: str | None = None,
    ) -> InteractionRequest:
        if strategy == InteractionStrategy.EXPIRE_AND_DENY and not expires_at:
            raise InteractionError("deadline_required", "expire_and_deny requires expires_at")
        request = InteractionRequest(
            request_id=request_id or f"ir_{uuid4().hex[:20]}",
            revision=1,
            kind=kind,
            task_id=task_id,
            turn_id=turn_id,
            plan_hash=plan_hash,
            step_id=step_id,
            child_id=child_id,
            continuation=dict(continuation or {}),
            tool_call_id=tool_call_id,
            payload=dict(payload or {}),
            questions=list(questions or []),
            strategy=strategy,
            created_at=self._now().isoformat(),
            expires_at=expires_at,
        )
        with self._lock:
            if self._path(request.request_id).exists():
                raise InteractionError("request_exists", request.request_id)
            self._write(request)
        return request

    def get(self, request_id: str) -> InteractionRequest:
        with self._lock:
            path = self._path(request_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise InteractionError("not_found", request_id) from exc
            except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
                raise InteractionError("corrupt_interaction", request_id) from exc
            if not isinstance(raw, dict):
                raise InteractionError("corrupt_interaction", request_id)
            return InteractionRequest.from_dict(raw)

    def list_pending(self, *, task_id: str | None = None) -> list[InteractionRequest]:
        return [
            request
            for request in self.list_all(task_id=task_id)
            if request.status == InteractionStatus.PENDING
        ]

    def list_all(self, *, task_id: str | None = None) -> list[InteractionRequest]:
        if not self.root.exists():
            return []
        requests: list[InteractionRequest] = []
        for path in sorted(self.root.glob("ir_*.json")):
            try:
                request = self.get(path.stem)
            except InteractionError:
                continue
            if task_id is not None and request.task_id != task_id:
                continue
            requests.append(request)
        return requests

    def respond(
        self,
        request_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        response: dict[str, Any],
    ) -> InteractionRequest:
        if not idempotency_key.strip():
            raise InteractionError("idempotency_key_required", "idempotency key is required")
        with self._lock:
            request = self.get(request_id)
            if idempotency_key in request.idempotency_keys:
                return request
            if request.revision != expected_revision:
                raise InteractionError("revision_mismatch", request_id)
            if request.status != InteractionStatus.PENDING:
                raise InteractionError("not_pending", request.status.value)
            request = self._expire_one(request)
            if request.status != InteractionStatus.PENDING:
                request.idempotency_keys.append(idempotency_key)
                request.revision += 1
                self._write(request)
                return request

            approved = response.get("approved")
            if request.kind == InteractionKind.APPROVAL:
                if approved is True:
                    request.status = InteractionStatus.APPROVED
                elif approved is False:
                    request.status = InteractionStatus.DENIED
                else:
                    raise InteractionError("approval_answer_required", request_id)
            else:
                request.status = InteractionStatus.ANSWERED
            request.response = dict(response)
            request.resolved_at = self._now().isoformat()
            request.idempotency_keys.append(idempotency_key)
            request.revision += 1
            self._write(request)
            return request

    def cancel(
        self,
        request_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> InteractionRequest:
        return self._transition(
            request_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            status=InteractionStatus.CANCELLED,
        )

    def consume(
        self,
        request_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> InteractionRequest:
        with self._lock:
            request = self.get(request_id)
            if idempotency_key in request.idempotency_keys:
                return request
            if request.revision != expected_revision:
                raise InteractionError("revision_mismatch", request_id)
            if request.status not in {
                InteractionStatus.ANSWERED,
                InteractionStatus.APPROVED,
                InteractionStatus.DENIED,
                InteractionStatus.TIMED_OUT,
                InteractionStatus.EXPIRED,
            }:
                raise InteractionError("not_resolved", request.status.value)
            request.resolution = {
                "status": request.status.value,
                "response": request.response,
            }
            request.status = InteractionStatus.CONSUMED
            request.consumed_at = self._now().isoformat()
            request.idempotency_keys.append(idempotency_key)
            request.revision += 1
            self._write(request)
            return request

    def expire_due(self) -> list[InteractionRequest]:
        resolved: list[InteractionRequest] = []
        with self._lock:
            for request in self.list_pending():
                updated = self._expire_one(request)
                if updated.status != InteractionStatus.PENDING:
                    updated.revision += 1
                    self._write(updated)
                    resolved.append(updated)
        return resolved

    def _expire_one(self, request: InteractionRequest) -> InteractionRequest:
        deadline = _parse_time(request.expires_at)
        if deadline is None or self._now() < deadline:
            return request
        if request.strategy == InteractionStrategy.REQUIRED:
            return request
        if request.strategy == InteractionStrategy.EXPIRE_AND_DENY:
            request.status = InteractionStatus.EXPIRED
            request.resolution = {"approved": False, "reason": "deadline_expired"}
            request.resolved_at = self._now().isoformat()
            return request
        request.status = InteractionStatus.TIMED_OUT
        default = request.payload.get("default")
        request.resolution = (
            {"source": "deterministic_default", "value": default}
            if "default" in request.payload
            else {"source": "model_best_judgment"}
        )
        request.resolved_at = self._now().isoformat()
        return request

    def _transition(
        self,
        request_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        status: InteractionStatus,
    ) -> InteractionRequest:
        with self._lock:
            request = self.get(request_id)
            if idempotency_key in request.idempotency_keys:
                return request
            if request.revision != expected_revision:
                raise InteractionError("revision_mismatch", request_id)
            if request.status in _TERMINAL:
                raise InteractionError("terminal", request.status.value)
            request.status = status
            request.resolved_at = self._now().isoformat()
            request.idempotency_keys.append(idempotency_key)
            request.revision += 1
            self._write(request)
            return request

    def _path(self, request_id: str) -> Path:
        if not request_id.startswith("ir_") or any(ch in request_id for ch in "/\\\0"):
            raise InteractionError("invalid_request_id", request_id)
        return self.root / f"{request_id}.json"

    def _write(self, request: InteractionRequest) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(request.request_id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(request.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
