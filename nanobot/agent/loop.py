"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from contextlib import AsyncExitStack, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent import context as agent_context
from nanobot.agent import model_presets as preset_helpers
from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.execution_mode import (
    EXECUTION_MODE_METADATA_KEY,
    EXECUTION_MODE_PLAN_ONLY,
    PLAN_REVISION_REQUEST_METADATA_KEY,
    execution_mode_from_metadata,
    plan_only_prompt,
    plan_only_registry,
)
from nanobot.agent.hook import AgentHook, CompositeHook
from nanobot.agent.memory import Consolidator
from nanobot.agent.progress_hook import AgentProgressHook
from nanobot.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.self import MyTool
from nanobot.bus.events import OUTBOUND_META_AGENT_UI, InboundMessage, OutboundMessage
from nanobot.bus.progress import build_bus_progress_callback
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventPublisher,
    ensure_runtime_event_publisher,
)
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.config.schema import AgentDefaults, ModelPresetConfig
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import ProviderSnapshot
from nanobot.runtime.approvals import ApprovalBinding, ApprovalManager, normalized_params_hash
from nanobot.runtime.checkpoint import CheckpointError, CheckpointStore
from nanobot.runtime.interactions import (
    InteractionKind,
    InteractionManager,
    InteractionStatus,
    InteractionStrategy,
)
from nanobot.runtime.langfuse import LangfuseRuntime
from nanobot.runtime.plan_scheduler import PlanScheduler, contract_hash
from nanobot.runtime.policy import (
    PermissionDecision,
    PolicyEngine,
    PolicyGateOutcome,
    sandbox_mode_for_scope,
)
from nanobot.runtime.trace import TraceHook, emit_trace_event
from nanobot.security.sandbox.network import (
    command_hash,
    command_network_targets,
    encode_address_binding,
    resolve_public_addresses,
)
from nanobot.security.workspace_access import (
    WorkspaceScopeResolver,
    bind_workspace_scope,
    reset_workspace_scope,
)
from nanobot.session import turn_continuation
from nanobot.session.goal_state import (
    goal_state_runtime_lines,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.document import extract_documents, reference_non_image_attachments
from nanobot.utils.helpers import image_placeholder_text
from nanobot.utils.helpers import truncate_text as truncate_text_fn
from nanobot.utils.image_generation_intent import image_generation_prompt
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    SUSTAINED_GOAL_CONTINUE_PROMPT,
)

if TYPE_CHECKING:
    from nanobot.config.schema import (
        ChannelsConfig,
        ProviderConfig,
        ToolsConfig,
    )
    from nanobot.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"

class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class StateTraceEntry:
    state: TurnState
    started_at: float
    duration_ms: float
    event: str
    error: str | None = None


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState
    turn_id: str
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)

    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False

    user_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None
    suppress_response: bool = False

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue | None = None
    pending_summary: str | None = None

    ephemeral: bool = False
    tools: ToolRegistry | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    visible_run_started_at: float | None = None
    turn_latency_ms: int | None = None

    trace: list[StateTraceEntry] = field(default_factory=list)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    def llm_runtime(self) -> LLMRuntime:
        """Return the current provider/model pair owned by this loop."""
        self._refresh_provider_snapshot()
        return LLMRuntime(self.provider, self.model)

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"
    _RUNTIME_TRACE_EVENTS_KEY = "runtime_trace_events"

    # Event-driven state transition table.
    # Handlers return an event string; the driver looks up the next state here.
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.RESTORE, "child_interaction"): TurnState.DONE,
        (TurnState.RESTORE, "waiting"): TurnState.DONE,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.RUN, "suspended"): TurnState.DONE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        image_generation_provider_config: ProviderConfig | None = None,
        image_generation_provider_configs: dict[str, ProviderConfig] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_events: RuntimeEventBus | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        observability: LangfuseRuntime | None = None,
    ):
        from nanobot.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.runtime_events = runtime_events or RuntimeEventBus()
        self.runtime_event_publisher = RuntimeEventPublisher(self.runtime_events)
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader
        self._runtime_model_publisher = runtime_model_publisher
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(provider_signature)
        self.workspace = workspace
        self.interactions = InteractionManager(workspace)
        self.approvals = ApprovalManager(self.interactions)
        self.policy = PolicyEngine(
            audit_path=workspace / ".nanobot-runtime" / "trace" / "policy-audit.jsonl"
        )
        self.checkpoints = CheckpointStore(workspace)
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self._image_generation_provider_configs = dict(image_generation_provider_configs or {})
        if (
            image_generation_provider_config is not None
            and "openrouter" not in self._image_generation_provider_configs
        ):
            self._image_generation_provider_configs["openrouter"] = image_generation_provider_config
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []
        self.observability = observability

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider, observability=observability)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=max_concurrent_subagents,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
            observability=observability,
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from nanobot.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        observability = extra.pop("observability", None)
        acquired_observability = False
        if observability is None and config.observability.langfuse.enabled:
            observability = LangfuseRuntime.acquire(config.observability.langfuse)
            acquired_observability = True
        try:
            provider = extra.pop("provider", None) or make_provider(config)
            resolved = config.resolve_preset()
            model = extra.pop("model", None) or resolved.model
            context_window_tokens = extra.pop("context_window_tokens", None) or resolved.context_window_tokens
            provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
            preset_snapshot_loader = extra.pop("preset_snapshot_loader", None) or preset_helpers.make_preset_snapshot_loader(
                config,
                provider_snapshot_loader,
            )
            return cls(
                bus=bus,
                provider=provider,
                workspace=config.workspace_path,
                model=model,
                max_iterations=defaults.max_tool_iterations,
                max_concurrent_subagents=defaults.max_concurrent_subagents,
                context_window_tokens=context_window_tokens,
                context_block_limit=defaults.context_block_limit,
                max_tool_result_chars=defaults.max_tool_result_chars,
                provider_retry_mode=defaults.provider_retry_mode,
                tool_hint_max_length=defaults.tool_hint_max_length,
                restrict_to_workspace=config.tools.restrict_to_workspace,
                mcp_servers=config.tools.mcp_servers,
                channels_config=config.channels,
                timezone=defaults.timezone,
                unified_session=defaults.unified_session,
                disabled_skills=defaults.disabled_skills,
                session_ttl_minutes=defaults.session_ttl_minutes,
                consolidation_ratio=defaults.consolidation_ratio,
                max_messages=defaults.max_messages,
                tools_config=config.tools,
                model_presets=preset_helpers.configured_model_presets(config),
                model_preset=defaults.model_preset,
                provider_snapshot_loader=provider_snapshot_loader,
                preset_snapshot_loader=preset_snapshot_loader,
                observability=observability,
                **extra,
            )
        except BaseException:
            if acquired_observability and observability is not None:
                observability.release()
            raise

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        if publish_update:
            self._runtime_events().runtime_model_changed(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self) -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except ValueError as exc:
            from nanobot.providers.unconfigured_provider import UnconfiguredProvider

            if (
                isinstance(self.provider, UnconfiguredProvider)
                and str(exc).startswith("No API key configured for provider '")
            ):
                logger.debug("Provider remains unconfigured; waiting for WebUI settings update")
                return
            logger.exception("Failed to refresh provider config")
            return
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(snapshot.signature)
        if self._active_preset and self._default_selection_signature in (None, default_selection):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(snapshot.signature)
        self._apply_provider_snapshot(snapshot)

    @property
    def model_preset(self) -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """Resolve a preset by name and apply all runtime model dependents."""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name

    def _register_default_tools(self) -> None:
        """Register the default set of tools via plugin loader."""
        from nanobot.agent.tools.context import ToolContext
        from nanobot.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            image_generation_provider_configs=self._image_generation_provider_configs,
            timezone=self.context.timezone or "UTC",
            workspace_sandbox=self.workspace_scopes.sandbox_status,
            runtime_events=self.runtime_events,
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        logger.info("Registered {} tools: {}", len(registered), registered)

    async def _connect_mcp(self) -> None:
        """Connect configured MCP servers."""
        await agent_context.connect_mcp(self, self.tools)

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from nanobot.agent.tools.context import ContextAware

        if session_key is not None:
            effective_key = session_key
        elif self._unified_session:
            effective_key = UNIFIED_SESSION_KEY
        else:
            effective_key = f"{channel}:{chat_id}"

        request_metadata = dict(metadata or {})
        session = self.sessions.get_or_create(effective_key)
        current_plan = (
            session.metadata.get("plan_state")
            if session is not None and isinstance(session.metadata, dict)
            else None
        )
        if isinstance(current_plan, dict) and current_plan.get("task_id"):
            request_metadata["_runtime_task_id"] = str(current_plan["task_id"])
            request_metadata["_runtime_plan_hash"] = str(current_plan.get("plan_hash") or "")
            request_metadata["_runtime_plan_status"] = str(current_plan.get("status") or "")
            request_metadata["_runtime_approved_plan_hash"] = str(
                current_plan.get("approved_plan_hash") or ""
            )
            request_metadata["_runtime_plan_managed_children"] = any(
                isinstance(step, dict) and step.get("executor") == "child"
                for step in (current_plan.get("steps") or [])
            )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=request_metadata,
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus."""

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _runtime_events(self) -> RuntimeEventPublisher:
        return ensure_runtime_event_publisher(self)

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        if not turn_continuation.should_persist_user_message(msg.metadata):
            return False
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = ({"media": list(media_paths)} if media_paths else {}) | agent_context.session_extra(msg.metadata)
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
        include_memory_recent_history: bool = True,
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        scope = self.workspace_scopes.for_message(msg, session.metadata)
        selected_skills = self.context.skills.selected_available_names(msg.metadata)
        return self.context.build_messages(
            history=history,
            current_message=plan_only_prompt(
                image_generation_prompt(msg.content, msg.metadata),
                msg.metadata,
            ),
            skill_names=selected_skills,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
            workspace=scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            include_memory_recent_history=include_memory_recent_history,
        )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        child_cancellation = asyncio.create_task(self.subagents.cancel_by_session(key))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        sub_cancelled = await child_cancellation
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
        )
        hook: AgentHook = loop_hook
        if not ephemeral and self._extra_hooks:
            hook = CompositeHook([loop_hook] + self._extra_hooks)

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            Only messages already available are injected. Background subagents
            report through the bus when they finish; the parent Runner must not
            occupy the foreground turn while waiting for them.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any] | None:
                if pending_msg.metadata.get("interaction_response") is True:
                    request_id = pending_msg.metadata.get("interaction_request_id")
                    if isinstance(request_id, str):
                        try:
                            request = self.interactions.get(request_id)
                        except Exception:
                            request = None
                        if request is not None and request.child_id:
                            return None
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = self._prepare_message_media(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                return {"role": "user", "content": user_content}

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    normalized = _to_user_message(pending_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
                if normalized is not None:
                    items.append(normalized)

            return items

        active_session_key = session.key if session else session_key
        effective_scope = self.workspace_scopes.for_turn(
            channel=channel,
            message_metadata=metadata,
            session_metadata=session.metadata if session is not None else None,
        )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=active_session_key,
            metadata=dict(metadata or {}),
        )
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        request_token = bind_request_context(request_ctx)
        workspace_token = bind_workspace_scope(effective_scope)
        # Build continuation message that embeds the active goal objective so
        # the LLM can see it even if earlier Runtime Context was truncated.
        _goal_lines = goal_state_runtime_lines(session.metadata if session is not None else None)
        _goal_continue = (
            "You have an active sustained goal:\n\n"
            + "\n".join(_goal_lines)
            + "\n\nPlease continue working toward the objective using your tools, "
            "or call complete_goal if the work is truly finished."
        ) if _goal_lines else SUSTAINED_GOAL_CONTINUE_PROMPT
        session_metadata = session.metadata if session is not None else None
        plan = session_metadata.get("plan_state") if isinstance(session_metadata, dict) else None
        if not isinstance(plan, dict):
            plan = {}
        task_id = str(plan.get("task_id")) if plan.get("task_id") else None
        plan_hash = str(plan.get("plan_hash")) if plan.get("plan_hash") else None
        sandbox_mode = sandbox_mode_for_scope(
            effective_scope,
            plan_only=execution_mode_from_metadata(metadata) == EXECUTION_MODE_PLAN_ONLY,
        )
        trace_task_id = task_id or (active_session_key or "ephemeral").replace(":", "_")
        webui_turn_id = metadata.get("webui_turn_id") if isinstance(metadata, dict) else None
        if not isinstance(webui_turn_id, str) or not webui_turn_id:
            webui_turn_id = None
        initial_trace_events = []
        if session is not None:
            queued = session.metadata.pop(self._RUNTIME_TRACE_EVENTS_KEY, [])
            if isinstance(queued, list):
                initial_trace_events = [item for item in queued if isinstance(item, dict)]
        trace_workspace = effective_scope.project_path
        if not isinstance(trace_workspace, Path):
            trace_workspace = self.workspace if isinstance(self.workspace, Path) else None
        if trace_workspace is not None:
            if self.observability is not None:
                from nanobot.runtime.langfuse_hook import LangfuseTraceHook

                trace_hook = LangfuseTraceHook(
                    self.observability,
                    task_id=trace_task_id,
                    actor="main",
                    model=self.model,
                    session_id=active_session_key,
                    turn_id=webui_turn_id,
                    plan_hash=plan_hash,
                    sandbox_mode=getattr(sandbox_mode, "value", str(sandbox_mode)),
                    initial_events=initial_trace_events,
                )
            else:
                trace_hook = TraceHook(
                    trace_workspace / ".nanobot-runtime" / "trace" / f"{trace_task_id}.jsonl",
                    task_id=trace_task_id,
                    actor="main",
                    model=self.model,
                    session_id=active_session_key,
                    turn_id=webui_turn_id,
                    initial_events=initial_trace_events,
                )
            if isinstance(hook, CompositeHook):
                hook = CompositeHook([*hook._hooks, trace_hook])
            else:
                hook = CompositeHook([hook, trace_hook])

        async def _policy_gate(*, tool_call, tool, params, spec) -> PolicyGateOutcome:
            if tool.name == "request_user_input":
                raw_strategy = str(params.get("strategy") or "required")
                strategy = InteractionStrategy(raw_strategy)
                expires_at = None
                if strategy == InteractionStrategy.AUTO_RESOLVE:
                    timeout_seconds = int(params.get("timeout_seconds") or 60)
                    expires_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
                    ).isoformat()
                payload = {"chat_id": chat_id}
                if params.get("default") is not None:
                    payload["default"] = params["default"]
                request = self.interactions.create(
                    kind=InteractionKind.QUESTION,
                    strategy=strategy,
                    task_id=task_id,
                    turn_id=message_id,
                    plan_hash=plan_hash,
                    tool_call_id=tool_call.id,
                    continuation={"tool_name": tool.name},
                    payload=payload,
                    questions=list(params.get("questions") or []),
                    expires_at=expires_at,
                )
                interaction = request.as_dict()
                emit_trace_event("mybot.interaction.requested", {
                    "kind": request.kind.value,
                    "strategy": request.strategy.value,
                    "request_id": request.request_id,
                })
                await self.bus.publish_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content="Waiting for your input.",
                    metadata={
                        "_progress": True,
                        OUTBOUND_META_AGENT_UI: {
                            "kind": "interaction_request",
                            "interaction": interaction,
                        },
                    },
                ))
                return PolicyGateOutcome(
                    decision=PermissionDecision(
                        action="ask",
                        reason="the task is waiting for typed user input",
                        matched_rules=("interaction.question",),
                        risk_level="low",
                    ),
                    interaction=interaction,
                )
            decision = self.policy.evaluate(
                tool=tool,
                params=params,
                scope=effective_scope,
                sandbox_mode=sandbox_mode,
                task_id=task_id,
                plan_hash=plan_hash,
                child_id=None,
            )
            emit_trace_event("mybot.policy.decision", {
                "tool_name": tool.name,
                **decision.as_dict(),
            })
            if decision.action != "ask":
                return PolicyGateOutcome(decision=decision)

            params_digest = normalized_params_hash(params)
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
                normalized_params_hash=params_digest,
                task_id=task_id,
                plan_hash=plan_hash,
                step_id=None,
                child_id=None,
                target=decision.target,
                risk=decision.risk_level,
                reason=decision.reason,
                sandbox_mode=sandbox_mode.value,
                chat_id=chat_id,
                provider=effective_scope.sandbox_status.provider,
                command_hash=command_hash(raw_command) if raw_command else None,
                writable_roots=(str(effective_scope.project_path),),
                network_domains=network_domains,
                ports=network_ports,
                network_addresses=network_addresses,
            )
            approved = self.approvals.find_approved(binding)
            if approved is not None:
                self.interactions.consume(
                    approved.request_id,
                    expected_revision=approved.revision,
                    idempotency_key=f"tool:{tool_call.id}",
                )
                return PolicyGateOutcome(
                    decision=PermissionDecision(
                        action="allow",
                        reason="matched a parameter-bound one-shot approval",
                        matched_rules=("approval.one_shot",),
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
                turn_id=message_id,
            )
            interaction = request.as_dict()
            emit_trace_event("mybot.interaction.requested", {
                "kind": request.kind.value,
                "strategy": request.strategy.value,
                "request_id": request.request_id,
                "tool_name": tool.name,
            })
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=decision.reason,
                metadata={
                    "_progress": True,
                    OUTBOUND_META_AGENT_UI: {
                        "kind": "interaction_request",
                        "interaction": interaction,
                    },
                },
            ))
            return PolicyGateOutcome(decision=decision, interaction=interaction)
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=tools or self.tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=effective_scope.project_path,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                # Sustained goals may legitimately exceed NANOBOT_LLM_TIMEOUT_S; idle stall
                # is still capped by NANOBOT_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=session_metadata,
                    message_metadata=metadata,
                ),
                goal_active_predicate=lambda: sustained_goal_active(session.metadata) if session is not None else False,
                goal_continue_message=_goal_continue,
                policy_gate=_policy_gate,
                actor="main",
                task_id=task_id,
                plan_hash=plan_hash,
            ))
        finally:
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            should_stream = turn_continuation.should_stream_budget_response(
                stop_reason=result.stop_reason,
                pending_queue_available=pending_queue is not None and session is not None,
                session_metadata=session_metadata,
                message_metadata=metadata,
            )
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end and should_stream:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                await self._resume_expired_interactions()
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            effective_key = self._effective_session_key(msg)
            if await agent_context.handle_runtime_control(self, msg, self.tools):
                continue
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, effective_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _resume_expired_interactions(self) -> None:
        """Resolve due deadlines and resume their original WebUI session once."""
        for request in self.interactions.expire_due():
            payload = request.payload if isinstance(request.payload, dict) else {}
            binding = payload.get("binding") if isinstance(payload.get("binding"), dict) else {}
            chat_id = payload.get("chat_id") or binding.get("chat_id")
            if not isinstance(chat_id, str) or not chat_id:
                continue
            await self.bus.publish_outbound(OutboundMessage(
                channel="websocket",
                chat_id=chat_id,
                content="",
                metadata={
                    "_progress": True,
                    OUTBOUND_META_AGENT_UI: {
                        "kind": "interaction_updated",
                        "interaction": request.as_dict(),
                    },
                },
            ))
            await self.bus.publish_inbound(InboundMessage(
                channel="websocket",
                sender_id="runtime",
                chat_id=chat_id,
                content=(
                    f"[Interaction deadline for {request.request_id}: "
                    f"{request.status.value}; resolution={request.resolution}]"
                ),
                metadata={
                    "webui": True,
                    "interaction_response": True,
                    "interaction_request_id": request.request_id,
                },
            ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        pending: asyncio.Queue | None = None
        try:
            async with lock, gate:
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                pending = asyncio.Queue(maxsize=20)
                self._pending_queues[session_key] = pending
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    completed_channel = msg.channel
                    completed_chat_id = msg.chat_id
                    if response is not None:
                        await self.bus.publish_outbound(response)
                        completed_channel = response.channel
                        completed_chat_id = response.chat_id
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                    continuing = turn_continuation.internal_continuation_pending(msg.metadata)
                    if not continuing:
                        await self._runtime_events().turn_completed(
                            channel=completed_channel,
                            chat_id=completed_chat_id,
                            session_key=session_key,
                            metadata=msg.metadata,
                        )
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        plan = session.metadata.get("plan_state")
                        if isinstance(plan, dict) and self.checkpoints.eligible(plan):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Preserved durable planned-task checkpoint for cancelled session {}",
                                key,
                            )
                        elif self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        await self._runtime_events().turn_completed(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            session_key=session_key,
                            metadata=msg.metadata,
                        )
                finally:
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self._pending_queues.get(session_key) is pending:
                        queue = self._pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover, session_key,
                            )
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        await self._runtime_events().run_status_changed(
                            msg, session_key, "idle"
                        )
                        self._runtime_events().clear_turn(session_key)
        finally:
            if pending is None:
                await self._runtime_events().run_status_changed(
                    msg, session_key, "idle"
                )
                self._runtime_events().clear_turn(session_key)

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()
        if self.observability is not None:
            observability = self.observability
            self.observability = None
            self.runner.observability = None
            self.subagents.observability = None
            observability.release()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        await self.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        self._set_tool_context(
            channel, chat_id, msg.metadata.get("message_id"),
            msg.metadata, session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"
        workspace_scope = self.workspace_scopes.for_message(msg, session.metadata)
        selected_skills = self.context.skills.selected_available_names(msg.metadata)

        messages = self.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            skill_names=selected_skills,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
            workspace=workspace_scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            skip_runtime_lines=is_subagent,
        )
        t_wall = time.time()
        final_content, _, all_msgs, stop_reason, _ = await self._run_agent_loop(
            messages, session=session, channel=channel, chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._save_turn(session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms)
        self._runtime_events().record_turn_latency(key, latency_ms)
        session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._max_messages,
            )
        )
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if channel == "slack" and key.startswith("slack:") and key.count(":") >= 2:
            outbound_metadata["slack"] = {"thread_ts": key.split(":", 2)[2]}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        self._refresh_provider_snapshot()

        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        key = session_key or msg.session_key
        t0 = time.time()
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=f"{key}:{time.time_ns()}",
            turn_wall_started_at=t0,
            visible_run_started_at=turn_continuation.internal_continuation_run_started_at(
                msg.metadata,
            ),
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            ephemeral=ephemeral,
            tools=(
                plan_only_registry(tools or self.tools)
                if execution_mode_from_metadata(msg.metadata) == EXECUTION_MODE_PLAN_ONLY
                else tools
            ),
        )

        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return ctx.outbound

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents."""
        msg = ctx.msg

        if msg.media:
            original_media = list(msg.media)
            new_content, image_only = self._prepare_message_media(msg.content, msg.media)
            metadata = dict(msg.metadata or {})
            metadata["_runtime_input_paths"] = original_media
            ctx.msg = dataclasses.replace(
                msg,
                content=new_content,
                media=image_only,
                metadata=metadata,
            )
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        self._migrate_legacy_reflection_state(ctx.session)
        await self._runtime_events().session_turn_started(msg, ctx.session_key)
        self.workspace_scopes.persist_message_scope(ctx.session, msg)

        request_id = (
            msg.metadata.get("interaction_request_id")
            if msg.metadata.get("interaction_response") is True
            else None
        )
        if isinstance(request_id, str):
            try:
                request = self.interactions.get(request_id)
            except Exception:
                request = None
            if request is not None and request.child_id:
                ctx.suppress_response = True
                return "child_interaction"
            if self._materialize_interaction_response(ctx.session, request_id):
                ctx.msg = dataclasses.replace(msg, content="", media=[])
                msg = ctx.msg

        checkpoint, _ = self._runtime_checkpoint_snapshot(ctx.session)
        checkpoint_phase = str(checkpoint.get("phase") or "") if checkpoint else ""
        if checkpoint_phase == "awaiting_plan_confirmation":
            interaction = checkpoint.get("interaction") if isinstance(checkpoint, dict) else None
            request_id = interaction.get("request_id") if isinstance(interaction, dict) else None
            try:
                request = self.interactions.get(request_id) if isinstance(request_id, str) else None
            except Exception:
                request = None
            plan = ctx.session.metadata.get("plan_state")
            if (
                request is not None
                and request.kind == InteractionKind.PLAN_CONFIRMATION
                and request.status == InteractionStatus.PENDING
                and isinstance(plan, dict)
                and request.task_id == plan.get("task_id")
                and request.plan_hash == plan.get("plan_hash")
            ):
                cancelled = self.interactions.cancel(
                    request.request_id,
                    expected_revision=request.revision,
                    idempotency_key=f"plan-revision:{ctx.turn_id}",
                )
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata={
                        "_progress": True,
                        OUTBOUND_META_AGENT_UI: {
                            "kind": "interaction_updated",
                            "interaction": cancelled.as_dict(),
                        },
                    },
                ))
                self._materialize_interaction_response(ctx.session, request.request_id)
                self._restore_runtime_checkpoint(ctx.session)
                revision_metadata = dict(msg.metadata or {})
                revision_metadata[EXECUTION_MODE_METADATA_KEY] = EXECUTION_MODE_PLAN_ONLY
                revision_metadata[PLAN_REVISION_REQUEST_METADATA_KEY] = True
                ctx.msg = dataclasses.replace(msg, metadata=revision_metadata)
                ctx.tools = plan_only_registry(ctx.tools or self.tools)
                emit_trace_event("mybot.plan.confirmation_superseded_for_revision", {
                    "task_id": request.task_id,
                    "plan_hash": request.plan_hash,
                    "request_id": request.request_id,
                })
                return "ok"
        if checkpoint_phase in {
            "awaiting_question",
            "awaiting_approval",
            "awaiting_plan_confirmation",
            "awaiting_recovery_decision",
        }:
            ctx.outbound = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "This task is waiting for the typed interaction card to be resolved; "
                    "ordinary chat cannot consume or bypass it."
                ),
                metadata=dict(msg.metadata or {}),
            )
            return "waiting"
        if await self._prepare_uncertain_recovery(
            ctx.session,
            channel=msg.channel,
            chat_id=msg.chat_id,
            turn_id=msg.metadata.get("message_id"),
        ):
            ctx.suppress_response = True
            return "waiting"

        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"

    def _prepare_message_media(self, content: str, media: list[str]) -> tuple[str, list[str]]:
        if self._should_extract_document_text():
            return extract_documents(content, media)
        return reference_non_image_attachments(content, media)

    def _should_extract_document_text(self) -> bool:
        if self.channels_config is None:
            return True
        return self.channels_config.extract_document_text

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
        ctx.pending_summary = pending
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message(
                    "assistant", result.content, _command=True
                )
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self, ctx: TurnContext) -> str:
        if not ctx.ephemeral:
            await self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._runtime_events().record_turn_runtime(
            ctx.session_key,
            self.llm_runtime(),
        )

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg,
            ctx.session,
            ctx.history,
            ctx.pending_summary,
            include_memory_recent_history=not ctx.ephemeral,
        )
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        if ctx.visible_run_started_at is None:
            ctx.visible_run_started_at = time.time()
        await self._runtime_events().run_status_changed(
            ctx.msg,
            ctx.session_key,
            "running",
            started_at=ctx.visible_run_started_at,
        )
        result = await self._run_agent_loop(
            ctx.initial_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            pending_queue=ctx.pending_queue,
            ephemeral=ctx.ephemeral,
            tools=ctx.tools,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        if stop_reason.startswith("awaiting_"):
            ctx.suppress_response = True
            return "suspended"
        await turn_continuation.maybe_continue_turn(ctx)
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        turn_continuation.prepare_save_boundary(ctx)

        if (
            (ctx.final_content is None or not ctx.final_content.strip())
            and not ctx.suppress_response
        ):
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        latency_started_at = (
            ctx.visible_run_started_at
            if turn_continuation.internal_continuation_inbound(ctx.msg.metadata)
            and ctx.visible_run_started_at is not None
            else ctx.turn_wall_started_at
        )
        ctx.turn_latency_ms = max(0, int((time.time() - latency_started_at) * 1000))
        self._save_turn(
            ctx.session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        self._runtime_events().record_turn_latency(
            ctx.session_key,
            ctx.turn_latency_ms,
        )
        if not ctx.ephemeral:
            ctx.session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
            self._schedule_background(
                self.consolidator.maybe_consolidate_by_tokens(
                    ctx.session,
                    replay_max_messages=self._max_messages,
                )
            )
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        if ctx.suppress_response:
            ctx.outbound = None
            return "ok"
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.ephemeral and ctx.outbound is not None:
            ctx.outbound.metadata["_stop_reason"] = ctx.stop_reason
        return "ok"

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _migrate_legacy_reflection_state(self, session: Session) -> bool:
        plan = session.metadata.get("plan_state")
        if not isinstance(plan, dict) or plan.get("status") not in {
            "reviewing",
            "awaiting_reflection_decision",
        }:
            return False

        task_id = str(plan.get("task_id") or "")
        if not task_id:
            return False
        for request in self.interactions.list_pending(task_id=task_id or None):
            if request.kind != InteractionKind.REFLECTION_DECISION:
                continue
            self.interactions.cancel(
                request.request_id,
                expected_revision=request.revision,
                idempotency_key=f"reflection-removed:{request.request_id}",
            )

        now = datetime.now().astimezone().isoformat()
        plan["status"] = "completed"
        plan["completed_at"] = str(plan.get("completed_at") or now)
        plan["updated_at"] = now
        for key in (
            "interaction_request_id",
            "reflection",
            "reflection_attempts",
            "reflection_findings",
        ):
            plan.pop(key, None)
        session.metadata["plan_state"] = plan

        path = (
            self.workspace
            / ".nanobot-runtime"
            / "artifacts"
            / task_id
            / "plan.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
        self.checkpoints.artifacts.register(
            task_id=task_id,
            artifact_id="plan",
            path=path,
            type="plan",
            source_artifacts=list(plan.get("input_artifacts") or []),
            status="completed",
        )

        runner = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if isinstance(runner, dict):
            runner["phase"] = "tools_completed"
            runner.pop("interaction", None)
            completed = runner.get("completed_tool_results")
            if isinstance(completed, list):
                for item in reversed(completed):
                    if isinstance(item, dict) and item.get("name") == "plan":
                        item["content"] = json.dumps({
                            "path": str(path),
                            "plan": plan,
                            "verification": {"passed": True, "missing": []},
                        }, ensure_ascii=False)
                        break
            self._set_runtime_checkpoint(session, runner)
        else:
            self.sessions.save(session)
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)
        plan = session.metadata.get("plan_state")
        if isinstance(plan, dict):
            task_id = str(plan.get("task_id") or "")
            interactions = [
                request.request_id
                for request in self.interactions.list_pending(task_id=task_id or None)
            ]
            self.checkpoints.write(
                plan=plan,
                runner_payload=payload,
                session_key=session.key,
                interactions=interactions,
            )

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)
        plan = session.metadata.get("plan_state")
        if isinstance(plan, dict) and plan.get("task_id"):
            self.checkpoints.delete(str(plan["task_id"]))

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _queue_runtime_trace_event(
        self,
        session: Session,
        name: str,
        attributes: dict[str, Any],
    ) -> None:
        queued = session.metadata.setdefault(self._RUNTIME_TRACE_EVENTS_KEY, [])
        if not isinstance(queued, list):
            queued = []
            session.metadata[self._RUNTIME_TRACE_EVENTS_KEY] = queued
        queued.append({"name": name, "attributes": dict(attributes)})
        self.sessions.save(session)

    def _runtime_checkpoint_snapshot(
        self,
        session: Session,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return the runner checkpoint and its validated durable envelope, if any."""
        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if isinstance(checkpoint, dict):
            plan = session.metadata.get("plan_state")
            if isinstance(plan, dict) and self.checkpoints.eligible(plan):
                try:
                    durable = self.checkpoints.load(
                        str(plan.get("task_id")),
                        expected_plan_hash=str(plan.get("plan_hash")),
                    )
                except CheckpointError as exc:
                    logger.error("Durable checkpoint rejected: {}: {}", exc.code, exc.message)
                else:
                    return checkpoint, durable
            return checkpoint, None
        plan = session.metadata.get("plan_state")
        if not isinstance(plan, dict) or not self.checkpoints.eligible(plan):
            return None, None
        try:
            durable = self.checkpoints.load(
                str(plan.get("task_id")),
                expected_plan_hash=str(plan.get("plan_hash")),
            )
        except CheckpointError as exc:
            logger.error("Durable checkpoint rejected: {}: {}", exc.code, exc.message)
            return None, None
        runner = durable.get("runner")
        if not isinstance(runner, dict):
            return None, durable
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = runner
        return runner, durable

    def _materialize_interaction_response(
        self,
        session: Session,
        request_id: str,
    ) -> bool:
        """Replace an awaiting tool result with its typed durable resolution."""
        try:
            request = self.interactions.get(request_id)
        except Exception:
            logger.exception("Could not load interaction response {}", request_id)
            return False
        if request.status == InteractionStatus.PENDING:
            return False

        node_recovery_applied = False
        if (
            request.kind == InteractionKind.RECOVERY_DECISION
            and request.payload.get("uncertain_node_ids")
        ):
            node_recovery_applied = self._apply_plan_node_recovery_decision(session, request)
        checkpoint, _ = self._runtime_checkpoint_snapshot(session)
        if not isinstance(checkpoint, dict):
            if node_recovery_applied:
                try:
                    self.interactions.consume(
                        request.request_id,
                        expected_revision=request.revision,
                        idempotency_key=f"resume:{session.key}:{request.request_id}",
                    )
                except Exception:
                    logger.exception("Could not consume resolved interaction {}", request_id)
                    return False
                emit_trace_event("mybot.interaction.resumed", {
                    "request_id": request.request_id,
                    "kind": request.kind.value,
                    "status": request.status.value,
                })
                return True
            return False

        interaction = checkpoint.get("interaction")
        if not isinstance(interaction, dict) or interaction.get("request_id") != request_id:
            return False
        completed = checkpoint.get("completed_tool_results")
        if not isinstance(completed, list):
            return False

        resolution = {
            "request_id": request.request_id,
            "kind": request.kind.value,
            "status": request.status.value,
            "task_id": request.task_id,
            "plan_hash": request.plan_hash,
            "response": request.response,
            "resolution": request.resolution,
        }
        replaced = False
        for item in reversed(completed):
            if (
                isinstance(item, dict)
                and (
                    item.get("tool_call_id") == request.tool_call_id
                    or (
                        request.tool_call_id is None
                        and request.kind == InteractionKind.PLAN_CONFIRMATION
                        and item.get("name") == "plan"
                    )
                )
            ):
                item["content"] = json.dumps(resolution, ensure_ascii=False)
                replaced = True
                break
        if not replaced and request.kind == InteractionKind.RECOVERY_DECISION:
            pending = checkpoint.get("pending_tool_calls")
            uncertain_ids = {
                str(item)
                for item in request.payload.get("uncertain_tool_call_ids", [])
            }
            if isinstance(pending, list):
                remaining: list[dict[str, Any]] = []
                for call in pending:
                    if not isinstance(call, dict) or str(call.get("id") or "") not in uncertain_ids:
                        if isinstance(call, dict):
                            remaining.append(call)
                        continue
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    completed.append({
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": function.get("name") or "tool",
                        "content": json.dumps(resolution, ensure_ascii=False),
                    })
                    replaced = True
                checkpoint["pending_tool_calls"] = remaining
        replaced = replaced or node_recovery_applied
        if not replaced:
            return False

        checkpoint["phase"] = "tools_completed"
        checkpoint.pop("interaction", None)
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = checkpoint
        self._set_runtime_checkpoint(session, checkpoint)
        consume_here = (
            request.kind in {
                InteractionKind.QUESTION,
                InteractionKind.RECOVERY_DECISION,
            }
            or (
                request.kind == InteractionKind.APPROVAL
                and request.status != InteractionStatus.APPROVED
            )
        )
        if consume_here:
            try:
                self.interactions.consume(
                    request.request_id,
                    expected_revision=request.revision,
                    idempotency_key=f"resume:{session.key}:{request.request_id}",
                )
            except Exception:
                logger.exception("Could not consume resolved interaction {}", request_id)
                return False
        try:
            created_at = datetime.fromisoformat(request.created_at)
            resolved_at = datetime.fromisoformat(request.resolved_at or datetime.now(timezone.utc).isoformat())
            human_wait_ms = max(0, int((resolved_at - created_at).total_seconds() * 1000))
        except (TypeError, ValueError):
            human_wait_ms = 0
        trace_attributes = {
            "request_id": request.request_id,
            "kind": request.kind.value,
            "status": request.status.value,
            "mybot.human_wait_ms": human_wait_ms,
        }
        self._queue_runtime_trace_event(
            session,
            "mybot.interaction.resumed",
            trace_attributes,
        )
        emit_trace_event("mybot.interaction.resumed", trace_attributes)
        return True

    def _apply_plan_node_recovery_decision(self, session: Session, request: Any) -> bool:
        plan = session.metadata.get("plan_state")
        if not isinstance(plan, dict) or plan.get("plan_hash") != request.plan_hash:
            return False
        answer = str((request.response or {}).get("answer") or "").strip().lower()
        target_status = {
            "retry": "ready",
            "mark completed": "succeeded",
            "cancel": "cancelled",
        }.get(answer)
        if target_status is None:
            return False
        node_ids = {
            str(item)
            for item in request.payload.get("uncertain_node_ids", [])
            if str(item)
        }
        changed = False
        for node_id in node_ids:
            step = next((
                item
                for item in plan.get("steps", [])
                if isinstance(item, dict) and str(item.get("id")) == node_id
            ), None)
            if not isinstance(step, dict) or step.get("status") != "uncertain":
                continue
            try:
                PlanScheduler.transition(plan, node_id, target_status)
            except Exception:
                logger.exception("Could not recover plan node {}", node_id)
                return False
            step["recovery_decision"] = answer
            changed = True
        if not changed:
            return False
        plan["status"] = "active"
        plan["updated_at"] = datetime.now().astimezone().isoformat()
        session.metadata["plan_state"] = plan
        self.sessions.save(session)
        path = (
            self.workspace
            / ".nanobot-runtime"
            / "artifacts"
            / str(plan["task_id"])
            / "plan.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
        self.checkpoints.artifacts.register(
            task_id=str(plan["task_id"]),
            artifact_id="plan",
            path=path,
            type="plan",
            source_artifacts=list(plan.get("input_artifacts") or []),
            status=str(plan.get("status") or "active"),
        )
        return True

    def _reconcile_plan_nodes(self, session: Session) -> str | None:
        plan = session.metadata.get("plan_state")
        if not isinstance(plan, dict) or plan.get("status") != "active":
            return None
        if int(plan.get("schema_version") or 1) < 2 or not isinstance(plan.get("steps"), list):
            return None
        if contract_hash(plan) != plan.get("plan_hash"):
            plan["recovery_error"] = "Stored plan hash does not match the structured plan."
            self.sessions.save(session)
            return str(plan["recovery_error"])

        task_id = str(plan.get("task_id") or "")
        try:
            records = self.checkpoints.artifacts.list(task_id)
        except Exception as exc:
            plan["recovery_error"] = f"Artifact index validation failed: {exc}"
            self.sessions.save(session)
            return str(plan["recovery_error"])
        records_by_path = {str(Path(record.path).resolve()): record for record in records}
        task_root = self.checkpoints.artifacts.task_root(task_id)
        markdown = plan.get("plan_markdown")
        if isinstance(markdown, dict):
            artifact_id = str(markdown.get("artifact_id") or "")
            record = next((item for item in records if item.artifact_id == artifact_id), None)
            if not (
                record is not None
                and record.checksum == markdown.get("checksum")
                and record.path == str(Path(str(markdown.get("path") or "")).resolve())
                and self.checkpoints.artifacts.verify(record)
            ):
                plan["recovery_error"] = "Immutable plan Markdown failed checksum validation."
                self.sessions.save(session)
                return str(plan["recovery_error"])

        def _artifacts_verified(step: dict[str, Any]) -> bool:
            expected = [str(item) for item in step.get("expected_artifacts", [])]
            if not expected:
                return False
            roots = [task_root]
            if isinstance(step.get("artifact_root"), str):
                roots.insert(0, Path(step["artifact_root"]).resolve())
            for relative in expected:
                verified = False
                for root in roots:
                    candidate = (root / relative).resolve()
                    try:
                        candidate.relative_to(root.resolve())
                    except ValueError:
                        continue
                    record = records_by_path.get(str(candidate))
                    if record is not None and self.checkpoints.artifacts.verify(record):
                        verified = True
                        break
                if not verified:
                    return False
            return True

        before = json.dumps(plan.get("steps", []), ensure_ascii=False, sort_keys=True)
        PlanScheduler.recover_running(
            plan,
            live_child_ids=self.subagents.get_running_task_ids(),
            artifacts_verified=_artifacts_verified,
        )
        after = json.dumps(plan.get("steps", []), ensure_ascii=False, sort_keys=True)
        if before == after:
            return None
        plan["updated_at"] = datetime.now().astimezone().isoformat()
        session.metadata["plan_state"] = plan
        self.sessions.save(session)
        path = task_root / "plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
        self.checkpoints.artifacts.register(
            task_id=task_id,
            artifact_id="plan",
            path=path,
            type="plan",
            source_artifacts=list(plan.get("input_artifacts") or []),
            status=str(plan.get("status") or "active"),
        )
        runner = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if isinstance(runner, dict):
            self.checkpoints.write(
                plan=plan,
                runner_payload=runner,
                session_key=session.key,
                interactions=[
                    item.request_id
                    for item in self.interactions.list_pending(task_id=task_id)
                ],
            )
        summary = PlanScheduler.recovery_summary(plan)
        emit_trace_event("mybot.plan.recovered", {
            "task_id": task_id,
            "plan_hash": plan.get("plan_hash"),
            "recovery": {
                "completed": list(summary.completed),
                "pending": list(summary.pending),
                "uncertain": list(summary.uncertain),
            },
        })
        return None

    async def _prepare_uncertain_recovery(
        self,
        session: Session,
        *,
        channel: str,
        chat_id: str,
        turn_id: str | None,
    ) -> bool:
        recovery_error = self._reconcile_plan_nodes(session)
        plan = session.metadata.get("plan_state")
        if not isinstance(plan, dict) or not self.checkpoints.eligible(plan):
            return False
        if recovery_error:
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=f"Runtime recovery stopped: {recovery_error}",
                metadata={"_progress": True},
            ))
            return True
        durable = None
        try:
            durable = self.checkpoints.load(
                str(plan.get("task_id")),
                expected_plan_hash=str(plan.get("plan_hash")),
            )
        except CheckpointError:
            durable = None
        recovery = self.checkpoints.recovery_plan(durable) if durable is not None else None
        uncertain_tools = tuple(recovery.uncertain) if recovery is not None else ()
        uncertain_nodes = tuple(
            str(step.get("id"))
            for step in plan.get("steps", [])
            if isinstance(step, dict) and step.get("status") == "uncertain"
        )
        if not uncertain_tools and not uncertain_nodes:
            return False

        existing = next((
            request
            for request in self.interactions.list_pending(task_id=str(plan.get("task_id")))
            if request.kind == InteractionKind.RECOVERY_DECISION
            and set(request.payload.get("uncertain_tool_call_ids", [])) == set(uncertain_tools)
            and set(request.payload.get("uncertain_node_ids", [])) == set(uncertain_nodes)
        ), None)
        request = existing or self.interactions.create(
            kind=InteractionKind.RECOVERY_DECISION,
            strategy=InteractionStrategy.REQUIRED,
            task_id=str(plan.get("task_id")),
            turn_id=turn_id,
            plan_hash=str(plan.get("plan_hash")),
            tool_call_id=uncertain_tools[0] if uncertain_tools else None,
            continuation={"action": "recover_uncertain_runtime_state"},
            payload={
                "chat_id": chat_id,
                "uncertain_tool_call_ids": list(uncertain_tools),
                "uncertain_node_ids": list(uncertain_nodes),
                "checkpoint_state_hash": durable.get("state_hash") if durable else None,
            },
            questions=[{
                "id": "recovery_action",
                "header": "Recovery",
                "header_i18n_key": "thread.interaction.recovery.header",
                "question": (
                    "Interrupted work has an uncertain outcome. Check the target, then choose "
                    "whether to retry, mark it completed, or cancel the affected node."
                ),
                "question_i18n_key": "thread.interaction.recovery.question",
                "options": [
                    {
                        "label": "Retry",
                        "label_i18n_key": "thread.interaction.recovery.retry",
                        "description": "Retry only after checking the target.",
                        "description_i18n_key": (
                            "thread.interaction.recovery.retryDescription"
                        ),
                    },
                    {
                        "label": "Mark completed",
                        "label_i18n_key": "thread.interaction.recovery.markCompleted",
                        "description": "Do not repeat the side effect.",
                        "description_i18n_key": (
                            "thread.interaction.recovery.markCompletedDescription"
                        ),
                    },
                    {
                        "label": "Cancel",
                        "label_i18n_key": "thread.interaction.recovery.cancel",
                        "description": "Stop this recovery.",
                        "description_i18n_key": (
                            "thread.interaction.recovery.cancelDescription"
                        ),
                    },
                ],
            }],
        )
        runner = durable.get("runner") if durable is not None else session.metadata.get(
            self._RUNTIME_CHECKPOINT_KEY
        )
        if isinstance(runner, dict):
            runner["phase"] = "awaiting_recovery_decision"
            runner["interaction"] = request.as_dict()
            session.metadata[self._RUNTIME_CHECKPOINT_KEY] = runner
            self._set_runtime_checkpoint(session, runner)
        await self.bus.publish_outbound(OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content="Recovery confirmation is required before any uncertain side effect is retried.",
            metadata={
                "_progress": True,
                OUTBOUND_META_AGENT_UI: {
                    "kind": "interaction_request",
                    "interaction": request.as_dict(),
                },
            },
        ))
        trace_attributes = {
            "request_id": request.request_id,
            "uncertain_tool_call_ids": list(uncertain_tools),
            "uncertain_node_ids": list(uncertain_nodes),
        }
        self._queue_runtime_trace_event(
            session,
            "mybot.recovery.awaiting_decision",
            trace_attributes,
        )
        emit_trace_event("mybot.recovery.awaiting_decision", trace_attributes)
        return True

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint, durable = self._runtime_checkpoint_snapshot(session)
        if not isinstance(checkpoint, dict):
            return False

        recovery = self.checkpoints.recovery_plan(durable) if durable is not None else None
        pending_ids = set(recovery.pending) if recovery is not None else set()
        uncertain_ids = set(recovery.uncertain) if recovery is not None else set()

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            if str(tool_id) in uncertain_ids:
                recovery_payload = {
                    "status": "uncertain",
                    "safe_to_retry": False,
                    "reason": "external side effect requires an explicit recovery decision",
                }
            else:
                recovery_payload = {
                    "status": "pending_recovery",
                    "safe_to_retry": str(tool_id) in pending_ids or recovery is None,
                    "reason": "tool had not completed before interruption",
                }
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": json.dumps(recovery_payload, ensure_ascii=False),
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [], metadata=dict(metadata or {}),
        )
        # Share the dispatch lock so direct calls serialize with bus turns.
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        try:
            async with lock:
                kwargs: dict[str, Any] = {
                    "session_key": session_key,
                    "on_progress": on_progress,
                    "on_stream": on_stream,
                    "on_stream_end": on_stream_end,
                    "ephemeral": ephemeral,
                }
                if tools is not None:
                    kwargs["tools"] = tools
                return await self._process_message(
                    msg,
                    **kwargs,
                )
        finally:
            await self._runtime_events().run_status_changed(msg, session_key, "idle")
            self._runtime_events().clear_turn(session_key)
