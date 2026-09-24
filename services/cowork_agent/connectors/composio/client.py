"""The Composio SDK client, using the user's own key (see :mod:`.byo_key`).

Same nine call shapes as the retired ``services/swarm_api/composio.py`` so
``service.py`` calls them unchanged. The SDK is imported lazily and memoised on the
key value; a key change makes a new client. Connections are scoped to
``byo_key.user_id()`` — this backend's XO account id, not its Space — on every read.

Every call resolves that id *before* its ``try``: an unknown account is this backend's
own state, not something Composio said, so it must surface as
:class:`~.account_identity.XOAccountRequired` rather than be wrapped into a
:class:`ComposioError` by the handler below.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from services.cowork_agent.connectors.composio import account_identity, byo_key

log = logging.getLogger(__name__)

ComposioKeyRequired = byo_key.ComposioKeyRequired
XOAccountRequired = account_identity.XOAccountRequired


class ComposioError(RuntimeError):
    def __init__(self, message: str, *, authoritative: bool = False) -> None:
        super().__init__(message)
        self.authoritative = authoritative


class ComposioNotFound(ComposioError):
    pass


# Back-compat aliases for the retired ``swarm_api.composio`` names, kept so call sites
# and tests that caught the old exception classes keep working.
SwarmComposioError = ComposioError
SwarmComposioNotFound = ComposioNotFound


_sdk_client: Any = None
_sdk_key: str = ""


def _sdk() -> Any:
    """The memoised Composio SDK client for the active key."""
    global _sdk_client, _sdk_key
    key = byo_key.require()
    if _sdk_client is not None and _sdk_key == key:
        return _sdk_client
    try:
        from composio import Composio
    except ImportError as exc:
        raise ComposioError(
            "The `composio` Python package is not installed. Install it from "
            "requirements.txt (pinned >=0.18,<0.19)."
        ) from exc
    _sdk_client = Composio(api_key=key)
    _sdk_key = key
    return _sdk_client


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    from services.cowork_agent.connectors.composio import service
    return service._attr(obj, *names, default=default)


def _raise(exc: Exception) -> "ComposioError":
    """Map an SDK exception to our two classes. Import composio_client lazily."""
    try:
        import composio_client
    except ImportError:
        composio_client = None
    if composio_client is not None:
        if isinstance(exc, (composio_client.AuthenticationError,
                            composio_client.PermissionDeniedError)):
            return ComposioError(
                "Composio rejected your API key. Check COMPOSIO_API_KEY or the "
                "key on the Connectors tab.",
                authoritative=True,
            )
        if isinstance(exc, composio_client.NotFoundError):
            return ComposioNotFound(str(exc) or "Not found.")
    return ComposioError(f"Composio request failed: {exc}")


def _meta(toolkit_id: str):
    from services.cowork_agent.connectors.composio import service
    return service.toolkit_meta(toolkit_id)


def auth_config_for(toolkit_id: str) -> str:
    byo_key.require()
    cached = byo_key.load_auth_configs().get(toolkit_id)
    if cached:
        return cached
    meta = _meta(toolkit_id)
    slug = meta.slug
    sdk = _sdk()
    try:
        page = sdk.auth_configs.list(toolkit_slug=slug, show_disabled=False)
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    items = _attr(page, "items", default=page) or []
    schemes = {s.upper() for s in meta.schemes}

    def _ok(it: Any) -> bool:
        status = str(_attr(it, "status", default="") or "").upper()
        scheme = str(_attr(it, "auth_scheme", default="") or "").upper()
        return status == "ENABLED" and (not scheme or scheme in schemes)

    candidates = [it for it in items if _ok(it)]
    # Custom over managed, then most connections.
    candidates.sort(key=lambda it: (
        _attr(it, "type", default="") == "custom",
        _attr(it, "no_of_connections", default=0) or 0,
    ), reverse=True)
    chosen = _attr(candidates[0], "id") if candidates else None
    if not chosen:
        if "OAUTH2" in schemes:
            options = {"type": "use_composio_managed_auth", "name": meta.display_name}
        else:
            options = {"type": "use_custom_auth",
                       "auth_scheme": next(iter(meta.schemes)),
                       "name": meta.display_name}
        try:
            created = sdk.auth_configs.create(slug, options)
        except Exception as exc:  # noqa: BLE001
            raise ComposioError(
                f"Could not set up authentication for {meta.display_name}. Create an "
                f"auth config for it in your Composio dashboard.",
            ) from exc
        chosen = _attr(created, "id")
    if not chosen:
        raise ComposioError(f"Composio returned no auth config id for {slug}.")
    byo_key.save_auth_config(toolkit_id, chosen)
    return chosen


def connect(toolkit_id: str, *, auth_scheme: str, redirect_uri: str,
            alias: Optional[str], allow_multiple: bool) -> dict[str, Any]:
    uid = byo_key.user_id()
    ac = auth_config_for(toolkit_id)
    kwargs: dict[str, Any] = {
        "user_id": uid, "auth_config_id": ac, "callback_url": redirect_uri,
    }
    if alias:
        kwargs["alias"] = alias
    if allow_multiple:
        kwargs["allow_multiple"] = True
    try:
        request = _sdk().connected_accounts.link(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    return {
        "auth_url": _attr(request, "redirect_url"),
        "connection_request_id": _attr(request, "id"),
        "alias": alias,
    }


def connection_status(connection_request_id: str) -> dict[str, Any]:
    byo_key.require()
    try:
        record = _sdk().connected_accounts.get(connection_request_id)
    except Exception as exc:  # noqa: BLE001
        return {"status": "FAILED", "connected_account_id": None, "error": str(exc)}
    return {
        "status": _attr(record, "status", default="PENDING"),
        "connected_account_id": _attr(record, "id"),
    }


def list_connections(*, statuses: Optional[list[str]] = None,
                     toolkit_slugs: Optional[list[str]] = None) -> list[dict[str, Any]]:
    byo_key.require()
    kwargs: dict[str, Any] = {"user_ids": [byo_key.user_id()]}
    # Account-scoped, so this lists what the whole XO account has connected, including
    # connections another Space of the same account made.
    if statuses:
        kwargs["statuses"] = statuses
    if toolkit_slugs:
        kwargs["toolkit_slugs"] = [s.lower() for s in toolkit_slugs]
    try:
        page = _sdk().connected_accounts.list(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    items = _attr(page, "items", default=page) or []
    out: list[dict[str, Any]] = []
    for it in items:
        toolkit = (_attr(it, "toolkit", "slug", default="")
                   or _attr(it, "toolkit_slug", default="") or "")
        out.append({
            "toolkit": str(toolkit).upper() or None,
            "connected_account_id": _attr(it, "id"),
            "status": _attr(it, "status", default="UNKNOWN"),
            "scheme": _attr(it, "auth_scheme", default=None),
            "alias": _attr(it, "alias", default=None),
            "created_at": _attr(it, "created_at", default=None),
            "is_disabled": bool(_attr(it, "is_disabled", default=False)),
        })
    return out


def _owned_ids() -> set[str]:
    uid = byo_key.user_id()
    try:
        page = _sdk().connected_accounts.list(user_ids=[uid])
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    items = _attr(page, "items", default=page) or []
    return {_attr(it, "id") for it in items if _attr(it, "id")}


def set_alias(connected_account_id: str, alias: Optional[str]) -> Optional[str]:
    byo_key.require()
    if connected_account_id not in _owned_ids():
        raise ComposioNotFound("No such connected account for this user.")
    try:
        _sdk().connected_accounts.update(connected_account_id, alias=alias or "")
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    return alias


def disconnect(connected_account_id: str) -> None:
    byo_key.require()
    if connected_account_id not in _owned_ids():
        raise ComposioNotFound("No such connected account for this user.")
    try:
        _sdk().connected_accounts.delete(connected_account_id)
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc


def list_tools(toolkit_id: str) -> list[dict[str, Any]]:
    byo_key.require()
    slug = _meta(toolkit_id).slug
    try:
        tools = _sdk().tools.get_raw_composio_tools(toolkits=[slug], limit=200)
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append({
            "slug": _attr(t, "slug", default="") or _attr(t, "name", default=""),
            "name": _attr(t, "name", default=""),
            "description": _attr(t, "description", default=""),
            "parameters": _attr(t, "input_parameters", default={}),
        })
    return out


def _session_response(session_id: str, session: Any) -> dict[str, Any]:
    url = _attr(session, "mcp", "url")
    headers = _attr(session, "mcp", "headers", default=None)
    if not url:
        raise ComposioError(f"Composio session {session_id} exposed no MCP url.")
    mcp: dict[str, Any] = {"url": str(url)}
    if headers:
        mcp["headers"] = dict(headers)
    return {"session_id": session_id, "mcp": mcp}


def create_session(config: dict[str, Any]) -> dict[str, Any]:
    byo_key.require()
    uid = byo_key.user_id()
    try:
        session = _sdk().create(user_id=uid, mcp=True, **config)
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    new_id = _attr(session, "session_id") or _attr(session, "id")
    if not new_id:
        raise ComposioError("Composio session create returned no session id.")
    return _session_response(str(new_id), session)


def update_session(session_id: str, config: dict[str, Any]) -> dict[str, Any]:
    byo_key.require()
    try:
        session = _sdk().use(session_id, mcp=True)
        session.update(
            connected_accounts=config.get("connected_accounts") or {},
            toolkits=config.get("toolkits"),
            tools=config.get("tools") or {},
            multi_account=config.get("multi_account"),
        )
    except Exception as exc:  # noqa: BLE001
        raise _raise(exc) from exc
    return _session_response(session_id, session)


def delete_session(session_id: str) -> None:
    try:
        _sdk().sessions.delete(session_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("composio: could not delete session %s: %s", session_id, exc)
