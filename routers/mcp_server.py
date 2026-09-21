"""Local management and authenticated MCP transport for Space."""

from __future__ import annotations

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import StrictBool
from starlette.responses import JSONResponse

from routers.browser_guard import is_local_mutation, origin_allowed
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services.errors import ServiceError
from services.mcp_server import service

router = APIRouter()


def _manage(request: Request, response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    if not is_local_mutation(request):
        raise HTTPException(403, "Manage the MCP server from this Space's local Setup page.")


class SettingsRequest(ForbidExtra):
    enabled: StrictBool


@router.get("/api/mcp-server", dependencies=[Depends(_manage)])
def get_settings() -> dict:
    try:
        return service.status()
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.put("/api/mcp-server", dependencies=[Depends(_manage)])
def put_settings(body: SettingsRequest) -> dict:
    try:
        return service.configure(enabled=body.enabled)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.post("/api/mcp-server/rotate-token", dependencies=[Depends(_manage)])
def rotate_token() -> dict:
    try:
        return service.configure(rotate=True)
    except ServiceError as exc:
        raise http_error(exc) from exc


class MCPTransport:
    async def __call__(self, scope, receive, send) -> None:
        request = Request(scope)
        try:
            if not origin_allowed(request):
                raise ServiceError("mcp_origin_forbidden", "This browser origin is not allowed.", 403)
            await to_thread.run_sync(service.authenticate, request.headers.get("authorization", ""))
            protocol = getattr(request.app.state, "space_mcp", None)
            if protocol is None:
                raise ServiceError("mcp_unavailable", "The MCP transport is not running.", 503)
        except ServiceError as exc:
            headers = {"Cache-Control": "no-store"}
            if exc.status == 401:
                headers["WWW-Authenticate"] = 'Bearer realm="Space MCP"'
            await JSONResponse({"error": {"code": exc.code, "message": exc.message}},
                               status_code=exc.status, headers=headers)(scope, receive, send)
            return

        async def no_cache_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"cache-control", b"no-store")]
            await send(message)

        await protocol(scope, receive, no_cache_send)


# An ASGI route preserves SDK-owned HTTP/SSE handling and protocol validation.
router.add_route(service.ENDPOINT_PATH, MCPTransport(), methods=["GET", "POST", "DELETE"])
