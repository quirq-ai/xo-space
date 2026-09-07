from __future__ import annotations

import html
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors.composio import service as composio_service
from services.cowork_agent.connectors.composio.identity import get_composio_user

log = logging.getLogger(__name__)
router = APIRouter()


def _status_map_from_rows(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """One representative connection per toolkit slug: an active one wins.

    With multi-account mode on a toolkit can legitimately hold several active
    accounts; this picks the first of them (list_connections is newest-first
    once sorted by the caller) so the toolkit card keeps showing one primary.
    """
    by_slug: dict[str, dict[str, Any]] = {}
    for row in rows:
        slug = (row.get("toolkit") or "").upper()
        if not slug:
            continue
        prev = by_slug.get(slug)
        if prev and (prev.get("status") or "").upper() == "ACTIVE":
            continue
        by_slug[slug] = row
    return by_slug


def _toolkit_status_map(user_id: str) -> dict[str, dict[str, Any]]:
    return _status_map_from_rows(composio_service.list_connections(user_id))


def _account_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        slug = (row.get("toolkit") or "").upper()
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    return counts


class ConnectBody(BaseModel):
    auth_scheme: str = "OAUTH2"
    redirect_uri: Optional[str] = None
    # Multi-account: `alias` labels the account being connected ("work-gmail"),
    # and `allow_multiple` is what makes this a second account rather than a
    # replacement of the existing one.
    alias: Optional[str] = None
    allow_multiple: bool = False


class DisconnectBody(BaseModel):
    connected_account_id: str


class AliasBody(BaseModel):
    alias: Optional[str] = None


@router.get("/api/connectors/composio/toolkits")
async def list_toolkits(
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    from services.cowork_agent.connectors.composio import categories as composio_categories
    # The tab loading (or its Refresh) is the moment a user used to press "Reinstall
    # MCP gateway"; the sweep now runs itself here, in the background, rate-limited.
    composio_service.kick_gateway_sweep()
    # One fetch feeds both the primary-account map and the per-toolkit counts.
    rows = composio_service.newest_first(
        composio_service.list_connections(user_id)
    )
    status_by_slug = _status_map_from_rows(rows)
    account_counts = _account_counts(rows)
    classified = composio_categories.classified_toolkits()

    multi = composio_service.multi_account_config()

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
            # The card still shows one primary account; a client that wants the
            # rest reads /{toolkit}/accounts.
            "alias": (connection or {}).get("alias"),
            "account_count": account_counts.get(meta.slug, 0),
        })
    return JSONResponse({
        "toolkits": toolkits,
        "multi_account": multi or {"enable": False},
    })


@router.post("/api/connectors/composio/{toolkit}/connect")
async def connect(
    toolkit: str,
    body: ConnectBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    if body.allow_multiple and not composio_service.multi_account_enabled():
        log.info(
            "composio: allow_multiple requested for %s while multi-account mode "
            "is off; the extra account will be stored but only the newest one "
            "reaches the agent's session.", toolkit,
        )
    try:
        result = composio_service.initiate_connection(
            user_id=user_id,
            toolkit_id=toolkit,
            auth_scheme=body.auth_scheme,
            redirect_uri=body.redirect_uri,
            alias=body.alias,
            allow_multiple=body.allow_multiple,
        )
    except composio_service.AliasInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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


@router.get("/api/connectors/composio/{toolkit}/accounts")
async def list_toolkit_accounts(
    toolkit: str,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    """Every connected account this principal holds for one toolkit.

    Newest first. `is_default` marks the account a tool call gets when it names
    none; `pinned` marks the accounts actually reachable from the agent's
    session, which is only more than one when multi-account mode is on.
    """
    try:
        accounts = composio_service.list_toolkit_accounts(user_id, toolkit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    slug = composio_service.toolkit_meta(toolkit).slug
    pinned = set(
        composio_service.pinned_connected_accounts(user_id).get(slug.lower(), [])
    )
    default_seen = False
    for row in accounts:
        cid = row.get("connected_account_id")
        row["pinned"] = cid in pinned
        is_default = (
            not default_seen
            and cid in pinned
            and (row.get("status") or "").upper() == "ACTIVE"
        )
        row["is_default"] = is_default
        default_seen = default_seen or is_default

    multi = composio_service.multi_account_config()
    return JSONResponse({
        "toolkit": slug,
        "accounts": accounts,
        "multi_account": multi or {"enable": False},
    })


@router.put("/api/connectors/composio/{toolkit}/accounts/{connected_account_id}/alias")
async def put_account_alias(
    toolkit: str,
    connected_account_id: str,
    body: AliasBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    """Label one connected account, or clear its label with a null/empty alias.

    The alias is what an agent passes as a tool call's `account` parameter, so
    it is checked for uniqueness within the toolkit before the write.
    """
    try:
        accounts = composio_service.list_toolkit_accounts(user_id, toolkit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not any(
        row.get("connected_account_id") == connected_account_id for row in accounts
    ):
        raise HTTPException(
            status_code=404,
            detail="No such connected account for this user and toolkit.",
        )

    try:
        alias = composio_service.normalize_alias(body.alias)
        if alias:
            composio_service.assert_alias_free(
                user_id, toolkit, alias, except_account_id=connected_account_id,
            )
        stored = composio_service.set_alias(connected_account_id, alias)
    except composio_service.AliasInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # The alias is resolved inside the session, so the session must see it.
    composio_service.sync_session(user_id)
    return JSONResponse({
        "connected_account_id": connected_account_id,
        "alias": stored,
    })


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
    from services.cowork_agent.connectors.composio import action_prefs as composio_action_prefs
    return JSONResponse(
        {"actions": composio_action_prefs.get_toolkit_prefs(toolkit, user_id)}
    )


@router.put("/api/connectors/composio/{toolkit}/prefs")
async def put_toolkit_prefs(
    toolkit: str,
    body: PrefsBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    from services.cowork_agent.connectors.composio import action_prefs as composio_action_prefs
    from services.cowork_agent.connectors.composio import categories as composio_categories
    if toolkit not in composio_categories.classified_toolkits():
        raise HTTPException(
            status_code=404,
            detail=f"Per-action prefs are not configurable for toolkit '{toolkit}' yet.",
        )
    updated = composio_action_prefs.bulk_set(toolkit, body.actions, user_id)
    composio_service.sync_session(user_id)
    return JSONResponse({"actions": updated})


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
