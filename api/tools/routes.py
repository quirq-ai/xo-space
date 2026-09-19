"""``GET /api/tools``: tools across the caller's connected Composio toolkits."""

import logging

from fastapi import APIRouter, Request

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_tools(request: Request):
    """Aggregate tools across the caller's connected Composio toolkits.

    Identity comes from the request's session bearer. Returns [] when Composio
    is not configured, the caller sent no valid bearer, or the user has no
    active connections, preserving the previous stub's contract rather than
    401ing a list the UI polls opportunistically.
    """
    try:
        from services.cowork_agent.connectors.composio import service as composio_service
        from services.cowork_agent.connectors.composio.identity import resolve_user
    except Exception as exc:
        log.debug("tools: composio not importable: %s", exc)
        return []

    user_id = await resolve_user(request)
    if not user_id:
        log.debug("tools: no valid session bearer on request; returning []")
        return []

    try:
        active_toolkits = {
            (row.get("toolkit") or "").upper()
            for row in composio_service.list_connections(user_id)
            if row.get("status") == "ACTIVE"
        }
    except Exception as exc:
        log.debug("tools: list_connections failed: %s", exc)
        return []

    out: list[dict] = []
    for toolkit_id, meta in composio_service.TOOLKITS.items():
        if meta.slug not in active_toolkits:
            continue
        try:
            for tool in composio_service.list_tools(user_id, toolkit_id):
                out.append({
                    "toolkit": meta.slug,
                    "slug": tool.get("slug"),
                    "name": tool.get("name"),
                    "description": tool.get("description"),
                })
        except Exception as exc:
            log.debug("tools: list_tools(%s) failed: %s", meta.slug, exc)
    return out
