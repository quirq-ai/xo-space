from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.cowork_agent.connectors.composio import service as composio_service

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


def _proxy_user(token: str | None) -> str | None:
    if not token:
        return None
    return composio_service.user_for_proxy_token(token)


async def _proxy(
    request: Request, method: str, token: str | None = None,
) -> StreamingResponse | JSONResponse:
    user_id = _proxy_user(token)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={
                "error": "composio_identity_required",
                "detail": (
                    "This MCP proxy call carried no recognised user token. "
                    "Re-install the MCP config for this agent "
                    "(POST /api/connectors/composio/refresh-gateway) so it "
                    "points at a valid /mcp/cowork-proxy/u/<token> URL."
                ),
            },
        )

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
# error telling it to re-install, rather than silently reaching another tenant.
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
