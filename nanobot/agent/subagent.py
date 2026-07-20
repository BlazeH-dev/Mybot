"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import OUTBOUND_META_AGENT_UI, InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, ToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.runtime.approvals import ApprovalBinding, ApprovalManager, normalized_params_hash
from nanobot.runtime.artifacts import ArtifactStore
from nanobot.runtime.interactions import (
    InteractionKind,
    InteractionManager,
    InteractionRequest,
    InteractionStatus,
    InteractionStrategy,
)
from nanobot.runtime.policy import PermissionDecision, PolicyEngine, PolicyGateOutcome
from nanobot.runtime.trace import TraceContext, TraceHook, current_trace_context, emit_trace_event
from nanobot.security.sandbox import SandboxMode
from nanobot.security.sandbox.network import (
    command_hash,
    command_network_targets,
    encode_address_binding,
    resolve_public_addresses,
)
from nanobot.security.workspace_access import (
    WorkspaceScope,
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
    workspace_sandbox_status,
)
from nanobot.utils.prompt_templates import render_template


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None
    parent_task_id: str | None = None
    artifact_root: str | None = None


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self.runner = AgentRunner(provider)
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._spawned_by_parent: dict[str, int] = {}
        self.max_direct_children = 5
        self._artifacts = ArtifactStore(workspace)
        self.interactions = InteractionManager(workspace)
        self.approvals = ApprovalManager(self.interactions)
        self.policy = PolicyEngine(
            audit_path=workspace / ".nanobot-runtime" / "trace" / "policy-audit.jsonl"
        )

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
        )
        ToolLoader().load(ctx, registry, scope="subagent")
        return registry

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        parent_task_id: str | None = None,
        parent_plan_hash: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        parent_key = parent_task_id or session_key or f"{origin_channel}:{origin_chat_id}"
        spawned = self._spawned_by_parent.get(parent_key, 0)
        if spawned >= self.max_direct_children:
            return (
                "Cannot spawn subagent: direct child limit reached "
                f"({spawned}/{self.max_direct_children})."
            )
        self._spawned_by_parent[parent_key] = spawned + 1
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
            parent_task_id=parent_task_id,
        )
        self._task_statuses[task_id] = status
        emit_trace_event("mybot.subagent.spawn", {
            "child_id": task_id,
            "parent_task_id": parent_task_id,
            "workload_quotas_enabled": False,
            "plan_hash": parent_plan_hash,
        })
        parent_trace = current_trace_context()

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                origin_message_id,
                temperature,
                workspace_scope,
                parent_task_id,
                parent_plan_hash,
                parent_trace,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _publish_interaction(
        self,
        *,
        origin: dict[str, str],
        request: InteractionRequest,
        content: str,
    ) -> None:
        await self.bus.publish_outbound(OutboundMessage(
            channel=origin["channel"],
            chat_id=origin["chat_id"],
            content=content,
            metadata={
                "_progress": True,
                OUTBOUND_META_AGENT_UI: {
                    "kind": "interaction_request",
                    "interaction": request.as_dict(),
                },
            },
        ))

    async def _child_policy_gate(
        self,
        *,
        tool_call: Any,
        tool: Any,
        params: dict[str, Any],
        child_scope: WorkspaceScope,
        child_id: str,
        parent_task_id: str,
        parent_plan_hash: str | None,
        origin: dict[str, str],
        origin_message_id: str | None,
    ) -> PolicyGateOutcome:
        chat_id = origin["chat_id"]
        if tool.name == "request_user_input":
            strategy = InteractionStrategy(str(params.get("strategy") or "required"))
            expires_at = None
            if strategy == InteractionStrategy.AUTO_RESOLVE:
                expires_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=int(params.get("timeout_seconds") or 60))
                ).isoformat()
            payload: dict[str, Any] = {"chat_id": chat_id}
            if params.get("default") is not None:
                payload["default"] = params["default"]
            request = self.interactions.create(
                kind=InteractionKind.QUESTION,
                strategy=strategy,
                task_id=parent_task_id,
                turn_id=origin_message_id,
                plan_hash=parent_plan_hash,
                child_id=child_id,
                tool_call_id=tool_call.id,
                continuation={"tool_name": tool.name, "child_id": child_id},
                payload=payload,
                questions=list(params.get("questions") or []),
                expires_at=expires_at,
            )
            await self._publish_interaction(
                origin=origin,
                request=request,
                content="A subagent is waiting for your input.",
            )
            emit_trace_event("mybot.interaction.requested", {
                "kind": request.kind.value,
                "strategy": request.strategy.value,
                "request_id": request.request_id,
                "child_id": child_id,
            })
            return PolicyGateOutcome(
                decision=PermissionDecision(
                    action="ask",
                    reason="the child task is waiting for typed user input",
                    matched_rules=("interaction.child_question",),
                    risk_level="low",
                ),
                interaction=request.as_dict(),
            )

        decision = self.policy.evaluate(
            tool=tool,
            params=params,
            scope=child_scope,
            sandbox_mode=SandboxMode.WORKSPACE_WRITE,
            task_id=parent_task_id,
            plan_hash=parent_plan_hash,
            child_id=child_id,
        )
        emit_trace_event("mybot.policy.decision", {
            "tool_name": tool.name,
            "child_id": child_id,
            **decision.as_dict(),
        })
        if decision.action != "ask":
            return PolicyGateOutcome(decision=decision)

        raw_command = str(params.get("command") or params.get("cmd") or "")
        network_domains: tuple[str, ...] = ()
        network_ports: tuple[int, ...] = ()
        network_addresses: tuple[str, ...] = ()
        if tool.name == "exec" and raw_command:
            try:
                network_domains, network_ports, minimal_network_command = (
                    command_network_targets(raw_command)
                )
                if network_domains and not minimal_network_command:
                    return PolicyGateOutcome(decision=PermissionDecision(
                        action="deny",
                        reason=(
                            "network escalation only supports a single direct curl command "
                            "without shell composition or redirects"
                        ),
                        matched_rules=("hard.network_command_shape",),
                        risk_level="critical",
                        target=raw_command[:500],
                        hard_deny=True,
                    ))
                network_addresses = tuple(
                    encode_address_binding(domain, address)
                    for domain in network_domains
                    for address in resolve_public_addresses(domain)
                )
            except ValueError as exc:
                return PolicyGateOutcome(decision=PermissionDecision(
                    action="deny",
                    reason=str(exc),
                    matched_rules=("hard.ssrf",),
                    risk_level="critical",
                    target=raw_command[:500],
                    hard_deny=True,
                ))

        binding = ApprovalBinding(
            tool_name=tool.name,
            normalized_params_hash=normalized_params_hash(params),
            task_id=parent_task_id,
            plan_hash=parent_plan_hash,
            step_id=None,
            child_id=child_id,
            target=decision.target,
            risk=decision.risk_level,
            reason=decision.reason,
            sandbox_mode=SandboxMode.WORKSPACE_WRITE.value,
            chat_id=chat_id,
            provider=child_scope.sandbox_status.provider,
            command_hash=command_hash(raw_command) if raw_command else None,
            writable_roots=(str(child_scope.project_path),),
            network_domains=network_domains,
            ports=network_ports,
            network_addresses=network_addresses,
        )
        approved = self.approvals.find_approved(binding)
        if approved is not None:
            self.interactions.consume(
                approved.request_id,
                expected_revision=approved.revision,
                idempotency_key=f"child-tool:{child_id}:{tool_call.id}",
            )
            return PolicyGateOutcome(
                decision=PermissionDecision(
                    action="allow",
                    reason="matched a child-bound one-shot approval",
                    matched_rules=("approval.child_one_shot",),
                    risk_level=decision.risk_level,
                    target=decision.target,
                ),
                execution_context=(
                    {
                        "network_grant": {
                            "domains": list(binding.network_domains),
                            "ports": list(binding.ports),
                            "command_hash": binding.command_hash,
                            "expires_at": approved.expires_at,
                            "addresses": list(binding.network_addresses),
                        }
                    }
                    if binding.network_domains and binding.command_hash
                    else None
                ),
            )

        request = self.approvals.request(
            binding,
            tool_call_id=tool_call.id,
            turn_id=origin_message_id,
            child_id=child_id,
        )
        await self._publish_interaction(
            origin=origin,
            request=request,
            content=decision.reason,
        )
        emit_trace_event("mybot.interaction.requested", {
            "kind": request.kind.value,
            "strategy": request.strategy.value,
            "request_id": request.request_id,
            "tool_name": tool.name,
            "child_id": child_id,
        })
        return PolicyGateOutcome(decision=decision, interaction=request.as_dict())

    async def _wait_for_interaction(self, request_id: str) -> InteractionRequest:
        while True:
            self.interactions.expire_due()
            request = self.interactions.get(request_id)
            if request.status != InteractionStatus.PENDING:
                return request
            await asyncio.sleep(0.1)

    @staticmethod
    def _suspended_interaction(result: Any) -> dict[str, Any] | None:
        for message in reversed(result.messages):
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            try:
                payload = json.loads(message["content"])
            except (TypeError, json.JSONDecodeError):
                continue
            interaction = payload.get("interaction") if isinstance(payload, dict) else None
            if isinstance(interaction, dict) and interaction.get("request_id"):
                return interaction
        return None

    def _resume_child_messages(
        self,
        result: Any,
        request: InteractionRequest,
        *,
        child_id: str,
    ) -> list[dict[str, Any]]:
        messages = [dict(message) for message in result.messages]
        resumed = {
            "request_id": request.request_id,
            "kind": request.kind.value,
            "status": request.status.value,
            "response": request.response,
            "resolution": request.resolution,
        }
        for message in reversed(messages):
            if (
                message.get("role") == "tool"
                and message.get("tool_call_id") == request.tool_call_id
            ):
                message["content"] = json.dumps(resumed, ensure_ascii=False)
                break
        if request.kind != InteractionKind.APPROVAL or request.status != InteractionStatus.APPROVED:
            self.interactions.consume(
                request.request_id,
                expected_revision=request.revision,
                idempotency_key=f"child-resume:{child_id}:{request.request_id}",
            )
        return messages

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        parent_task_id: str | None = None,
        parent_plan_hash: str | None = None,
        parent_trace: TraceContext | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        temporary_parent: TemporaryDirectory[str] | None = None

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            parent_root = workspace_scope.project_path if workspace_scope is not None else self.workspace
            if not isinstance(parent_root, Path):
                temporary_parent = TemporaryDirectory(prefix="nanobot-subagent-")
                parent_root = Path(temporary_parent.name)
            effective_parent_task = parent_task_id or f"session_{origin.get('session_key') or 'direct'}"
            safe_parent_task = effective_parent_task.replace(":", "_").replace("/", "_")
            root = ArtifactStore(parent_root).child_root(safe_parent_task, task_id)
            root.mkdir(parents=True, exist_ok=True)
            status.artifact_root = str(root)
            cfg = self._subagent_tools_config()
            cfg.restrict_to_workspace = True
            tools = self._build_tools(workspace=root, tools_config=cfg)
            system_prompt = self._build_subagent_prompt(workspace=root)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            sess_key = origin.get("session_key")
            llm_timeout = (
                self._llm_wall_timeout_for_session(sess_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            child_scope = build_workspace_scope(root, "restricted", source_channel="subagent")
            token = bind_workspace_scope(child_scope)
            child_trace = TraceHook(
                root.parents[3] / "trace" / f"{safe_parent_task}.jsonl",
                task_id=safe_parent_task,
                actor=f"child:{task_id}",
                model=self.model,
                parent=parent_trace,
            )

            async def _policy_gate(*, tool_call, tool, params, spec) -> PolicyGateOutcome:
                del spec
                return await self._child_policy_gate(
                    tool_call=tool_call,
                    tool=tool,
                    params=params,
                    child_scope=child_scope,
                    child_id=task_id,
                    parent_task_id=effective_parent_task,
                    parent_plan_hash=parent_plan_hash,
                    origin=origin,
                    origin_message_id=origin_message_id,
                )

            async def _run_lifecycle() -> AgentRunResult:
                nonlocal messages
                cumulative_usage: dict[str, int] = {}
                cumulative_events: list[dict[str, str]] = []
                cumulative_tools: list[str] = []
                while True:
                    result = await self.runner.run(AgentRunSpec(
                        initial_messages=messages,
                        tools=tools,
                        model=self.model,
                        temperature=temperature,
                        max_iterations=self.max_iterations,
                        max_tool_result_chars=self.max_tool_result_chars,
                        hook=CompositeHook([_SubagentHook(task_id, status), child_trace]),
                        max_iterations_message=(
                            "Stopped by the loop guard after {max_iterations} iterations. "
                            "Partial progress is reported below."
                        ),
                        error_message=None,
                        fail_on_tool_error=True,
                        checkpoint_callback=_on_checkpoint,
                        session_key=sess_key,
                        workspace=root,
                        llm_timeout_s=llm_timeout,
                        policy_gate=_policy_gate,
                        actor=f"child:{task_id}",
                        task_id=safe_parent_task,
                        plan_hash=parent_plan_hash,
                    ))
                    for key, value in result.usage.items():
                        cumulative_usage[key] = cumulative_usage.get(key, 0) + int(value)
                    cumulative_events.extend(result.tool_events)
                    cumulative_tools.extend(result.tools_used)
                    if not result.stop_reason.startswith("awaiting_"):
                        result.usage = cumulative_usage
                        result.tool_events = cumulative_events
                        result.tools_used = cumulative_tools
                        return result
                    interaction = self._suspended_interaction(result)
                    if interaction is None:
                        return AgentRunResult(
                            final_content=None,
                            messages=result.messages,
                            tools_used=cumulative_tools,
                            usage=cumulative_usage,
                            stop_reason="error",
                            error="Error: child interaction suspension lost its request payload.",
                            tool_events=cumulative_events,
                        )
                    status.phase = result.stop_reason
                    request = await self._wait_for_interaction(str(interaction["request_id"]))
                    try:
                        created_at = datetime.fromisoformat(request.created_at)
                        resolved_at = datetime.fromisoformat(
                            request.resolved_at or datetime.now(timezone.utc).isoformat()
                        )
                        human_wait_ms = max(
                            0,
                            int((resolved_at - created_at).total_seconds() * 1000),
                        )
                    except (TypeError, ValueError):
                        human_wait_ms = 0
                    emit_trace_event("mybot.interaction.resumed", {
                        "request_id": request.request_id,
                        "kind": request.kind.value,
                        "status": request.status.value,
                        "child_id": task_id,
                        "mybot.human_wait_ms": human_wait_ms,
                    })
                    messages = self._resume_child_messages(result, request, child_id=task_id)

            try:
                result = await _run_lifecycle()
            finally:
                reset_workspace_scope(token)
            partial_failure = result.stop_reason in {
                "tool_error",
                "budget_exceeded",
                "max_iterations",
            }
            status.phase = "error" if partial_failure else "done"
            status.stop_reason = result.stop_reason
            emit_trace_event("mybot.subagent.complete", {
                "child_id": task_id,
                "stop_reason": result.stop_reason,
                "usage": result.usage,
                "artifact_root": str(root),
            })

            if result.stop_reason == "max_iterations":
                emit_trace_event("mybot.subagent.loop_guard", {
                    "child_id": task_id,
                    "max_iterations": self.max_iterations,
                    "partial_progress": True,
                })
            if partial_failure:
                status.tool_events = list(result.tool_events)
                await self._announce_result(
                    task_id, label, task,
                    self._format_partial_progress(result),
                    origin, "error", origin_message_id,
                )
            elif result.stop_reason == "error":
                await self._announce_result(
                    task_id, label, task,
                    result.error or "Error: subagent execution failed.",
                    origin, "error", origin_message_id,
                )
            else:
                final_result = result.final_content or "Task completed but no final response was generated."
                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(task_id, label, task, final_result, origin, "ok", origin_message_id)

        except asyncio.CancelledError:
            status.phase = "error"
            status.stop_reason = "cancelled"
            status.error = "Subagent cancelled."
            emit_trace_event("mybot.subagent.cancelled", {
                "child_id": task_id,
                "parent_task_id": parent_task_id,
            })
            raise
        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            emit_trace_event("mybot.subagent.failed", {
                "child_id": task_id,
                "error": type(e).__name__,
            })
            logger.exception("Subagent [{}] failed", task_id)
            await self._announce_result(task_id, label, task, f"Error: {e}", origin, "error", origin_message_id)
        finally:
            if temporary_parent is not None:
                temporary_parent.cleanup()

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        final_content = result.final_content if isinstance(result.final_content, str) else None
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (
            result.error
            or final_content
            or "Error: subagent execution failed."
        )

    def _build_subagent_prompt(self, workspace: Path | None = None) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        root = workspace or self.workspace
        skills_summary = SkillsLoader(
            root,
            disabled_skills=self.disabled_skills,
        ).build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
