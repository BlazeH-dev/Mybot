"""Parameter-bound, one-shot approval records built on InteractionRequest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from nanobot.runtime.interactions import (
    InteractionKind,
    InteractionManager,
    InteractionRequest,
    InteractionStatus,
    InteractionStrategy,
)


def normalized_params_hash(params: dict[str, Any]) -> str:
    payload = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    tool_name: str
    normalized_params_hash: str
    task_id: str | None
    plan_hash: str | None
    step_id: str | None
    child_id: str | None
    target: str | None
    risk: str
    reason: str
    sandbox_mode: str
    chat_id: str | None = None
    provider: str | None = None
    command_hash: str | None = None
    writable_roots: tuple[str, ...] = ()
    network_domains: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    network_addresses: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "normalized_params_hash": self.normalized_params_hash,
            "task_id": self.task_id,
            "plan_hash": self.plan_hash,
            "step_id": self.step_id,
            "child_id": self.child_id,
            "target": self.target,
            "risk": self.risk,
            "reason": self.reason,
            "sandbox_mode": self.sandbox_mode,
            "chat_id": self.chat_id,
            "provider": self.provider,
            "command_hash": self.command_hash,
            "writable_roots": list(self.writable_roots),
            "network_domains": list(self.network_domains),
            "ports": list(self.ports),
            "network_addresses": list(self.network_addresses),
        }


class ApprovalManager:
    def __init__(self, interactions: InteractionManager) -> None:
        self.interactions = interactions

    def request(
        self,
        binding: ApprovalBinding,
        *,
        tool_call_id: str,
        turn_id: str | None,
        child_id: str | None = None,
        ttl_seconds: int = 300,
    ) -> InteractionRequest:
        expires = datetime.now(timezone.utc) + timedelta(seconds=max(1, ttl_seconds))
        return self.interactions.create(
            kind=InteractionKind.APPROVAL,
            strategy=InteractionStrategy.EXPIRE_AND_DENY,
            task_id=binding.task_id,
            turn_id=turn_id,
            plan_hash=binding.plan_hash,
            step_id=binding.step_id,
            child_id=child_id,
            tool_call_id=tool_call_id,
            continuation={
                "tool_name": binding.tool_name,
                "params_hash": binding.normalized_params_hash,
            },
            payload={"binding": binding.as_dict(), "chat_id": binding.chat_id},
            expires_at=expires.isoformat(),
        )

    @staticmethod
    def matches(request: InteractionRequest, binding: ApprovalBinding) -> bool:
        raw = request.payload.get("binding") if isinstance(request.payload, dict) else None
        return (
            isinstance(raw, dict)
            and raw == binding.as_dict()
            and request.status == InteractionStatus.APPROVED
        )

    def find_approved(self, binding: ApprovalBinding) -> InteractionRequest | None:
        for request in reversed(self.interactions.list_all(task_id=binding.task_id)):
            if self.matches(request, binding):
                return request
        return None
