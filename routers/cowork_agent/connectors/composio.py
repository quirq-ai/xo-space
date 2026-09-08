from __future__ import annotations

import html
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors.composio import service as composio_service
from services.cowork_agent.connectors.composio import state as composio_state
from services.cowork_agent.connectors.composio import workspace_scope
from services.cowork_agent.connectors.composio.identity import get_composio_user

log = logging.getLogger(__name__)
router = APIRouter()


async def _legacy_connection_counts() -> list[dict[str, Any]]:
    """Toolkits still holding connections under the retired workspace-scoped user id.

    Drives the reconnect prompt. Best-effort and never fatal: a swarm that cannot be
    reached simply means the prompt does not appear this time round.
    """
    try:
        legacy = await composio_state.alegacy_principal()
    except Exception:
        return []
    counts: dict[str, int] = {}
    for row in composio_service.legacy_connections(legacy):
        slug = (row.get("toolkit") or "").upper()
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    return [
        {"toolkit": slug, "count": count} for slug, count in sorted(counts.items())
    ]


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
    scope = workspace_scope.load()

    toolkits: list[dict[str, Any]] = []
    for toolkit_id, meta in composio_service.TOOLKITS.items():
        connection = status_by_slug.get(meta.slug)
        entry = scope.get(toolkit_id) or {}
        toolkits.append({
            "id": toolkit_id,
            "slug": meta.slug,
            "display_name": meta.display_name,
            "schemes": list(meta.schemes),
            # Account-wide: whether the account holds a connection at all.
            "status": (connection or {}).get("status", "NEEDS_AUTH"),
            "connected_account_id": (connection or {}).get("connected_account_id"),
            "scheme": (connection or {}).get("scheme"),
            "supports_action_prefs": toolkit_id in classified,
            # The card still shows one primary account; a client that wants the
            # rest reads /{toolkit}/accounts.
            "alias": (connection or {}).get("alias"),
            "account_count": account_counts.get(meta.slug, 0),
            # Workspace-scoped: a toolkit can be connected on the account and still
            # be off here. That is the whole point of the split.
            "workspace_enabled": bool(entry.get("enabled")),
            "pinned_account_ids": list(entry.get("connected_account_ids") or []),
        })
    return JSONResponse({
        "toolkits": toolkits,
        "multi_account": multi or {"enable": False},
        "max_accounts_per_toolkit": composio_service.max_accounts_per_toolkit(),
        "legacy_connections": await _legacy_connection_counts(),
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
        # The workspace that ran the OAuth flow gets the connection without a second
        # step. Every *other* workspace of the account starts with it off and opts in —
        # connections are account-wide now, reach is not.
        connected_account_id = result.get("connected_account_id")
        if connected_account_id:
            try:
                workspace_scope.adopt_connection(
                    toolkit,
                    connected_account_id,
                    max_accounts=composio_service.max_accounts_per_toolkit(),
                )
            except Exception as exc:
                log.warning(
                    "composio: could not enable %s in this workspace after connect: %s",
                    toolkit, exc,
                )
        composio_service.sync_session(user_id)
    return JSONResponse(result)


@router.post("/api/connectors/composio/{toolkit}/disconnect")
async def disconnect(
    toolkit: str,
    body: DisconnectBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    """Delete a connected account. **Account-wide** — every workspace loses it.

    Since connections belong to the account, this is destructive well beyond the
    workspace making the call, and the UI confirms it. To stop using a connection *here*
    without touching anyone else, use the unlink route below.

    Other workspaces cannot be reached to clean their pins; their next session build
    prunes the dead id itself (see ``service.prune_scope_to_live_accounts``).
    """
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
    workspace_scope.unlink_account(toolkit, body.connected_account_id)
    rows = composio_service.list_connections(user_id)
    still_connected = any(
        r.get("connected_account_id") == body.connected_account_id and r.get("status") == "ACTIVE"
        for r in rows
    )
    composio_service.sync_session(user_id)
    return JSONResponse({"status": "needs_auth" if not still_connected else "connected"})


@router.post("/api/connectors/composio/{toolkit}/accounts/{connected_account_id}/unlink")
async def unlink_account(
    toolkit: str,
    connected_account_id: str,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    """Stop using one connected account *in this workspace*. Nothing is deleted.

    The account stays connected on the XO account and in every other workspace that has
    pinned it. A toolkit left with no pins is switched off here rather than falling back
    to Composio's most-recently-connected default, which would quietly re-point it.
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
    entry = workspace_scope.unlink_account(toolkit, connected_account_id)
    composio_service.sync_session(user_id)
    return JSONResponse({
        "toolkit": toolkit,
        "workspace_enabled": bool(entry.get("enabled")),
        "pinned_account_ids": list(entry.get("connected_account_ids") or []),
    })


class ScopeBody(BaseModel):
    """This workspace's opinion about one toolkit. Omitted fields are left alone."""

    enabled: Optional[bool] = None
    connected_account_ids: Optional[list[str]] = None


@router.get("/api/connectors/composio/{toolkit}/scope")
async def get_toolkit_scope(
    toolkit: str,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    entry = workspace_scope.load().get(toolkit) or {}
    return JSONResponse({
        "toolkit": toolkit,
        "workspace_enabled": bool(entry.get("enabled")),
        "pinned_account_ids": list(entry.get("connected_account_ids") or []),
        "max_accounts_per_toolkit": composio_service.max_accounts_per_toolkit(),
    })


@router.put("/api/connectors/composio/{toolkit}/scope")
async def put_toolkit_scope(
    toolkit: str,
    body: ScopeBody,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    """Choose what this workspace reaches for one toolkit.

    A pinned account must be one the XO account actually holds — validated here so a
    typo is a 422 naming the id, rather than a session creation that fails for every
    toolkit at once.
    """
    if toolkit not in composio_service.TOOLKITS:
        raise HTTPException(status_code=404, detail=f"Unknown toolkit '{toolkit}'.")

    if body.connected_account_ids is not None:
        owned = {
            row.get("connected_account_id")
            for row in composio_service.list_toolkit_accounts(user_id, toolkit)
        }
        unknown = [cid for cid in body.connected_account_ids if cid not in owned]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Not a connected account of this user on {toolkit}: "
                    f"{', '.join(unknown)}."
                ),
            )

    entry = workspace_scope.set_toolkit(
        toolkit,
        enabled=body.enabled,
        connected_account_ids=body.connected_account_ids,
        max_accounts=composio_service.max_accounts_per_toolkit(),
    )
    composio_service.sync_session(user_id)
    return JSONResponse({
        "toolkit": toolkit,
        "workspace_enabled": bool(entry.get("enabled")),
        "pinned_account_ids": list(entry.get("connected_account_ids") or []),
    })


@router.get("/api/connectors/composio/{toolkit}/accounts")
async def list_toolkit_accounts(
    toolkit: str,
    user_id: str = Depends(get_composio_user),
) -> JSONResponse:
    """Every connected account the XO account holds for one toolkit.

    Newest first. `pinned` marks the accounts **this workspace** has chosen, which is
    what the agent's session can actually reach; `is_default` marks the one a tool call
    gets when it names none.

    The account list is account-wide, so a connection made in a sibling workspace shows
    up here unpinned — ready to be enabled, not silently in use.
    """
    try:
        accounts = composio_service.list_toolkit_accounts(user_id, toolkit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    slug = composio_service.toolkit_meta(toolkit).slug
    scope_entry = workspace_scope.load().get(toolkit) or {}
    pinned = set(scope_entry.get("connected_account_ids") or [])
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
        "workspace_enabled": bool(scope_entry.get("enabled")),
        "max_accounts_per_toolkit": composio_service.max_accounts_per_toolkit(),
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
        {"actions": composio_action_prefs.get_toolkit_prefs(toolkit)}
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
    updated = composio_action_prefs.bulk_set(toolkit, body.actions)
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
