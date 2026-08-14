"""FastAPI transport for the unified WebUI HTTP and WebSocket gateway."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.websockets import WebSocketDisconnect
from websockets.datastructures import Headers

if TYPE_CHECKING:
    from nanobot.channels.websocket import WebSocketChannel

_MAX_JSON_BODY_BYTES = 1024 * 1024
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LEGACY_WRITE_PATHS = (
    "/api/settings/",
    "/api/skill-evolution/analyze",
    "/api/skill-evolution/generate",
    "/api/webui/sidebar-state/update",
)


class AnalyzeSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    threshold: float
    source_model_preset: str = Field(min_length=1)
    optimizer_preset: str = Field(min_length=1)
    case_ids: list[str] = Field(min_length=1)


class EvolveSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(min_length=1)
    analysis_digest: str = Field(min_length=1)
    category_ids: list[str] | None = None
    finding_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> "EvolveSkillRequest":
        if not self.category_ids and not self.finding_ids:
            raise ValueError("select at least one reason category or finding")
        return self


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str = "r1"


class ReviseSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_ids: list[str] | None = None
    category_ids: list[str] | None = None


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _HTTPConnection:
    """Expose the tiny connection surface used by the legacy business router."""

    def __init__(self, client: tuple[str, int] | None) -> None:
        self.remote_address = client

    @staticmethod
    def respond(status: int, text: str) -> Any:
        from nanobot.webui.http_utils import http_error

        return http_error(status, text)


class ASGIWebSocketConnection:
    """Adapt a Starlette WebSocket to the channel's transport-neutral loop."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.remote_address = websocket.client
        self.request = SimpleNamespace(
            path=websocket.url.path
            + (f"?{websocket.url.query}" if websocket.url.query else ""),
            headers=websocket.headers,
        )

    async def send(self, raw: str) -> None:
        await self.websocket.send_text(raw)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        try:
            message = await self.websocket.receive()
        except WebSocketDisconnect as exc:
            raise StopAsyncIteration from exc
        if message["type"] == "websocket.disconnect":
            raise StopAsyncIteration
        if message.get("text") is not None:
            return message["text"]
        if message.get("bytes") is not None:
            return message["bytes"]
        return await self.__anext__()


def _headers(headers: Mapping[str, str]) -> Headers:
    return Headers([(str(key), str(value)) for key, value in headers.items()])


def _legacy_request(request: Request, path: str, method: str | None = None) -> Any:
    return SimpleNamespace(
        path=path,
        headers=_headers(request.headers),
        method=method or request.method,
    )


def _query_path(path: str, payload: Mapping[str, Any]) -> str:
    values: dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            values[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(value, bool):
            values[key] = "true" if value else "false"
        else:
            values[key] = str(value)
    query = urlencode(values)
    return f"{path}?{query}" if query else path


def _client(request: Request) -> tuple[str, int] | None:
    if request.client is None:
        return None
    return request.client.host, request.client.port


def _response(
    raw: Any,
    *,
    deprecated: bool = False,
    json_errors: bool = False,
) -> Response:
    headers = {key: value for key, value in raw.headers.raw_items()}
    headers.pop("Connection", None)
    headers.pop("Content-Length", None)
    headers.pop("Date", None)
    if deprecated:
        headers["Deprecation"] = "true"
        headers["Warning"] = '299 nanobot "Legacy query write API is deprecated; use JSON"'
    if json_errors and raw.status_code >= 400:
        message = raw.body.decode("utf-8", errors="replace").strip()
        return JSONResponse(
            {"detail": message or "Request failed"},
            status_code=raw.status_code,
            headers=headers,
        )
    return Response(
        content=raw.body,
        status_code=raw.status_code,
        headers=headers,
        media_type=None,
    )


def _is_legacy_write(path: str, method: str) -> bool:
    if any(path.startswith(prefix) for prefix in _LEGACY_WRITE_PATHS):
        return True
    if path.endswith("/delete") or path.endswith("/update"):
        return True
    if "/api/skill-evolution/tasks/" in path and path.rsplit("/", 1)[-1] in {
        "evolve", "reanalyze", "revise", "cancel", "test", "apply", "switch-back",
    }:
        return True
    return method in _WRITE_METHODS and path.startswith("/api/")


def create_gateway_app(channel: WebSocketChannel) -> FastAPI:
    """Build the single-port HTTP + WebSocket ASGI application."""

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    legacy_warnings: set[str] = set()

    @app.middleware("http")
    async def limit_json_body(request: Request, call_next: Any) -> Response:
        if request.method in _WRITE_METHODS:
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > _MAX_JSON_BODY_BYTES:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
            body = await request.body()
            if len(body) > _MAX_JSON_BODY_BYTES:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)

    @app.get("/openapi.json")
    @app.get("/docs")
    @app.get("/redoc")
    async def disabled_schema_routes() -> Response:
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    async def dispatch(request: Request, path: str, *, method: str | None = None) -> Response:
        raw = await channel.gateway.http.dispatch(
            _HTTPConnection(_client(request)),
            _legacy_request(request, path, method),
        )
        if raw is None:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return _response(raw, json_errors=True)

    async def legacy_query_write(request: Request) -> Response:
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raw = await channel.gateway.http.dispatch(
            _HTTPConnection(_client(request)), _legacy_request(request, path)
        )
        route = request.url.path
        if route not in legacy_warnings:
            legacy_warnings.add(route)
            channel.logger.warning("deprecated WebUI query write route used: {}", route)
        return _response(raw, deprecated=True, json_errors=True)

    def validated(model: type[BaseModel], payload: Any) -> BaseModel:
        try:
            return model.model_validate(payload)
        except Exception as exc:
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                raise RequestValidationError(exc.errors()) from exc
            raise

    @app.post("/api/skill-evolution/analyze")
    async def analyze(request: Request, body: dict[str, Any] | None = Body(default=None)) -> Response:
        if body is None:
            return await legacy_query_write(request)
        body = validated(AnalyzeSkillRequest, body)
        return await dispatch(request, _query_path(request.url.path, body.model_dump()), method="POST")

    @app.post("/api/skill-evolution/tasks/{task_id}/evolve")
    async def evolve(
        task_id: str, request: Request, body: dict[str, Any] | None = Body(default=None)
    ) -> Response:
        if body is None:
            return await legacy_query_write(request)
        body = validated(EvolveSkillRequest, body)
        path = _query_path(f"/api/skill-evolution/tasks/{task_id}/evolve", body.model_dump())
        return await dispatch(request, path, method="POST")

    @app.post("/api/skill-evolution/tasks/{task_id}/revise")
    async def revise(
        task_id: str, request: Request, body: dict[str, Any] | None = Body(default=None)
    ) -> Response:
        if body is None:
            return await legacy_query_write(request)
        body = validated(ReviseSkillRequest, body)
        path = _query_path(f"/api/skill-evolution/tasks/{task_id}/revise", body.model_dump())
        return await dispatch(request, path, method="POST")

    @app.post("/api/skill-evolution/tasks/{task_id}/{action}")
    async def skill_action(
        task_id: str,
        action: str,
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
    ) -> Response:
        allowed = {"reanalyze", "cancel", "test", "apply", "switch-back"}
        if action not in allowed:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if body is None and request.url.query:
            return await legacy_query_write(request)
        if action in {"test", "apply"}:
            payload = validated(RevisionRequest, body or {}).model_dump()
        else:
            validated(EmptyRequest, body or {})
            payload = {}
        path = _query_path(f"/api/skill-evolution/tasks/{task_id}/{action}", payload)
        return await dispatch(request, path, method="POST")

    @app.delete("/api/evaluations/runs/{run_id}")
    async def delete_evaluation(run_id: str, request: Request) -> Response:
        return await dispatch(request, f"/api/evaluations/runs/{run_id}/delete?confirm=1")

    @app.delete("/api/sessions/{session_key:path}")
    async def delete_session(session_key: str, request: Request) -> Response:
        return await dispatch(request, f"/api/sessions/{session_key}/delete")

    @app.put("/api/webui/sidebar-state")
    async def update_sidebar(request: Request, body: dict[str, Any]) -> Response:
        return await dispatch(
            request,
            _query_path("/api/webui/sidebar-state/update", {"state": body}),
        )

    async def settings_write(
        request: Request,
        body: dict[str, Any],
        legacy_path: str,
        *,
        extra: Mapping[str, Any] | None = None,
        mcp_values: bool = False,
    ) -> Response:
        payload = {**(extra or {}), **body}
        if mcp_values:
            headers = dict(request.headers)
            headers["X-Nanobot-MCP-Values"] = json.dumps(payload, ensure_ascii=False)
            legacy = SimpleNamespace(path=legacy_path, headers=_headers(headers), method="POST")
            raw = await channel.gateway.http.dispatch(_HTTPConnection(_client(request)), legacy)
            return _response(raw, json_errors=True)
        return await dispatch(request, _query_path(legacy_path, payload))

    @app.patch("/api/settings")
    async def patch_settings(request: Request, body: dict[str, Any]) -> Response:
        return await settings_write(request, body, "/api/settings/update")

    @app.put("/api/settings/skills/{name}")
    async def put_skill(name: str, request: Request, body: dict[str, Any]) -> Response:
        return await settings_write(request, body, "/api/settings/skills/update", extra={"name": name})

    @app.post("/api/settings/model-configurations")
    async def create_model(request: Request, body: dict[str, Any]) -> Response:
        return await settings_write(request, body, "/api/settings/model-configurations/create")

    @app.patch("/api/settings/model-configurations/{name}")
    async def patch_model(name: str, request: Request, body: dict[str, Any]) -> Response:
        return await settings_write(request, body, "/api/settings/model-configurations/update", extra={"name": name})

    @app.delete("/api/settings/model-configurations/{name}")
    async def delete_model(name: str, request: Request) -> Response:
        return await settings_write(request, {}, "/api/settings/model-configurations/delete", extra={"name": name})

    settings_sections = {
        "provider": "/api/settings/provider/update",
        "web-search": "/api/settings/web-search/update",
        "image-generation": "/api/settings/image-generation/update",
        "transcription": "/api/settings/transcription/update",
        "network-safety": "/api/settings/network-safety/update",
        "observability": "/api/settings/observability/update",
    }

    @app.patch("/api/settings/{section}")
    async def patch_settings_section(section: str, request: Request, body: dict[str, Any]) -> Response:
        legacy_path = settings_sections.get(section)
        if legacy_path is None:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await settings_write(request, body, legacy_path)

    @app.post("/api/settings/provider/oauth/{action}")
    async def provider_oauth(action: str, request: Request, body: dict[str, Any]) -> Response:
        if action not in {"login", "logout"}:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await settings_write(request, body, f"/api/settings/provider/oauth-{action}")

    @app.post("/api/settings/cli-apps/{action}")
    async def cli_app_action(action: str, request: Request, body: dict[str, Any]) -> Response:
        if action not in {"install", "update", "uninstall", "test"}:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await settings_write(request, body, f"/api/settings/cli-apps/{action}")

    @app.post("/api/settings/mcp-presets/{action}")
    async def mcp_action(action: str, request: Request, body: dict[str, Any]) -> Response:
        allowed = {"enable", "remove", "test", "custom", "import", "import-cursor", "tools"}
        if action not in allowed:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await settings_write(
            request, body, f"/api/settings/mcp-presets/{action}", mcp_values=True
        )

    async def websocket_endpoint(websocket: WebSocket) -> None:
        async def reject(status: int, detail: str) -> None:
            await websocket.send_denial_response(PlainTextResponse(detail, status_code=status))

        query = dict(websocket.query_params)
        client_id = str(query.get("client_id") or "")[:128]
        if not channel.is_allowed(client_id):
            await reject(403, "Forbidden")
            return
        supplied = query.get("token")
        static_token = channel.config.token.strip()
        if static_token:
            import hmac

            valid = bool(supplied) and hmac.compare_digest(supplied, static_token)
            if not valid and supplied:
                valid = channel.gateway.tokens.take_issued_token_if_valid(supplied)
        elif channel.config.websocket_requires_token:
            valid = channel.gateway.tokens.take_issued_token_if_valid(supplied)
        else:
            valid = True
            if supplied:
                channel.gateway.tokens.take_issued_token_if_valid(supplied)
        if not valid:
            await reject(401, "Unauthorized")
            return
        await websocket.accept()
        await channel._connection_loop(ASGIWebSocketConnection(websocket))

    app.add_api_websocket_route(channel.config.path, websocket_endpoint)
    if channel.config.path != "/":
        app.add_api_websocket_route(channel.config.path + "/", websocket_endpoint)

    async def missing_websocket(websocket: WebSocket) -> None:
        await websocket.send_denial_response(PlainTextResponse("Not Found", status_code=404))

    app.add_api_websocket_route("/{path:path}", missing_websocket)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def legacy_or_static(path: str, request: Request) -> Response:
        full_path = "/" + path
        if request.url.query:
            full_path += f"?{request.url.query}"
        raw = await channel.gateway.http.dispatch(
            _HTTPConnection(_client(request)), _legacy_request(request, full_path)
        )
        if raw is None:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        route = request.url.path
        deprecated = _is_legacy_write(route, request.method)
        if deprecated and route not in legacy_warnings:
            legacy_warnings.add(route)
            channel.logger.warning("deprecated WebUI query write route used: {}", route)
        return _response(
            raw,
            deprecated=deprecated,
            json_errors=request.url.path.startswith("/api/"),
        )

    return app
