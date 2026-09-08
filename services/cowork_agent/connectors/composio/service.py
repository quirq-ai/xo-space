from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from services.cowork_agent.connectors.composio import credentials, paths, state

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
    # May raise CredentialsUnavailable when the whole bundle is missing; the router
    # maps that to 422 on /connect.
    value = credentials.auth_config_id(env_key)
    if not value:
        raise RuntimeError(
            f"Composio auth config for {meta.slug}/{scheme} is not configured. "
            f"Set {env_key} where this install reads its Composio credentials — "
            f"xo-swarm-api's environment, or locally with "
            f"COMPOSIO_CREDENTIALS_SOURCE=env (see Composio dashboard)."
        )
    return value


_client: Any = None
# The api key `_client` was built with. Keying the memo on the credential is what makes
# a rotation self-invalidating, with no cross-module wiring.
#
# Existing sessions are deliberately *not* purged: a rotation within the same Composio
# project keeps them valid, and get_session already re-mints one whose `use()` raises.
_client_key: str = ""


def _composio():
    global _client, _client_key
    # Raises CredentialsUnavailable (a RuntimeError) whose message always contains the
    # literal "COMPOSIO_API_KEY" — the string the Connectors tab matches on.
    api_key = credentials.api_key()
    if _client is not None and _client_key == api_key:
        return _client
    try:
        from composio import Composio
    except ImportError as exc:
        raise RuntimeError(
            "The `composio` Python package is not installed. "
            "Install it from requirements.txt (pinned to >=0.18,<0.19 — the "
            "0.7.x range carries GHSA-3mwv-j45g-vp3w; do not install it)."
        ) from exc
    _client = Composio(api_key=api_key)
    _client_key = api_key
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
    """This deployment's public OAuth callback. Required; there is no default.

    Fails closed on purpose. A wrong-origin callback is accepted by /connect and
    only breaks later, inside the popup, when the provider refuses the redirect —
    so guessing loopback here buys a 200 that lies. Read per call, so a test or a
    reload sees the current value.
    """
    url = os.getenv("COMPOSIO_CALLBACK_URL", "").strip()
    if not url:
        raise RuntimeError(
            "COMPOSIO_CALLBACK_URL is not set. Set it to this deployment's public "
            "callback URL (e.g. https://<origin>/api/connectors/composio/callback) "
            "and register that origin as an allowed callback on the Composio auth "
            "configs in the dashboard."
        )
    return url


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Composio rejects a max outside this range; clamped so an operator typo cannot 400
# every session creation.
MULTI_ACCOUNT_MIN_MAX = 2
MULTI_ACCOUNT_MAX_MAX = 10
MULTI_ACCOUNT_DEFAULT_MAX = 5

ALIAS_MAX_LENGTH = 128


def multi_account_config() -> Optional[dict[str, Any]]:
    """The session `multi_account` block, or None when the feature is off.

    Off is the Composio default: one account per toolkit per session, the most
    recently connected one. Turning it on lets an account hold several accounts
    for the same toolkit (work and personal Gmail) inside one session.
    """
    if not _env_flag("COMPOSIO_MULTI_ACCOUNT"):
        return None
    raw = os.getenv("COMPOSIO_MULTI_ACCOUNT_MAX", "").strip()
    try:
        max_accounts = int(raw) if raw else MULTI_ACCOUNT_DEFAULT_MAX
    except ValueError:
        log.warning(
            "composio: COMPOSIO_MULTI_ACCOUNT_MAX=%r is not an integer; using %d.",
            raw, MULTI_ACCOUNT_DEFAULT_MAX,
        )
        max_accounts = MULTI_ACCOUNT_DEFAULT_MAX
    clamped = max(MULTI_ACCOUNT_MIN_MAX, min(MULTI_ACCOUNT_MAX_MAX, max_accounts))
    if clamped != max_accounts:
        log.warning(
            "composio: COMPOSIO_MULTI_ACCOUNT_MAX=%d is outside %d-%d; using %d.",
            max_accounts, MULTI_ACCOUNT_MIN_MAX, MULTI_ACCOUNT_MAX_MAX, clamped,
        )
    return {
        "enable": True,
        "max_accounts_per_toolkit": clamped,
        # When true, an agent must name an account via the tool call's `account`
        # parameter instead of silently getting the most recent.
        "require_explicit_selection": _env_flag(
            "COMPOSIO_MULTI_ACCOUNT_REQUIRE_SELECTION"
        ),
    }


def multi_account_enabled() -> bool:
    return multi_account_config() is not None


def normalize_alias(alias: Optional[str]) -> Optional[str]:
    """Fold an alias to its stored form: trimmed, or None to mean "cleared"."""
    text = (alias or "").strip()
    if not text:
        return None
    if len(text) > ALIAS_MAX_LENGTH:
        raise ValueError(
            f"Alias is too long ({len(text)} chars); the limit is {ALIAS_MAX_LENGTH}."
        )
    return text


class AliasInUseError(ValueError):
    """An alias is already taken by another account of the same toolkit."""


def assert_alias_free(
    user_id: str,
    toolkit_id: str,
    alias: str,
    *,
    except_account_id: Optional[str] = None,
) -> None:
    """Composio requires an alias to be unique per user and toolkit.

    Checked here so a collision is a 409 naming the account that holds the
    alias, rather than an opaque SDK error surfaced as a 502.
    """
    slug = toolkit_meta(toolkit_id).slug
    folded = alias.casefold()
    for row in list_connections(user_id, toolkit_slugs=[slug]):
        if row.get("connected_account_id") == except_account_id:
            continue
        existing = (row.get("alias") or "").strip()
        if existing and existing.casefold() == folded:
            raise AliasInUseError(
                f"Alias {alias!r} is already used by connected account "
                f"{row.get('connected_account_id')} on {slug}."
            )


def initiate_connection(
    user_id: str,
    toolkit_id: str,
    auth_scheme: str = "OAUTH2",
    redirect_uri: Optional[str] = None,
    alias: Optional[str] = None,
    allow_multiple: bool = False,
) -> dict[str, Any]:
    # OAUTH2-only: _auth_config_id_for raises for any other scheme before we get here.
    # Add an API_KEY branch (connected_accounts.initiate) if such a toolkit is registered.
    scheme = auth_scheme.upper()
    auth_config_id = _auth_config_id_for(toolkit_id, scheme)
    callback = redirect_uri or _callback_url()
    alias = normalize_alias(alias)
    if alias:
        assert_alias_free(user_id, toolkit_id, alias)

    link_kwargs: dict[str, Any] = {
        "user_id": user_id,
        "auth_config_id": auth_config_id,
        "callback_url": callback,
    }
    if alias:
        link_kwargs["alias"] = alias
    if allow_multiple:
        # Without this Composio reuses/replaces the existing account for this
        # user + auth config instead of adding a second one.
        link_kwargs["allow_multiple"] = True

    request = _composio().connected_accounts.link(**link_kwargs)
    return {
        "auth_url": _attr(request, "redirect_url"),
        "connection_request_id": _attr(request, "id"),
        "alias": alias,
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
    user_id: str,
    *,
    statuses: Optional[list[str]] = None,
    toolkit_slugs: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    client = _composio()
    list_kwargs: dict[str, Any] = {"user_ids": [user_id]}
    if statuses:
        list_kwargs["statuses"] = statuses
    if toolkit_slugs:
        list_kwargs["toolkit_slugs"] = [s.lower() for s in toolkit_slugs]
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
            # `alias` is what an agent passes as a tool call's `account`; `created_at`
            # is what "most recently connected" means when no account is named.
            "alias": _attr(it, "alias", default=None),
            "created_at": _attr(it, "created_at", default=None),
            "is_disabled": bool(_attr(it, "is_disabled", default=False)),
        })
    return out


def newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort connection rows newest-first, tolerating a missing created_at.

    Composio's ISO-8601 timestamps sort lexicographically, so no parsing is
    needed; a row with no timestamp sorts last rather than crashing the sort.
    """
    return sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)


def list_toolkit_accounts(user_id: str, toolkit_id: str) -> list[dict[str, Any]]:
    """Every connected account this XO account holds for one toolkit.

    Newest first, which is also the order Composio resolves "the default
    account" in when a tool call names none.
    """
    meta = toolkit_meta(toolkit_id)
    rows = [
        row
        for row in list_connections(user_id, toolkit_slugs=[meta.slug])
        # Belt and braces: an SDK that ignored the server-side slug filter would
        # otherwise leak other toolkits' accounts into this list.
        if (row.get("toolkit") or "").upper() == meta.slug
    ]
    return newest_first(rows)


def set_alias(connected_account_id: str, alias: Optional[str]) -> Optional[str]:
    """Set or clear one connected account's alias.

    Returns the stored alias (None when cleared). Raises RuntimeError on an SDK
    failure — unlike the read paths, a silent no-op here would leave the caller
    believing a rename happened.
    """
    normalized = normalize_alias(alias)
    try:
        _composio().connected_accounts.update(
            connected_account_id, alias=normalized or "",
        )
    except Exception as exc:
        log.warning(
            "composio: set_alias failed for account=%s: %s", connected_account_id, exc,
        )
        raise RuntimeError(f"Composio rejected the alias update: {exc}") from exc
    return normalized


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

    from services.cowork_agent.connectors.composio import action_prefs as composio_action_prefs
    from services.cowork_agent.connectors.composio import categories as composio_categories

    # Read the prefs ONCE, not per slug inside the loop: that is up to 200 reads of the
    # whole store per request.
    disabled = composio_action_prefs.disabled_slugs(toolkit_id)

    out: list[dict[str, Any]] = []
    for t in tools:
        slug = _attr(t, "slug", default="") or _attr(t, "name", default="")
        enabled = slug not in disabled
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


# The store lives in the user's config directory, never the checkout — see paths.py.
# Resolved once at import; use sites read this global rather than re-resolving, which is
# what keeps `patch.object(service, "_SESSIONS_PATH", ...)` a working test seam.
_SESSIONS_PATH = paths.store_dir() / "sessions.json"
_LEGACY_SESSIONS_PATHS = (paths.legacy_checkout_path("composio_sessions.json"),)

# v4 dropped the per-principal maps: a pod serves one workspace and Composio is addressed
# by the bare account id, so there is exactly one session and one account here.
STORE_VERSION = 4

_SESSION_ID: Optional[str] = None
_PROXY_TOKENS: set[str] = set()
_STORE_ACCOUNT: Optional[str] = None
_SESSIONS_LOADED = False

# Session ids from a store this pod will not adopt. Composio sessions never expire, so
# they would linger server-side forever. Drained by the boot sweep, never by whoever loads
# the store first — that is the MCP hot path and it must not touch the network.
_ORPHANED_SESSION_IDS: list[str] = []


class NoToolkitsEnabled(RuntimeError):
    """This workspace has not enabled any toolkit, so it has no session.

    Not an error condition so much as a state: connections are account-wide and every
    workspace opts in to the ones it wants (see :mod:`.workspace_scope`). Carried as an
    exception because the MCP proxy has to answer *something*, and "no connectors are
    enabled in this workspace" is a far better answer than an empty tool list that looks
    like a broken integration.
    """


def _load_store() -> tuple[Optional[str], Optional[str], Optional[str], set[str]]:
    """Read the store, returning ``(workspace, account, session_id, proxy_tokens)``.

    ``workspace`` is the stamp: the ``CODER_WORKSPACE_ID`` of the pod that wrote the
    document. It is what lets this pod tell its own store from one restored out of a
    backup or another workspace's home directory — with connections now account-wide,
    adopting a foreign store would mean inheriting that workspace's connector scope.

    Anything below v4 is discarded rather than upgraded. Those rows are keyed by the
    retired ``<account>__ws__<workspace>`` tenant key and their sessions were minted
    against it, so every one of them addresses a Composio user that is no longer ours.
    Their session ids are parked in ``_ORPHANED_SESSION_IDS`` for the boot sweep to
    delete.
    """
    from services.cowork_agent.visualizer.reader import read_json

    paths.migrate_legacy(_SESSIONS_PATH, _LEGACY_SESSIONS_PATHS, mode=0o600)
    data = read_json(_SESSIONS_PATH)
    if not isinstance(data, dict):
        return None, None, None, set()
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0

    if version < STORE_VERSION:
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            for sid in sessions.values():
                if isinstance(sid, str) and sid and sid not in _ORPHANED_SESSION_IDS:
                    _ORPHANED_SESSION_IDS.append(sid)
        log.info(
            "composio: discarding a v%d session store. Its sessions were minted against "
            "the retired workspace-scoped user id; a fresh one is minted on demand.",
            version,
        )
        return None, None, None, set()

    workspace = str(data.get("workspace_id") or "").strip() or None
    account = str(data.get("account_id") or "").strip() or None
    session_id = str(data.get("session") or "").strip() or None
    raw_tokens = data.get("proxy_tokens")
    tokens = {
        str(t) for t in raw_tokens if isinstance(t, str) and t
    } if isinstance(raw_tokens, list) else set()
    return workspace, account, session_id, tokens


def _ensure_sessions_loaded() -> None:
    """Populate the in-memory mirrors from disk, if the store is this workspace's.

    Classifying the document needs no network: the stamp is compared against this pod's
    own ``CODER_WORKSPACE_ID``. That matters because proxy-token resolution runs on every
    agent ``tools/call``.

    A store stamped for another workspace is left alone on disk and simply not adopted —
    it is somebody's restored backup, and destroying it here would be an odd thing for a
    read to do. The next write replaces it.
    """
    global _SESSIONS_LOADED, _SESSION_ID, _STORE_ACCOUNT
    if _SESSIONS_LOADED:
        return
    try:
        workspace, account, session_id, tokens = _load_store()
    except Exception as exc:
        log.warning("composio: could not read session store: %s", exc)
        _SESSIONS_LOADED = True
        return

    try:
        mine = state.workspace_id()
    except state.WorkspaceIdentityUnavailable:
        # No stamp to compare against. Leave _SESSIONS_LOADED False so a later call
        # retries once the pod's environment is complete.
        return

    if workspace and workspace != mine:
        log.warning(
            "composio: ignoring a session store stamped for a different workspace. "
            "It was most likely restored from a backup; this workspace mints its own.",
        )
        if session_id and session_id not in _ORPHANED_SESSION_IDS:
            _ORPHANED_SESSION_IDS.append(session_id)
        _SESSIONS_LOADED = True
        return

    _SESSION_ID = session_id
    _PROXY_TOKENS.update(tokens)
    if account:
        _STORE_ACCOUNT = account
        state.adopt_account_id(account)
    _SESSIONS_LOADED = True


def _write_store(mutate) -> None:
    """Lock, re-read, mutate, atomically replace — stamped with this workspace.

    ``mutate(session_id, tokens) -> (session_id, tokens)`` sees what is on disk, not the
    in-memory mirror, so two processes sharing a store converge instead of clobbering.
    """
    global _STORE_ACCOUNT
    from services.cowork_agent.visualizer.atomic_write import write_json_atomic
    from services.cowork_agent.visualizer.flock import locked

    try:
        workspace = state.workspace_id()
    except state.WorkspaceIdentityUnavailable as exc:
        log.warning("composio: refusing to write an unstamped session store: %s", exc)
        return

    account = _STORE_ACCOUNT or state.account_id_if_known()
    try:
        # Before the lock: the sentinel is keyed on the store's absolute path.
        paths.migrate_legacy(_SESSIONS_PATH, _LEGACY_SESSIONS_PATHS, mode=0o600)
        with locked(_SESSIONS_PATH):
            existing_ws, existing_account, session_id, tokens = _load_store()
            if existing_ws and existing_ws != workspace:
                # Another workspace's document. Do not merge its rows into ours.
                session_id, tokens = None, set()
            elif existing_account:
                account = account or existing_account
            session_id, tokens = mutate(session_id, set(tokens))
            write_json_atomic(
                _SESSIONS_PATH,
                {
                    "version": STORE_VERSION,
                    "workspace_id": workspace,
                    "account_id": account,
                    "session": session_id,
                    "proxy_tokens": sorted(tokens),
                },
            )
        if account:
            _STORE_ACCOUNT = account
        try:
            _SESSIONS_PATH.chmod(0o600)
        except OSError:
            pass
    except Exception as exc:
        log.warning("composio: could not persist session store: %s", exc)


def _persist_session_id(session_id: Optional[str]) -> None:
    def _mutate(_existing: Optional[str], tokens: set[str]):
        return session_id, tokens

    _write_store(_mutate)


def proxy_token() -> str:
    """The stable opaque MCP proxy token for this workspace, minting one if needed.

    Idempotent on purpose: the boot-time gateway install calls this on every restart, and
    churning the token would strand agents holding the previous URL.

    The token never leaves this pod: it is minted here and stored in the 0600
    ``sessions.json``, which is also the only thing that can resolve it. A store that is
    lost takes every agent's proxy URL with it, and the next sweep mints a fresh token and
    rewrites every agent's MCP config.
    """
    _ensure_sessions_loaded()
    for token in sorted(_PROXY_TOKENS):
        return token

    minted = secrets.token_urlsafe(32)
    chosen: list[str] = []

    def _mutate(session_id: Optional[str], tokens: set[str]):
        # Another process may have minted one between our read and this lock; prefer
        # whatever is already on disk so both agree.
        existing = sorted(tokens)
        if existing:
            chosen.append(existing[0])
            return session_id, tokens
        chosen.append(minted)
        tokens.add(minted)
        return session_id, tokens

    _write_store(_mutate)
    token = chosen[0] if chosen else minted
    _PROXY_TOKENS.add(token)
    return token


def account_for_proxy_token_local(token: str) -> Optional[str]:
    """Resolve a proxy token to this workspace's Composio account id. No network."""
    if not token:
        return None
    _ensure_sessions_loaded()
    if token not in _PROXY_TOKENS:
        # A row written by another process since this one last read. _load_store has
        # already refused anything stamped for a different workspace.
        try:
            workspace, account, _session, tokens = _load_store()
        except Exception:
            return None
        try:
            mine = state.workspace_id()
        except state.WorkspaceIdentityUnavailable:
            return None
        if workspace and workspace != mine:
            return None
        _PROXY_TOKENS.update(tokens)
        if account:
            state.adopt_account_id(account)
        if token not in _PROXY_TOKENS:
            return None
    return _STORE_ACCOUNT or state.account_id_if_known()


async def account_for_proxy_token(token: str) -> Optional[str]:
    """Resolve an MCP proxy token to this workspace's Composio account id.

    **Purely local.** This pod's ``sessions.json`` is the only thing that knows a proxy
    token, so this runs on `initialize`, `tools/list` and every `tools/call` as a set
    lookup with no network. A token this pod cannot place is unknown, full stop: the proxy
    answers 401 and the agent re-reads the config the next sweep rewrites.

    Async because the MCP proxy awaits it and the lookup is on that hot path; nothing here
    blocks.
    """
    return account_for_proxy_token_local(token)


def _delete_remote_session(session_id: str) -> None:
    try:
        _composio().sessions.delete(session_id)
        log.info("composio: deleted session %s", session_id)
    except Exception as exc:
        log.warning(
            "composio: could not delete session %s (it may linger server-side): %s",
            session_id, exc,
        )


def drain_orphaned_sessions() -> int:
    """Delete sessions belonging to a store this pod would not adopt. Returns the count.

    Called from the boot sweep rather than from whoever first reads the store, because
    that reader is usually the MCP proxy and a network call there would sit on the agent
    hot path. Best-effort throughout: a session that cannot be deleted is dropped from
    the queue anyway, since retrying it forever would re-block every boot.
    """
    if not _ORPHANED_SESSION_IDS:
        return 0
    pending, _ORPHANED_SESSION_IDS[:] = list(_ORPHANED_SESSION_IDS), []
    for session_id in pending:
        _delete_remote_session(session_id)
    return len(pending)


def _disabled_tools_config() -> dict[str, dict[str, list[str]]]:
    from services.cowork_agent.connectors.composio import action_prefs as composio_action_prefs

    try:
        prefs = composio_action_prefs.load_prefs()
    except Exception as exc:
        log.warning("composio: could not read action prefs: %s", exc)
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


def max_accounts_per_toolkit() -> int:
    """How many connected accounts one toolkit may pin in a session.

    Composio rejects a session pinning more than this, and a session without
    multi-account mode accepts exactly one.
    """
    multi = multi_account_config()
    return int(multi["max_accounts_per_toolkit"]) if multi else 1


def prune_scope_to_live_accounts(user_id: str) -> bool:
    """Drop pinned accounts that no longer exist in Composio. Returns True if any went.

    Runs before every session create and update. Composio requires a pinned connected
    account to exist and be enabled, and **one stale id fails the entire session**, not
    just its toolkit. A connection deleted from another workspace cannot reach into this
    pod's store, so this is what makes that deletion self-heal here.
    """
    from services.cowork_agent.connectors.composio import workspace_scope

    try:
        rows = list_connections(user_id, statuses=["ACTIVE"])
    except Exception as exc:
        log.warning(
            "composio: could not list connections while pruning scope for user=%s: %s",
            user_id, exc,
        )
        return False
    live = {
        row.get("connected_account_id")
        for row in rows
        if row.get("connected_account_id") and not row.get("is_disabled")
    }
    return workspace_scope.prune_to(live)


def _session_config(user_id: str) -> dict[str, Any]:
    """The toolkits/tools/connected_accounts this workspace's session is built from."""
    from services.cowork_agent.connectors.composio import workspace_scope

    prune_scope_to_live_accounts(user_id)
    enabled = workspace_scope.enabled_toolkits()
    if not enabled:
        raise NoToolkitsEnabled(
            "No connectors are enabled in this workspace. Connections are shared across "
            "the account; enable the ones this workspace should use on the Connectors "
            "tab."
        )
    config: dict[str, Any] = {
        # Checked before Composio looks up a connection, so this is the outer boundary.
        "toolkits": {"enable": enabled},
        "tools": _disabled_tools_config(),
    }
    pinned = workspace_scope.pins()
    if pinned:
        # An exact override with no fallback: without it Composio resolves the most
        # recently connected account at execution time, so a connection made in another
        # workspace could silently repoint this one.
        config["connected_accounts"] = pinned
    multi = multi_account_config()
    if multi:
        config["multi_account"] = multi
    return config


def invalidate_session() -> None:
    global _SESSION_ID
    _ensure_sessions_loaded()
    session_id, _SESSION_ID = _SESSION_ID, None
    _persist_session_id(None)
    if session_id:
        _delete_remote_session(session_id)


def sync_session(user_id: str) -> None:
    """Push this workspace's current scope onto its live session, if it has one."""
    if not user_id:
        return
    _ensure_sessions_loaded()
    sid = _SESSION_ID
    if not sid:
        return
    try:
        config = _session_config(user_id)
    except NoToolkitsEnabled:
        # Nothing left enabled here. Drop the session rather than leaving one behind
        # that still reaches whatever it was last configured with.
        invalidate_session()
        return
    try:
        session = _composio().use(sid)
        # Passed even when absent from the config: that is how a session minted while
        # the flag was on converges after an operator turns it off.
        session.update(
            connected_accounts=config.get("connected_accounts", {}),
            toolkits=config["toolkits"],
            tools=config["tools"],
            multi_account=multi_account_config(),
        )
        log.info("composio: updated session %s", sid)
    except Exception as exc:
        log.warning(
            "composio: session update failed, falling back to re-mint: %s", exc,
        )
        invalidate_session()


def get_session(user_id: str):
    """This workspace's Composio session, minting one if needed.

    ``user_id`` is the bare account id — connections belong to the account, not to a
    workspace. What *this* workspace may reach comes from :func:`_session_config`.

    Raises :class:`NoToolkitsEnabled` when the workspace has enabled nothing. Composio's
    behaviour for an empty ``toolkits`` allowlist is unspecified, and "everything" would
    be the catastrophic reading of it, so the session is never created in that state.
    """
    global _SESSION_ID
    user_id = _require_user_id(user_id, "get_session")
    _ensure_sessions_loaded()
    config = _session_config(user_id)          # raises before any network call

    sid = _SESSION_ID
    if sid:
        try:
            return _composio().use(sid)
        except Exception as exc:
            log.debug("composio: use(%s) failed: %s", sid, exc)
            _SESSION_ID = None
            _persist_session_id(None)

    session = _composio().create(user_id=user_id, mcp=True, **config)
    new_id = getattr(session, "session_id", None) or getattr(session, "id", None)
    if new_id:
        _SESSION_ID = str(new_id)
        _persist_session_id(str(new_id))
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
    log.info("composio: session %s -> %s", _SESSION_ID or "?", url)
    return entry


def _composio_proxy_url() -> str:
    port = int(os.getenv("PORT", "5002"))
    return f"http://127.0.0.1:{port}/mcp/composio-proxy/u/{proxy_token()}"


def install_into_gateway(
    agent: str, *, proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    """Point one agent's config at this workspace's proxy.

    Synchronous, and it may block: minting the proxy URL reads and may rewrite this pod's
    token store. The reconcile sweep therefore mints once and passes ``proxy_url`` in,
    rather than paying that per agent.

    Takes no identity: the proxy URL carries an opaque token that only this pod can
    resolve, and the account behind it is whatever ``sessions.json`` is stamped with.
    """
    from services.cowork_agent.connectors.composio import mcp

    target = mcp.load_target(agent)
    if target is None:
        return {
            "ok": False,
            "error": (
                f"Agent '{agent}' does not support gateway MCP install "
                f"(no enabled 'mcp' block in config/agents/{agent}/manifest.json)."
            ),
        }
    if proxy_url is None:
        try:
            proxy_url = _composio_proxy_url()
        except Exception as exc:
            log.warning("composio: gateway install could not build proxy URL: %s", exc)
            return {"ok": False, "error": str(exc)}
    return mcp.apply(target, proxy_url)


def gateway_install_agents() -> list[str]:
    from services.cowork_agent.connectors.composio import mcp

    return mcp.agents_with_targets()


# ── The reconcile sweep ─────────────────────────────────────────────────────
#
# Installing the MCP gateway into agents is automatic and has no manual path. The sweep
# is idempotent and runs at boot with backoff, on a timer, and when the Connectors tab
# loads — which between them close every case a one-shot boot install would miss: XO
# unreachable at boot, an agent config that did not exist yet, an agent that rewrote its
# config and dropped the entry, a token the swarm never recorded.


@dataclass(frozen=True)
class GatewaySweep:
    """The outcome of one :func:`install_gateways` pass."""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    # None when the sweep ran; otherwise which gate stopped it: "no_agents",
    # "no_credential", "no_workspace" or "account_unavailable".
    skipped: Optional[str] = None
    # Whether waiting can help. Only an unreachable swarm changes on its own: the XO
    # credential is fixed at boot and the workspace id is injected by the pod.
    retryable: bool = False
    # One sentence for the console, where the boot summary is the only thing read.
    detail: str = ""

    @property
    def ran(self) -> bool:
        return self.skipped is None


_SWEEP_LOCK: Optional[asyncio.Lock] = None
_SWEEP_TASK: Optional["asyncio.Task[GatewaySweep]"] = None
_LAST_SWEEP_AT = 0.0                     # monotonic; 0 = never
_LAST_ERRORS: dict[str, str] = {}        # agent -> last printed error, so sweeps stay quiet
_RETRY_DELAYS: tuple[int, ...] = (5, 15, 30, 60, 120, 300)   # while XO is unreachable
_KICK_MIN_INTERVAL = 30.0                # seconds between sweeps a page load may start
_DEFAULT_RECONCILE_INTERVAL = 600.0
_sleep = asyncio.sleep                   # module attribute so tests can shorten the loop


def _sweep_lock() -> asyncio.Lock:
    # Created lazily so importing this module does not require a running loop.
    global _SWEEP_LOCK
    if _SWEEP_LOCK is None:
        _SWEEP_LOCK = asyncio.Lock()
    return _SWEEP_LOCK


def reconcile_interval() -> float:
    """Seconds between periodic sweeps: ``COMPOSIO_MCP_RECONCILE_INTERVAL``, default 600.

    ``0`` (or a negative number) disables the periodic pass — the boot sweep, its
    retries and the Connectors-tab kick still run.
    """
    raw = (os.getenv("COMPOSIO_MCP_RECONCILE_INTERVAL") or "").strip()
    if not raw:
        return _DEFAULT_RECONCILE_INTERVAL
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "composio: COMPOSIO_MCP_RECONCILE_INTERVAL=%r is not a number; using %s.",
            raw, _DEFAULT_RECONCILE_INTERVAL,
        )
        return _DEFAULT_RECONCILE_INTERVAL


def _report(agent: str, result: dict[str, Any], announce: bool) -> None:
    # print, not log.info: `services.*` loggers are not wired to a handler, and the boot
    # summary is the only place anyone looks. Later sweeps print only what changed.
    if result.get("ok"):
        _LAST_ERRORS.pop(agent, None)
        if result.get("changed") is False:
            if announce:
                print(f"   Composio MCP: {agent} already current ({result.get('config_path')})")
        else:
            print(f"✅ Composio MCP installed for {agent}: {result.get('config_path')}")
        return
    error = str(result.get("error") or "")
    if announce or _LAST_ERRORS.get(agent) != error:
        # Expected when an agent isn't provisioned on this host — its config file
        # simply doesn't exist yet. A later sweep installs once it appears.
        print(f"⚠️ Composio MCP skipped for {agent}: {error}")
    _LAST_ERRORS[agent] = error


def _apply_to_agents(agents: list[str], announce: bool) -> dict[str, dict[str, Any]]:
    """The blocking half of a sweep, run off the event loop.

    Mints the proxy URL once — that is one read of this pod's token store — then writes
    every agent. Also drains any sessions left behind by a store this pod would not
    adopt; this is the one place a network call for that is safe to make.
    """
    try:
        drain_orphaned_sessions()
    except Exception as exc:
        log.warning("composio: could not drain orphaned sessions: %s", exc)

    try:
        proxy_url = _composio_proxy_url()
    except Exception as exc:
        log.warning("composio: gateway install could not build proxy URL: %s", exc)
        results = {agent: {"ok": False, "error": str(exc)} for agent in agents}
        for agent, result in results.items():
            _report(agent, result, announce)
        return results

    results: dict[str, dict[str, Any]] = {}
    for agent in agents:
        try:
            result = install_into_gateway(agent, proxy_url=proxy_url)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        results[agent] = result
        _report(agent, result, announce)
    return results


async def install_gateways(*, announce: bool = True) -> GatewaySweep:
    """One idempotent sweep: point every agent whose manifest declares an enabled
    ``mcp`` block at this workspace's Composio proxy.

    Identity: ``install_into_gateway`` wants this account's Composio user id, and there
    is no request to carry one. The backend holds its own XO credential, so it asks
    xo-swarm-api directly — the same fetch every later request reads from cache, so
    this also warms it.

    Fail closed and quietly: no credential, no workspace identity, or an unreachable
    swarm means nothing is installed and the agents keep whatever config they already
    have. The returned :class:`GatewaySweep` says which gate closed and whether a
    later sweep can pass it. Never raises; nothing here is fatal to boot.

    Single-flight: the boot loop and a Connectors-tab kick share one lock, so two
    sweeps never interleave their writes. The file and network work runs in a worker
    thread — minting the token is a blocking HTTP call — so a slow swarm does not
    stall the event loop.

    ``announce`` prints the full per-agent summary (the boot pass). Later sweeps print
    only what changed and errors not seen before.
    """
    # Local import: this module is reached from server.py's lifespan, and the
    # composio and auth packages import each other lazily to avoid a load cycle.
    from routers.auth.auth import get_auth_token
    global _LAST_SWEEP_AT

    async with _sweep_lock():
        try:
            agents = gateway_install_agents()
            if not agents:
                detail = "no agent manifest declares an enabled 'mcp' block"
                log.info("composio: %s; nothing to install.", detail)
                return GatewaySweep(skipped="no_agents", detail=detail)

            if not get_auth_token():
                detail = (
                    "the backend holds no XO credential (no XO_API_KEY and no "
                    "consumed session)"
                )
                log.info("composio: %s; skipping MCP install for %s.", detail, agents)
                return GatewaySweep(skipped="no_credential", detail=detail)

            try:
                state.workspace_id()
            except state.WorkspaceIdentityUnavailable as exc:
                detail = f"{exc} — {state.WORKSPACE_ENV} is injected by the Coder pod"
                log.warning(
                    "composio: %s; the session store cannot be stamped, so it could not "
                    "be told apart from one restored out of another workspace.", detail,
                )
                return GatewaySweep(skipped="no_workspace", detail=detail)

            # A store that already names its account lets the identity fetch fall
            # back to it during a swarm outage (state.identity_payload), so read first.
            _ensure_sessions_loaded()

            # Not needed to write the config — the proxy URL carries an opaque token,
            # not an identity. Fetched anyway because it is the gate (an account we
            # cannot name is an install we should not do), and because it warms the
            # cache and records the account for the offline hot path.
            try:
                account_id = await state.aaccount_id()
                state.adopt_account_id(account_id)
            except state.StateUnavailable as exc:
                retryable = not exc.authoritative
                detail = (
                    f"xo-swarm-api could not provide this account's id ({exc})"
                    if retryable else
                    f"xo-swarm-api rejected this backend's XO credential ({exc})"
                )
                log.warning(
                    "composio: %s; skipping MCP install. Agents keep their existing "
                    "config; %s.", detail,
                    "the next sweep retries" if retryable else "fix it and restart",
                )
                return GatewaySweep(
                    skipped="account_unavailable", retryable=retryable, detail=detail,
                )
            except Exception as exc:  # network/JSON faults must not break boot
                detail = f"xo-swarm-api could not provide this account's id ({exc})"
                log.warning(
                    "composio: %s; skipping MCP install. Agents keep their existing "
                    "config; the next sweep retries.", detail,
                )
                return GatewaySweep(
                    skipped="account_unavailable", retryable=True, detail=detail,
                )

            # Now that the account is known, an unstamped store can classify and
            # rewrite itself — which keeps the proxy serving locally through a later outage.
            _ensure_sessions_loaded()

            results = await asyncio.to_thread(_apply_to_agents, agents, announce)
            return GatewaySweep(results=results)
        finally:
            _LAST_SWEEP_AT = time.monotonic()


async def gateway_reconcile_loop() -> None:
    """The lifespan task: sweep now, retry while XO is unreachable, then keep every
    agent's config current on a timer. Cancelled at shutdown.

    Backoff follows ``_RETRY_DELAYS`` and stays at its last value; a gate that cannot
    open without a restart ends the loop after one console line. After a sweep runs,
    the next one is :func:`reconcile_interval` seconds later — ``0`` stops here.
    """
    attempt = 0
    announce = True
    while True:
        try:
            sweep = await install_gateways(announce=announce)
        except Exception as exc:  # a bug must not kill the timer; the next tick retries
            log.exception("composio: gateway sweep failed unexpectedly: %s", exc)
            sweep = GatewaySweep(skipped="account_unavailable", retryable=True, detail=str(exc))
        announce = False

        if sweep.skipped and not sweep.retryable:
            print(f"⚠️ Composio MCP: not installed — {sweep.detail}. Fix it and restart xo-space.")
            return

        if sweep.skipped:
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            if attempt < len(_RETRY_DELAYS):
                print(f"   Composio MCP: {sweep.detail}; retrying in {delay}s")
            attempt += 1
        else:
            attempt = 0
            delay = reconcile_interval()
            if delay <= 0:
                return
        await _sleep(delay)


def _log_sweep_task_outcome(task: "asyncio.Task[GatewaySweep]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("composio: background gateway sweep failed: %s", exc)


def kick_gateway_sweep() -> bool:
    """Start a background sweep from a request handler — fire and forget.

    Called where the Reinstall button used to be pressed: when the Connectors tab
    loads or its Refresh is pressed. Single-flight and rate-limited, so a page that
    reloads in a loop costs nothing; the handler never waits on it. Returns whether a
    sweep was scheduled.
    """
    global _SWEEP_TASK
    if _SWEEP_TASK is not None and not _SWEEP_TASK.done():
        return False
    if _SWEEP_LOCK is not None and _SWEEP_LOCK.locked():
        return False
    if _LAST_SWEEP_AT and time.monotonic() - _LAST_SWEEP_AT < _KICK_MIN_INTERVAL:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _SWEEP_TASK = loop.create_task(install_gateways(announce=False))
    _SWEEP_TASK.add_done_callback(_log_sweep_task_outcome)
    return True
