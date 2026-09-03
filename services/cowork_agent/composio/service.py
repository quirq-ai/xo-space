from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from services import tenancy

log = logging.getLogger(__name__)


def _require_user_id(user_id: Optional[str], what: str) -> str:
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError(
            f"composio.{what}: a real user_id is required (got {user_id!r})."
        )
    return uid


@dataclass(frozen=True)
class ToolkitMeta:
    slug: str
    display_name: str
    schemes: tuple[str, ...]
    auth_env_keys: dict[str, str]


TOOLKITS: dict[str, ToolkitMeta] = {
    "gmail":           ToolkitMeta("GMAIL",           "Gmail",            ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GMAIL"}),
    "googlecalendar":  ToolkitMeta("GOOGLECALENDAR",  "Google Calendar",  ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLECALENDAR"}),
    "notion":          ToolkitMeta("NOTION",          "Notion",           ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_NOTION"}),
    "googlesheets":    ToolkitMeta("GOOGLESHEETS",    "Google Sheets",    ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLESHEETS"}),
    "googledocs":      ToolkitMeta("GOOGLEDOCS",      "Google Docs",      ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLEDOCS"}),
    "googleslides":    ToolkitMeta("GOOGLESLIDES",    "Google Slides",    ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLESLIDES"}),
    "googlemeet":      ToolkitMeta("GOOGLEMEET",      "Google Meet",      ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLEMEET"}),
    "figma":           ToolkitMeta("FIGMA",           "Figma",            ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_FIGMA"}),
}


def toolkit_meta(toolkit_id: str) -> ToolkitMeta:
    meta = TOOLKITS.get(toolkit_id.lower())
    if meta is None:
        raise ValueError(f"Unknown toolkit: {toolkit_id!r}. Known: {sorted(TOOLKITS)}")
    return meta


def _auth_config_id_for(toolkit_id: str, scheme: str) -> str:
    meta = toolkit_meta(toolkit_id)
    env_key = meta.auth_env_keys.get(scheme.upper())
    if not env_key:
        raise ValueError(
            f"Toolkit {meta.slug} does not support auth scheme {scheme!r}. "
            f"Supported: {meta.schemes}"
        )
    value = os.getenv(env_key, "").strip()
    if not value:
        raise RuntimeError(
            f"Composio auth config for {meta.slug}/{scheme} is not configured. "
            f"Set {env_key} in the environment (see Composio dashboard)."
        )
    return value


_client: Any = None


def _composio():
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("COMPOSIO_API_KEY is not set in the environment.")
    try:
        from composio import Composio
    except ImportError as exc:
        raise RuntimeError(
            "The `composio` Python package is not installed. "
            "Install it from requirements.txt (pinned to >=0.18,<0.19 — the "
            "0.7.x range carries GHSA-3mwv-j45g-vp3w; do not install it)."
        ) from exc
    _client = Composio(api_key=api_key)
    return _client


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if obj is None:
            return default
        if isinstance(obj, dict):
            if name in obj:
                obj = obj[name]
                continue
            return default
        if hasattr(obj, name):
            obj = getattr(obj, name)
            continue
        return default
    return obj


def _callback_url() -> str:
    return os.getenv(
        "COMPOSIO_CALLBACK_URL",
        "http://127.0.0.1:5002/api/connectors/composio/callback",
    ).strip()


def initiate_connection(
    user_id: str,
    toolkit_id: str,
    auth_scheme: str = "OAUTH2",
    redirect_uri: Optional[str] = None,
) -> dict[str, Any]:
    # Every toolkit in TOOLKITS is OAUTH2-only, so _auth_config_id_for raises
    # for any other scheme before we get here. Re-add an API_KEY branch (via
    # connected_accounts.initiate) if an API_KEY toolkit is ever registered.
    scheme = auth_scheme.upper()
    auth_config_id = _auth_config_id_for(toolkit_id, scheme)
    callback = redirect_uri or _callback_url()

    request = _composio().connected_accounts.link(
        user_id=user_id,
        auth_config_id=auth_config_id,
        callback_url=callback,
    )
    return {
        "auth_url": _attr(request, "redirect_url"),
        "connection_request_id": _attr(request, "id"),
    }


def check_connection(connection_request_id: str) -> dict[str, Any]:
    client = _composio()
    try:
        record = client.connected_accounts.get(connection_request_id)
    except Exception as exc:
        log.warning("composio: check_connection failed: %s", exc)
        return {"status": "FAILED", "connected_account_id": None, "error": str(exc)}

    return {
        "status": _attr(record, "status", default="PENDING"),
        "connected_account_id": _attr(record, "id"),
    }


def list_connections(
    user_id: str, *, statuses: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    client = _composio()
    list_kwargs: dict[str, Any] = {"user_ids": [user_id]}
    if statuses:
        list_kwargs["statuses"] = statuses
    try:
        page = client.connected_accounts.list(**list_kwargs)
    except Exception as exc:
        log.warning("composio: list_connections failed for user=%s: %s", user_id, exc)
        return []

    items = _attr(page, "items", default=page) or []
    out: list[dict[str, Any]] = []
    for it in items:
        toolkit = (
            _attr(it, "toolkit", "slug", default="")
            or _attr(it, "toolkit_slug", default="")
            or _attr(it, "app", default="")
        )
        out.append({
            "toolkit": str(toolkit).upper() or None,
            "connected_account_id": _attr(it, "id"),
            "status": _attr(it, "status", default="UNKNOWN"),
            "scheme": _attr(it, "auth_scheme", default=None),
        })
    return out


def disconnect(connected_account_id: str) -> bool:
    client = _composio()
    try:
        client.connected_accounts.delete(connected_account_id)
        return True
    except Exception as exc:
        log.warning("composio: disconnect failed: %s", exc)
        return False


def list_tools(
    user_id: str,
    toolkit_id: str,
    *,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    meta = toolkit_meta(toolkit_id)
    client = _composio()
    try:
        tools = client.tools.get_raw_composio_tools(
            toolkits=[meta.slug], limit=200,
        )
    except Exception as exc:
        log.warning("composio: list_tools failed (toolkit=%s): %s", meta.slug, exc)
        return []

    from services.cowork_agent.composio import action_prefs as composio_action_prefs
    from services.cowork_agent.composio import categories as composio_categories

    out: list[dict[str, Any]] = []
    for t in tools:
        slug = _attr(t, "slug", default="") or _attr(t, "name", default="")
        enabled = composio_action_prefs.is_action_enabled(toolkit_id, slug, user_id)
        if not include_disabled and not enabled:
            continue
        entry: dict[str, Any] = {
            "slug": slug,
            "name": _attr(t, "name", default=""),
            "description": _attr(t, "description", default=""),
            "parameters": _attr(t, "input_parameters", default={}),
            "enabled": enabled,
        }
        category = composio_categories.classify(toolkit_id, slug)
        if category is not None:
            entry["category"] = category
        out.append(entry)
    return out


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SESSIONS_PATH = _REPO_ROOT / "data" / "composio_sessions.json"

_SESSION_IDS: dict[str, str] = {}
_PROXY_TOKENS: dict[str, str] = {}
_SESSIONS_LOADED = False


def _load_store() -> tuple[dict[str, str], dict[str, str]]:
    from services.cowork_agent.visualizer.reader import read_json

    data = read_json(_SESSIONS_PATH)
    if not isinstance(data, dict):
        return {}, {}

    def _str_map(raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and k and v
        }

    return _str_map(data.get("sessions")), _str_map(data.get("proxy_tokens"))


def _ensure_sessions_loaded() -> None:
    global _SESSIONS_LOADED
    if _SESSIONS_LOADED:
        return
    try:
        sessions, tokens = _load_store()
        _SESSION_IDS.update(sessions)
        _PROXY_TOKENS.update(tokens)
    except Exception as exc:
        log.warning("composio: could not read session store: %s", exc)
    _SESSIONS_LOADED = True


def _write_store(mutate) -> None:
    from services.cowork_agent.visualizer.atomic_write import write_json_atomic
    from services.cowork_agent.visualizer.flock import locked

    try:
        with locked(_SESSIONS_PATH):
            sessions, tokens = _load_store()
            mutate(sessions, tokens)
            write_json_atomic(
                _SESSIONS_PATH,
                {"version": 2, "sessions": sessions, "proxy_tokens": tokens},
            )
        try:
            _SESSIONS_PATH.chmod(0o600)
        except OSError:
            pass
    except Exception as exc:
        log.warning("composio: could not persist session store: %s", exc)


def _persist_session_id(user_id: str, session_id: Optional[str]) -> None:
    def _mutate(sessions: dict[str, str], _tokens: dict[str, str]) -> None:
        if session_id:
            sessions[user_id] = session_id
        else:
            sessions.pop(user_id, None)

    _write_store(_mutate)


def proxy_token_for_user(user_id: str) -> str:
    uid = _require_user_id(user_id, "proxy_token_for_user")
    _ensure_sessions_loaded()
    for token, owner in _PROXY_TOKENS.items():
        if owner == uid:
            return token

    token = secrets.token_urlsafe(32)
    _PROXY_TOKENS[token] = uid

    def _mutate(_sessions: dict[str, str], tokens: dict[str, str]) -> None:
        for existing, owner in tokens.items():
            if owner == uid:
                _PROXY_TOKENS.pop(token, None)
                _PROXY_TOKENS[existing] = uid
                return
        tokens[token] = uid

    _write_store(_mutate)
    for tok, owner in _PROXY_TOKENS.items():
        if owner == uid:
            return tok
    return token


def user_for_proxy_token(token: str) -> Optional[str]:
    """Resolve an MCP proxy token to its owning principal.

    Rows written before workspace scoping hold a bare account id. Honouring one
    would let an agent whose MCP config predates the change keep reaching the
    account-wide Composio bucket — a silent bypass of per-workspace isolation —
    so unscoped rows are rejected rather than upgraded. The caller turns None
    into a 401 that tells the agent to re-install its config, which re-mints the
    token against the current principal.
    """
    if not token:
        return None
    _ensure_sessions_loaded()
    user_id = _PROXY_TOKENS.get(token)
    if not user_id:
        try:
            _, tokens = _load_store()
        except Exception:
            return None
        _PROXY_TOKENS.update(tokens)
        user_id = _PROXY_TOKENS.get(token)
    if user_id and not tenancy.is_scoped(user_id):
        log.warning(
            "composio: proxy token maps to a pre-workspace-scoping principal; "
            "rejecting it. Re-install the agent's MCP config via "
            "POST /api/connectors/composio/refresh-gateway."
        )
        return None
    return user_id


def _delete_remote_session(session_id: str, user_id: str) -> None:
    try:
        _composio().sessions.delete(session_id)
        log.info("composio: deleted session %s for user=%s", session_id, user_id)
    except Exception as exc:
        log.warning(
            "composio: could not delete session %s for user=%s (it may linger "
            "server-side): %s", session_id, user_id, exc,
        )


def _disabled_tools_config(user_id: str) -> dict[str, dict[str, list[str]]]:
    from services.cowork_agent.composio import action_prefs as composio_action_prefs

    try:
        prefs = composio_action_prefs.load_prefs(user_id)
    except Exception as exc:
        log.warning("composio: could not read action prefs for user=%s: %s", user_id, exc)
        prefs = {}
    return {
        toolkit_id: {
            "disable": sorted(
                slug
                for slug, enabled in prefs.get(toolkit_id, {}).items()
                if enabled is False
            )
        }
        for toolkit_id in TOOLKITS
    }


def _pinned_connected_accounts(user_id: str) -> dict[str, list[str]]:
    pinned: dict[str, list[str]] = {}
    try:
        rows = list_connections(user_id, statuses=["ACTIVE"])
    except Exception as exc:
        log.warning("composio: list_connections failed while building pin map for user=%s: %s", user_id, exc)
        return pinned
    for row in rows:
        if (row.get("status") or "").upper() != "ACTIVE":
            continue
        slug = (row.get("toolkit") or "").lower()
        cid = row.get("connected_account_id")
        if not slug or not cid:
            continue
        pinned.setdefault(slug, []).append(cid)
    return pinned


def invalidate_session(user_id: str) -> None:
    if not user_id:
        return
    _ensure_sessions_loaded()
    session_id = _SESSION_IDS.pop(user_id, None)
    _persist_session_id(user_id, None)
    if session_id:
        _delete_remote_session(session_id, user_id)


def sync_session(user_id: str) -> None:
    if not user_id:
        return
    _ensure_sessions_loaded()
    sid = _SESSION_IDS.get(user_id)
    if not sid:
        return
    try:
        session = _composio().use(sid)
        session.update(
            connected_accounts=_pinned_connected_accounts(user_id),
            tools=_disabled_tools_config(user_id),
        )
        log.info("composio: updated session %s for user=%s", sid, user_id)
    except Exception as exc:
        log.warning(
            "composio: session update failed for user=%s, falling back to re-mint: %s",
            user_id, exc,
        )
        invalidate_session(user_id)


def get_session(user_id: str):
    user_id = _require_user_id(user_id, "get_session")
    _ensure_sessions_loaded()
    sid = _SESSION_IDS.get(user_id)
    if sid:
        try:
            return _composio().use(sid)
        except Exception as exc:
            log.debug("composio: use(%s) failed for user=%s: %s", sid, user_id, exc)
            _SESSION_IDS.pop(user_id, None)
            _persist_session_id(user_id, None)
    create_kwargs: dict[str, Any] = {
        "user_id": user_id,
        "tools": _disabled_tools_config(user_id),
        "mcp": True,
    }
    pinned = _pinned_connected_accounts(user_id)
    if pinned:
        create_kwargs["connected_accounts"] = pinned
    session = _composio().create(**create_kwargs)
    new_id = getattr(session, "session_id", None) or getattr(session, "id", None)
    if new_id:
        _SESSION_IDS[user_id] = str(new_id)
        _persist_session_id(user_id, str(new_id))
    return session


def build_mcp_server_entry(user_id: str) -> dict[str, Any]:
    session = get_session(user_id)
    url = _attr(session, "mcp", "url")
    headers = _attr(session, "mcp", "headers", default=None)
    if not url:
        # Without this the entry would carry the literal string "None", which is
        # truthy — it passes every downstream guard and fails much later as an
        # opaque connection error.
        raise RuntimeError(
            f"composio: session for user={user_id} exposed no MCP url."
        )
    entry: dict[str, Any] = {"type": "http", "url": str(url)}
    if headers:
        entry["headers"] = dict(headers)
    log.info(
        "composio: session %s for user=%s -> %s",
        _SESSION_IDS.get(user_id, "?"), user_id, url,
    )
    return entry


def _composio_proxy_url(user_id: str) -> str:
    uid = _require_user_id(user_id, "_composio_proxy_url")
    port = int(os.getenv("PORT", "5002"))
    return f"http://127.0.0.1:{port}/mcp/composio-proxy/u/{proxy_token_for_user(uid)}"


def install_into_gateway(user_id: str, agent: str) -> dict[str, Any]:
    from services.cowork_agent.adapters.loader import try_load_capability

    mod = try_load_capability("mcp_install", agent=agent)
    if mod is None or not hasattr(mod, "install"):
        return {
            "ok": False,
            "error": (
                f"Agent '{agent}' does not support gateway MCP install "
                "(no mcp_install capability)."
            ),
        }
    try:
        proxy_url = _composio_proxy_url(user_id)
    except Exception as exc:
        log.warning("composio: gateway install could not build proxy URL: %s", exc)
        return {"ok": False, "error": str(exc)}
    return mod.install(proxy_url)


def gateway_install_agents() -> list[str]:
    from services.cowork_agent.adapters.loader import try_load_capability
    from services.cowork_agent.registry.adapter_registry import list_adapters

    out: list[str] = []
    for name in list_adapters():
        mod = try_load_capability("mcp_install", agent=name)
        if mod is not None and hasattr(mod, "install"):
            out.append(name)
    return sorted(out)
