from __future__ import annotations

import html
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from services.cowork_agent.composio import service as composio_service
from services.cowork_agent.composio.identity import get_composio_user

log = logging.getLogger(__name__)
router = APIRouter()


def _toolkit_status_map(user_id: str) -> dict[str, dict[str, Any]]:
    by_slug: dict[str, dict[str, Any]] = {}
    for row in composio_service.list_connections(user_id):
        slug = (row.get("toolkit") or "").upper()
        if not slug:
            continue
        prev = by_slug.get(slug)
        if prev and prev.get("status") == "ACTIVE" and row.get("status") != "ACTIVE":
            continue
        by_slug[slug] = row
    return by_slug


class ConnectBody(BaseModel):
    auth_scheme: str = "OAUTH2"
    redirect_uri: Optional[str] = None


class DisconnectBody(BaseModel):
    connected_account_id: str


@router.get("/api/connectors/composio/toolkits")
async def list_toolkits(
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    from services.cowork_agent.composio import categories as composio_categories
    status_by_slug = _toolkit_status_map(user_id)
    classified = composio_categories.classified_toolkits()

    toolkits: list[dict[str, Any]] = []
    for toolkit_id, meta in composio_service.TOOLKITS.items():
        connection = status_by_slug.get(meta.slug)
        toolkits.append({
            "id": toolkit_id,
            "slug": meta.slug,
            "display_name": meta.display_name,
            "schemes": list(meta.schemes),
            "status": (connection or {}).get("status", "NEEDS_AUTH"),
            "connected_account_id": (connection or {}).get("connected_account_id"),
            "scheme": (connection or {}).get("scheme"),
            "supports_action_prefs": toolkit_id in classified,
        })
    return JSONResponse({"toolkits": toolkits})


@router.post("/api/connectors/composio/{toolkit}/connect")
async def connect(
    toolkit: str,
    body: ConnectBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    try:
        result = composio_service.initiate_connection(
            user_id=user_id,
            toolkit_id=toolkit,
            auth_scheme=body.auth_scheme,
            redirect_uri=body.redirect_uri,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    composio_service.sync_session(user_id)
    return JSONResponse(result)


@router.get("/api/connectors/composio/{toolkit}/status")
async def connect_status(
    toolkit: str,
    connection_request_id: str = Query(...),
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    result = composio_service.check_connection(connection_request_id)
    if (result.get("status") or "").upper() == "ACTIVE":
        composio_service.sync_session(user_id)
    return JSONResponse(result)


@router.post("/api/connectors/composio/{toolkit}/disconnect")
async def disconnect(
    toolkit: str,
    body: DisconnectBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    owned = {
        r.get("connected_account_id") for r in composio_service.list_connections(user_id)
    }
    if body.connected_account_id not in owned:
        raise HTTPException(
            status_code=404,
            detail="No such connected account for this user.",
        )
    ok = composio_service.disconnect(body.connected_account_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Composio disconnect failed.")
    rows = composio_service.list_connections(user_id)
    still_connected = any(
        r.get("connected_account_id") == body.connected_account_id and r.get("status") == "ACTIVE"
        for r in rows
    )
    composio_service.sync_session(user_id)
    return JSONResponse({"status": "needs_auth" if not still_connected else "connected"})


@router.get("/api/connectors/composio/{toolkit}/tools")
async def list_toolkit_tools(
    toolkit: str,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    try:
        tools = composio_service.list_tools(user_id, toolkit, include_disabled=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return JSONResponse({"tools": tools})


class PrefsBody(BaseModel):
    actions: dict[str, bool]


@router.get("/api/connectors/composio/{toolkit}/prefs")
async def get_toolkit_prefs(
    toolkit: str,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    from services.cowork_agent.composio import action_prefs as composio_action_prefs
    return JSONResponse(
        {"actions": composio_action_prefs.get_toolkit_prefs(toolkit, user_id)}
    )


@router.put("/api/connectors/composio/{toolkit}/prefs")
async def put_toolkit_prefs(
    toolkit: str,
    body: PrefsBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    from services.cowork_agent.composio import action_prefs as composio_action_prefs
    from services.cowork_agent.composio import categories as composio_categories
    if toolkit not in composio_categories.classified_toolkits():
        raise HTTPException(
            status_code=404,
            detail=f"Per-action prefs are not configurable for toolkit '{toolkit}' yet.",
        )
    updated = composio_action_prefs.bulk_set(toolkit, body.actions, user_id)
    composio_service.sync_session(user_id)
    return JSONResponse({"actions": updated})


@router.post("/api/connectors/composio/refresh-gateway")
async def refresh_gateway(
    agent: str = Query(...),
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    supported = composio_service.gateway_install_agents()
    if agent not in supported:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Agent '{agent}' has no gateway MCP install path. "
                f"Supported: {supported or '(none installed)'}."
            ),
        )

    result = composio_service.install_into_gateway(user_id, agent)
    if result.get("ok"):
        result["multi_tenant_warning"] = (
            f"{agent}'s MCP config is machine-global. It now points at "
            f"Composio user '{user_id}' for every session on this host, "
            "including other users'. Per-user isolation on this backend "
            "requires one gateway process per user."
        )
    status = 200 if result.get("ok") else 422
    return JSONResponse(result, status_code=status)


@router.get("/api/connectors/composio/callback")
async def composio_callback(
    toolkit: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
) -> HTMLResponse:
    if error or (status and status.upper() == "FAILED"):
        desc = error_description or error or "Authorization failed."
        body = {
            "type": "connector-auth-error",
            "connector": "composio",
            "toolkit": toolkit or "",
            "error": desc,
        }
        return HTMLResponse(content=_callback_html(body, ok=False), status_code=400)

    body = {
        "type": "connector-auth-complete",
        "connector": "composio",
        "toolkit": toolkit or "",
    }
    return HTMLResponse(content=_callback_html(body, ok=True))


def _callback_html(payload: dict[str, Any], ok: bool) -> str:
    # Everything interpolated below can originate in a query parameter the
    # provider redirect controls, so it is untrusted. HTML text is escaped, and
    # `<` is escaped in the JSON so a payload cannot close the <script> element
    # it is embedded in (JSON alone does not escape "</script>").
    title = html.escape("Connected" if ok else "Authorization failed")
    heading = html.escape("You're connected." if ok else "Authorization failed")
    sub = html.escape(
        "You can close this window." if ok else str(payload.get("error", ""))
    )
    payload_json = json.dumps(payload).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; padding: 32px; max-width: 480px; margin: 0 auto; }}
  h2 {{ margin: 0 0 8px; }} p {{ color: #555; }}
</style></head>
<body>
  <h2>{heading}</h2>
  <p>{sub}</p>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage({payload_json}, "*");
      }}
    }} catch (e) {{}}
    setTimeout(function () {{ window.close(); }}, 300);
  </script>
</body></html>"""
