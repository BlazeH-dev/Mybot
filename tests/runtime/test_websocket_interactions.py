from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.runtime.interactions import (
    InteractionKind,
    InteractionStatus,
    InteractionStrategy,
)
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services


@pytest.mark.asyncio
async def test_typed_websocket_interaction_response_resumes_original_chat(tmp_path: Path) -> None:
    bus = MessageBus()
    cfg = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "websocketRequiresToken": False,
    })
    gateway = build_gateway_services(
        config=cfg,
        bus=bus,
        session_manager=SessionManager(tmp_path),
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=True,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(cfg, bus, gateway=gateway)
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    channel.send_interaction_updated = AsyncMock()  # type: ignore[method-assign]
    connection = MagicMock()
    connection.send = AsyncMock()
    connection.remote_address = ("127.0.0.1", 12345)
    request = gateway.interactions.create(
        kind=InteractionKind.APPROVAL,
        strategy=InteractionStrategy.EXPIRE_AND_DENY,
        payload={"chat_id": "chat-1", "binding": {"chat_id": "chat-1"}},
        expires_at="2099-01-01T00:00:00+00:00",
    )

    await channel._dispatch_envelope(connection, "client-1", {
        "type": "interaction_response",
        "chat_id": "chat-1",
        "request_id": request.request_id,
        "expected_revision": request.revision,
        "idempotency_key": "response-1",
        "response": {"approved": True},
    })

    updated = gateway.interactions.get(request.request_id)
    assert updated.status == InteractionStatus.APPROVED
    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["chat_id"] == "chat-1"
    assert kwargs["metadata"]["interaction_response"] is True


@pytest.mark.asyncio
async def test_ordinary_message_cannot_implicitly_approve(tmp_path: Path) -> None:
    bus = MessageBus()
    cfg = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "websocketRequiresToken": False,
    })
    gateway = build_gateway_services(
        config=cfg,
        bus=bus,
        session_manager=SessionManager(tmp_path),
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=True,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(cfg, bus, gateway=gateway)
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    connection = MagicMock()
    connection.send = AsyncMock()
    connection.remote_address = ("127.0.0.1", 12345)
    request = gateway.interactions.create(
        kind=InteractionKind.APPROVAL,
        strategy=InteractionStrategy.EXPIRE_AND_DENY,
        payload={"chat_id": "chat-1", "binding": {"chat_id": "chat-1"}},
        expires_at="2099-01-01T00:00:00+00:00",
    )
    await channel._dispatch_envelope(connection, "client-1", {
        "type": "message",
        "chat_id": "chat-1",
        "content": f"approve {request.request_id}",
        "webui": True,
    })
    assert gateway.interactions.get(request.request_id).status == InteractionStatus.PENDING
