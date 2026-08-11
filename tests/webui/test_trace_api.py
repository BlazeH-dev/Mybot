from __future__ import annotations

import json
import time
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.channels.websocket import WebSocketConfig
from nanobot.webui.gateway_services import build_gateway_services


@pytest.mark.asyncio
async def test_trace_route_reads_only_the_requested_session_turn(tmp_path, monkeypatch) -> None:
    services = build_gateway_services(
        config=WebSocketConfig(
            enabled=True,
            allowFrom=["*"],
            websocketRequiresToken=False,
        ),
        bus=MagicMock(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    services.tokens.api_tokens["tok"] = time.monotonic() + 300
    calls = []

    def fake_read(workspace, session_key, turn_id):
        calls.append((workspace, session_key, turn_id))
        return {
            "available": True,
            "source": "local",
            "session_key": session_key,
            "turn_id": turn_id,
            "trace_id": "trace-1",
            "trace_url": None,
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            "spans": [],
        }

    monkeypatch.setattr("nanobot.runtime.trace_reader.read_turn_trace", fake_read)
    encoded_key = quote("websocket:chat-1", safe="")
    request = Request(
        f"/api/sessions/{encoded_key}/trace?turn_id=turn%3A1",
        Headers([("Authorization", "Bearer tok")]),
    )

    response = await services.http.dispatch(MagicMock(), request)

    assert response is not None
    assert response.status_code == 200
    assert json.loads(response.body)["trace_id"] == "trace-1"
    assert calls == [(tmp_path, "websocket:chat-1", "turn:1")]


@pytest.mark.asyncio
async def test_trace_route_rejects_invalid_turn_id(tmp_path) -> None:
    services = build_gateway_services(
        config=WebSocketConfig(
            enabled=True,
            allowFrom=["*"],
            websocketRequiresToken=False,
        ),
        bus=MagicMock(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    services.tokens.api_tokens["tok"] = time.monotonic() + 300
    encoded_key = quote("websocket:chat-1", safe="")
    request = Request(
        f"/api/sessions/{encoded_key}/trace?turn_id=../../secret",
        Headers([("Authorization", "Bearer tok")]),
    )

    response = await services.http.dispatch(MagicMock(), request)

    assert response is not None
    assert response.status_code == 400
