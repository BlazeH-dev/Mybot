"""Contract tests for the FastAPI WebUI transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from nanobot.webui.asgi_gateway import create_gateway_app
from nanobot.webui.http_utils import http_json_response


class _Router:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def dispatch(self, connection: object, request: object) -> object:
        self.requests.append(request)
        return http_json_response({"path": request.path, "method": request.method})


def _client() -> tuple[TestClient, _Router]:
    router = _Router()
    channel = SimpleNamespace(
        config=SimpleNamespace(path="/ws", token="", websocket_requires_token=False),
        gateway=SimpleNamespace(http=router, tokens=MagicMock()),
        logger=MagicMock(),
        is_allowed=lambda _client_id: True,
    )
    return TestClient(create_gateway_app(channel)), router


def test_skill_analysis_uses_validated_json_body() -> None:
    client, router = _client()
    response = client.post(
        "/api/skill-evolution/analyze",
        json={
            "run_id": "run-1",
            "threshold": 0.5,
            "source_model_preset": "source",
            "optimizer_preset": "optimizer",
            "case_ids": ["case-1", "case-2"],
        },
    )

    assert response.status_code == 200
    request = router.requests[-1]
    assert request.method == "POST"
    assert request.path.startswith("/api/skill-evolution/analyze?")
    assert "case_ids=%5B%22case-1%22%2C%22case-2%22%5D" in request.path
    assert response.headers.get("Deprecation") is None


def test_skill_analysis_rejects_invalid_json() -> None:
    client, router = _client()
    response = client.post("/api/skill-evolution/analyze", json={"run_id": "run-1"})

    assert response.status_code == 422
    assert response.json()["detail"]
    assert not router.requests


def test_legacy_query_write_is_supported_with_deprecation_headers() -> None:
    client, router = _client()
    response = client.post(
        "/api/skill-evolution/analyze",
        params={
            "run_id": "run-1",
            "threshold": "0.5",
            "source_model_preset": "source",
            "optimizer_preset": "optimizer",
            "case_ids": json.dumps(["case-1"]),
        },
    )

    assert response.status_code == 200
    assert router.requests[-1].method == "POST"
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Warning"].startswith("299 nanobot")


def test_json_body_limit_rejects_oversized_writes() -> None:
    client, router = _client()
    response = client.patch(
        "/api/settings",
        content=json.dumps({"value": "x" * (1024 * 1024)}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}
    assert not router.requests


def test_settings_secret_is_forwarded_without_appearing_in_route_path() -> None:
    client, router = _client()
    response = client.patch(
        "/api/settings/provider",
        json={"provider": "openai", "api_key": "sk-secret"},
    )

    assert response.status_code == 200
    request = router.requests[-1]
    assert request.path.startswith("/api/settings/provider/update?")
    assert "sk-secret" in request.path  # Internal adapter only; never an HTTP URL or access log.


def test_openapi_and_docs_are_not_exposed() -> None:
    client, _ = _client()
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
