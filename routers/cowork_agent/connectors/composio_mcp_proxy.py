from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.cowork_agent.connectors.composio import service as composio_service
from services.cowork_agent.connectors.composio import state as composio_state

log = logging.getLogger(__name__)
router = APIRouter()

_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "authorization",
}

_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)


def _forwarded_headers(incoming: dict[str, str], inject: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in incoming.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    for k, v in (inject or {}).items():
        out[k] = v
    return out


async def _proxy_user(token: str | None) -> str | None:
    if not token:
        return None
    return await composio_service.user_for_proxy_token(token)


_IDENTITY_REQUIRED = {
    "error": "composio_identity_required",
    "detail": (
        "This MCP proxy call carried no recognised user token: the agent's MCP config "
        "is stale, or was written for another workspace. xo-space rewrites it "
        "automatically (at boot, periodically, and when the Connectors tab loads) — "
        "make sure xo-space is running, then restart the agent so it re-reads its "
        "/mcp/composio-proxy/u/<token> URL. If this persists, check the server log "
        "for 'Composio MCP' lines."
    ),
}


async def _proxy(
    request: Request, method: str, token: str | None = None,
) -> StreamingResponse | JSONResponse:
    try:
        user_id = await _proxy_user(token)
    except composio_state.StateUnavailable as exc:
        # Reached only when this pod does not know the token AND xo-swarm-api could not
        # be asked. Deliberately not a 401: that tells the agent its config is stale
        # when it is not, and the reconcile sweep could not rewrite it during the same
        # outage anyway. 503 is truthful and retryable, and the agent backs off
        # instead of looping.
        log.warning("mcp_proxy: tenant state unavailable: %s", exc)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "30"},
            content={
                "error": "composio_state_unavailable",
                "detail": (
                    "Could not reach xo-swarm-api to resolve this MCP proxy token. "
                    "This is transient — retry shortly. The agent's configuration is "
                    "fine; do not re-install it."
                ),
            },
        )
    if not user_id:
        return JSONResponse(status_code=401, content=_IDENTITY_REQUIRED)

    try:
        entry = composio_service.build_mcp_server_entry(user_id)
    except Exception as exc:
        log.exception("mcp_proxy: build_mcp_server_entry failed")
        return JSONResponse(
            status_code=502,
            content={"error": "composio_session_unavailable", "detail": str(exc)},
        )

    upstream_url = entry.get("url")
    upstream_headers = entry.get("headers") or {}
    if not upstream_url:
        return JSONResponse(
            status_code=502,
            content={"error": "composio_session_unavailable", "detail": "no upstream url"},
        )

    body = await request.body()
    forward_headers = _forwarded_headers(dict(request.headers), upstream_headers)

    client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    req = client.build_request(
        method,
        upstream_url,
        headers=forward_headers,
        content=body if body else None,
        params=dict(request.query_params),
    )
    try:
        upstream_resp = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        log.warning("mcp_proxy: upstream request failed: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": "composio_unreachable", "detail": str(exc)},
        )

    response_headers: dict[str, str] = {}
    for k, v in upstream_resp.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        if k.lower() == "content-type":
            response_headers[k] = v
        elif k.lower() == "mcp-session-id":
            response_headers[k] = v

    async def relay() -> Any:
        try:
            async for chunk in upstream_resp.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


# Every handler below serves the canonical `/mcp/composio-proxy/...` path AND the
# legacy `/mcp/cowork-proxy/...` one, which is what configs written before the
# rename still point at. Both resolve the same token to the same principal, so old
# and new configs work side by side and no already-running agent is stranded.
# Retire the cowork-proxy decorators only once every config has been rewritten.
#
# The unscoped routes carry no identity and therefore always 401. They are
# deliberate: a stale agent config that predates the /u/<token> URLs gets a clear
# error saying its config is stale (the reconcile sweep rewrites it; the agent
# needs a restart), rather than silently reaching another tenant.
# Do not delete them as dead code.


@router.post("/mcp/composio-proxy/")
@router.post("/mcp/composio-proxy")
@router.post("/mcp/cowork-proxy/")
@router.post("/mcp/cowork-proxy")
async def mcp_proxy_post(request: Request):
    return await _proxy(request, "POST")


@router.get("/mcp/composio-proxy/")
@router.get("/mcp/composio-proxy")
@router.get("/mcp/cowork-proxy/")
@router.get("/mcp/cowork-proxy")
async def mcp_proxy_get(request: Request):
    return await _proxy(request, "GET")


@router.delete("/mcp/composio-proxy/")
@router.delete("/mcp/composio-proxy")
@router.delete("/mcp/cowork-proxy/")
@router.delete("/mcp/cowork-proxy")
async def mcp_proxy_delete(request: Request):
    return await _proxy(request, "DELETE")


@router.post("/mcp/composio-proxy/u/{token}/")
@router.post("/mcp/composio-proxy/u/{token}")
@router.post("/mcp/cowork-proxy/u/{token}/")
@router.post("/mcp/cowork-proxy/u/{token}")
async def mcp_proxy_post_scoped(request: Request, token: str):
    return await _proxy(request, "POST", token)


@router.get("/mcp/composio-proxy/u/{token}/")
@router.get("/mcp/composio-proxy/u/{token}")
@router.get("/mcp/cowork-proxy/u/{token}/")
@router.get("/mcp/cowork-proxy/u/{token}")
async def mcp_proxy_get_scoped(request: Request, token: str):
    return await _proxy(request, "GET", token)


@router.delete("/mcp/composio-proxy/u/{token}/")
@router.delete("/mcp/composio-proxy/u/{token}")
@router.delete("/mcp/cowork-proxy/u/{token}/")
@router.delete("/mcp/cowork-proxy/u/{token}")
async def mcp_proxy_delete_scoped(request: Request, token: str):
    return await _proxy(request, "DELETE", token)
