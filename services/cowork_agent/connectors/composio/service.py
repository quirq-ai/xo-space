from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from services import tenancy
from services.cowork_agent.connectors.composio import credentials, state

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
    # May raise CredentialsUnavailable (a RuntimeError) when the whole bundle is
    # missing; the router maps that to 422 on /connect, which is the documented
    # "no API key => /connect 422s" behaviour, unchanged.
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
# The api key `_client` was built with. Keying the memo on the credential itself is
# what makes a rotation self-invalidating: when credentials.api_key() starts returning
# a new value the old client is dropped and rebuilt, with no cross-module wiring.
#
# Existing Composio sessions are deliberately *not* purged here. A rotation within the
# same Composio project keeps them valid, and if the new key points somewhere else,
# get_session already drops a session id whose `use()` raises and mints a fresh one.
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
    return os.getenv(
        "COMPOSIO_CALLBACK_URL",
        "http://127.0.0.1:5002/api/connectors/composio/callback",
    ).strip()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Composio rejects a max outside this range, so the env value is clamped rather
# than forwarded: an operator typo must not make every session creation 400.
MULTI_ACCOUNT_MIN_MAX = 2
MULTI_ACCOUNT_MAX_MAX = 10
MULTI_ACCOUNT_DEFAULT_MAX = 5

ALIAS_MAX_LENGTH = 128


def multi_account_config() -> Optional[dict[str, Any]]:
    """The session `multi_account` block, or None when the feature is off.

    Off is the Composio default: one account per toolkit per session, the most
    recently connected one. Turning it on lets a principal hold several accounts
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
        # When true and a toolkit has more than one active account, the agent
        # must name one via the tool call's `account` parameter (id or alias)
        # instead of silently getting the most recent.
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
    # Every toolkit in TOOLKITS is OAUTH2-only, so _auth_config_id_for raises
    # for any other scheme before we get here. Re-add an API_KEY branch (via
    # connected_accounts.initiate) if an API_KEY toolkit is ever registered.
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
            # Multi-account fields. `alias` is the human label an agent can pass
            # as a tool call's `account`; `created_at` is what "most recently
            # connected" means when no account is named.
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
    """Every connected account this principal holds for one toolkit.

    Newest first, which is also the order Composio resolves "the default
    account" in when a tool call names none.
    """
    meta = toolkit_meta(toolkit_id)
    rows = [
        row
        for row in list_connections(user_id, toolkit_slugs=[meta.slug])
        # The slug filter is server-side, but an SDK that ignores the parameter
        # would otherwise leak other toolkits' accounts into this list.
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

    # Read the prefs ONCE. This used to be a per-slug lookup inside the loop below, each
    # re-reading the whole store — up to 200 reads per request. Bearable against a local
    # file, unacceptable now the store is remote.
    disabled = composio_action_prefs.disabled_slugs(user_id, toolkit_id)

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


# connectors/composio/ → connectors/ → cowork_agent/ → services/ → repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SESSIONS_PATH = _REPO_ROOT / "data" / "composio_sessions.json"

_SESSION_IDS: dict[str, str] = {}
_PROXY_TOKENS: dict[str, str] = {}
_SESSIONS_LOADED = False


def _load_store() -> tuple[Optional[str], dict[str, str], dict[str, str]]:
    """Read the store, returning ``(owner, sessions, proxy_tokens)``.

    ``owner`` is the principal the document says its rows belong to — v3 records it, v2
    (which predates the swarm owning the tenant key) does not, and anything older is
    discarded. It exists so this pod can tell its own rows from another workspace's
    without composing a principal or asking the network: the MCP hot path runs on every
    agent tool call and must stay offline.
    """
    from services.cowork_agent.visualizer.reader import read_json

    data = read_json(_SESSIONS_PATH)
    if not isinstance(data, dict):
        return None, {}, {}
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 2:
        # Pre-workspace-scoping rows address an account-wide Composio bucket shared by
        # every workspace of the account. Ignored, never upgraded.
        return None, {}, {}

    def _str_map(raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and k and v
        }

    owner = str(data.get("principal") or "").strip() or None
    return owner, _str_map(data.get("sessions")), _str_map(data.get("proxy_tokens"))


def _ensure_sessions_loaded() -> None:
    """Populate the in-memory mirrors from disk, keeping only this pod's own rows.

    A v3 document names its owner, so it classifies itself with no network. A v2 one
    cannot, so it waits until the principal is known and is then upgraded in place —
    deliberately *not* discarded, so a pod that boots during a swarm outage keeps its
    file for a later boot.
    """
    global _SESSIONS_LOADED
    if _SESSIONS_LOADED:
        return
    try:
        owner, sessions, tokens = _load_store()
    except Exception as exc:
        log.warning("composio: could not read session store: %s", exc)
        _SESSIONS_LOADED = True
        return

    if owner:
        state.adopt_principal(owner)
        _SESSION_IDS.update({k: v for k, v in sessions.items() if v == owner})
        _PROXY_TOKENS.update({k: v for k, v in tokens.items() if v == owner})
        _SESSIONS_LOADED = True
        return

    known = state.principal_if_known()
    if not known:
        # Leave _SESSIONS_LOADED False so a later call retries once the principal is in.
        return
    _SESSION_IDS.update({k: v for k, v in sessions.items() if v == known})
    _PROXY_TOKENS.update({k: v for k, v in tokens.items() if v == known})
    _write_store(lambda _s, _t: None, owner=known)   # one-time v2 -> v3 upgrade
    _SESSIONS_LOADED = True


def _write_store(mutate, *, owner: str) -> None:
    """Lock, re-read, mutate, atomically replace — stamped with the owning principal.

    ``owner`` is explicit rather than looked up: every caller already knows whose rows it
    is writing, and deriving it here would put a cache read (or worse a network call) on
    a path that holds a file lock.
    """
    from services.cowork_agent.visualizer.atomic_write import write_json_atomic
    from services.cowork_agent.visualizer.flock import locked

    try:
        with locked(_SESSIONS_PATH):
            _existing, sessions, tokens = _load_store()
            mutate(sessions, tokens)
            write_json_atomic(
                _SESSIONS_PATH,
                {
                    "version": 3,
                    "principal": owner,
                    "sessions": sessions,
                    "proxy_tokens": tokens,
                },
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

    _write_store(_mutate, owner=user_id)
    # Mirror to xo-swarm-api so the id outlives this pod. Best-effort: a swarm that is
    # down, or predates these endpoints, must not break session handling.
    state.put_session_id(session_id)


# Set by the last proxy_token_for_user call: False when the swarm did not record the
# token, so the refresh-gateway response can tell the operator the install is not durable.
_LAST_TOKEN_DURABLE = True


def proxy_token_for_user(user_id: str) -> str:
    """The stable opaque MCP proxy token for a principal, minting one if needed.

    Idempotent on purpose: the boot-time gateway install calls this on every restart, and
    churning the token would strand agents holding the previous URL.

    The plaintext stays on this pod — only its sha256 goes to the swarm, which is all the
    swarm needs to answer "who owns this token?". Minting deliberately does **not** fall
    back to swarm-less operation silently: a token the swarm never recorded stops working
    the moment this pod's local store is lost, and `refresh-gateway` would fail too, so
    the caller is told via `_LAST_TOKEN_DURABLE`.
    """
    global _LAST_TOKEN_DURABLE
    uid = _require_user_id(user_id, "proxy_token_for_user")
    _ensure_sessions_loaded()
    for token, owner in _PROXY_TOKENS.items():
        if owner == uid:
            state.cache_principal(token, uid)
            _LAST_TOKEN_DURABLE = state.put_proxy_token(token)
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

    _write_store(_mutate, owner=uid)
    for tok, owner in _PROXY_TOKENS.items():
        if owner == uid:
            state.cache_principal(tok, uid)
            _LAST_TOKEN_DURABLE = state.put_proxy_token(tok)
            return tok
    _LAST_TOKEN_DURABLE = state.put_proxy_token(token)
    return token


def last_token_was_durable() -> bool:
    """Whether the swarm recorded the most recently minted proxy token."""
    return _LAST_TOKEN_DURABLE


def user_for_proxy_token_local(token: str) -> Optional[str]:
    """Resolve a proxy token from this pod's own store. No network."""
    if not token:
        return None
    _ensure_sessions_loaded()
    user_id = _PROXY_TOKENS.get(token)
    if not user_id:
        # A row written by another process since this one last read. _load_store has
        # already dropped anything that is not ours.
        try:
            owner, _sessions, tokens = _load_store()
        except Exception:
            return None
        if owner:
            _PROXY_TOKENS.update({k: v for k, v in tokens.items() if v == owner})
        user_id = _PROXY_TOKENS.get(token)
    return user_id


async def user_for_proxy_token(token: str) -> Optional[str]:
    """Resolve an MCP proxy token to its owning principal.

    **Local first.** This runs on `initialize`, `tools/list` and every `tools/call`, so
    the steady state must stay a dict lookup with no network. The swarm is consulted only
    on a local miss — which is precisely the case this whole change exists for: the pod's
    store was lost but the agent's config, with its token, was not. The answer is written
    back into the local store, so the pod self-heals and the next call is local again.

    Raises `state.StateUnavailable` when the swarm cannot be reached and nothing is
    cached; the proxy renders that as a retryable 503 rather than a 401, because a 401
    would send the user to `refresh-gateway`, which during an outage also fails.
    """
    local = user_for_proxy_token_local(token)
    if local:
        state.cache_principal(token, local)
        return local

    if state.source() != "swarm":
        return None

    remote = await state.resolve_proxy_token(token)
    if remote:
        # Self-heal: adopt the row so the next request never leaves the pod.
        _PROXY_TOKENS[token] = remote

        def _mutate(_sessions: dict[str, str], tokens: dict[str, str]) -> None:
            tokens[token] = remote

        _write_store(_mutate, owner=remote)
        log.info("composio: recovered proxy token ownership from xo-swarm-api.")
    return remote


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
    from services.cowork_agent.connectors.composio import action_prefs as composio_action_prefs

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


def pinned_connected_accounts(user_id: str) -> dict[str, list[str]]:
    """Which connected accounts this principal's session may use, per toolkit.

    With multi-account mode off a session may only carry one account per
    toolkit, so a principal holding two active Gmail accounts gets the most
    recently connected one — the same account Composio would pick itself.
    Pinning both would be rejected at session creation.
    """
    pinned: dict[str, list[str]] = {}
    try:
        rows = list_connections(user_id, statuses=["ACTIVE"])
    except Exception as exc:
        log.warning("composio: list_connections failed while building pin map for user=%s: %s", user_id, exc)
        return pinned
    multi = multi_account_config()
    per_toolkit_cap = (
        int(multi["max_accounts_per_toolkit"]) if multi else 1
    )
    for row in newest_first(rows):
        if (row.get("status") or "").upper() != "ACTIVE":
            continue
        if row.get("is_disabled"):
            continue
        slug = (row.get("toolkit") or "").lower()
        cid = row.get("connected_account_id")
        if not slug or not cid:
            continue
        bucket = pinned.setdefault(slug, [])
        if len(bucket) >= per_toolkit_cap:
            log.info(
                "composio: user=%s has more than %d active %s account(s); "
                "pinning the newest and skipping %s.",
                user_id, per_toolkit_cap, slug.upper(), cid,
            )
            continue
        bucket.append(cid)
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
        # multi_account is passed even when it is None: that is how a session
        # minted while the flag was on converges after the operator turns it
        # off. If the API rejects the shape the except below re-mints, which
        # reaches the same state by the other road.
        session.update(
            connected_accounts=pinned_connected_accounts(user_id),
            tools=_disabled_tools_config(user_id),
            multi_account=multi_account_config(),
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
    multi = multi_account_config()
    if multi:
        create_kwargs["multi_account"] = multi
    pinned = pinned_connected_accounts(user_id)
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
    from services.cowork_agent.connectors.composio import mcp

    target = mcp.load_target(agent)
    if target is None:
        return {
            "ok": False,
            "error": (
                f"Agent '{agent}' does not support gateway MCP install "
                f"(no 'mcp' block in config/agents/{agent}/manifest.json)."
            ),
        }
    try:
        proxy_url = _composio_proxy_url(user_id)
    except Exception as exc:
        log.warning("composio: gateway install could not build proxy URL: %s", exc)
        return {"ok": False, "error": str(exc)}
    return mcp.apply(target, proxy_url)


def gateway_install_agents() -> list[str]:
    from services.cowork_agent.connectors.composio import mcp

    return mcp.agents_with_targets()


async def install_gateways_at_startup() -> dict[str, dict]:
    """Point every agent whose manifest declares an ``mcp`` block at this
    workspace's Composio proxy. Called from ``server.py``'s lifespan.

    Before this existed the only install path was a manual
    ``POST /api/connectors/composio/refresh-gateway``, so a fresh workspace —
    or a restart after the proxy token changed — left agents with no Composio
    tools and no signal beyond a 401 telling the user to run that endpoint.

    Identity at boot: ``install_into_gateway`` wants this workspace's principal, and
    there is no request to carry one. The backend holds its own XO credential, so it asks
    xo-swarm-api directly — the same fetch every later request reads from cache, so this
    also warms it.

    Fail closed and quietly: no credential, no workspace identity, or an unreachable
    swarm means nothing is installed and the agents keep whatever config they already
    have. Nothing here is fatal to boot.

    Returns a per-agent result map (also useful in tests); callers at boot ignore
    it and rely on the printed summary.
    """
    # Local imports: this module is reached from server.py's lifespan, and the
    # composio and auth packages import each other lazily to avoid a load cycle.
    from services.xo_credential import get_auth_token

    agents = gateway_install_agents()
    if not agents:
        log.info("composio: no agent manifest declares an 'mcp' block; nothing to install.")
        return {}

    token = get_auth_token()
    if not token:
        log.info(
            "composio: backend holds no XO credential (no XO_API_KEY and no "
            "consumed session); skipping MCP install for %s.", agents,
        )
        return {}

    try:
        tenancy.workspace_id()
    except tenancy.WorkspaceIdentityUnavailable as exc:
        log.warning(
            "composio: %s — refusing to install an MCP config bound to an unscoped "
            "Composio bucket, which every workspace of this account would share. "
            "%s is injected by the Coder pod.",
            exc, tenancy.WORKSPACE_ENV,
        )
        return {}

    try:
        principal = await state.aprincipal()
    except Exception as exc:  # network/JSON faults must not break boot
        log.warning(
            "composio: no principal for this workspace (%s); skipping MCP install. "
            "Agents keep their existing config; it resolves on the next boot or a "
            "manual refresh-gateway.", exc,
        )
        return {}

    # Now that the principal is known, a pre-ownership store can classify and upgrade
    # itself — which is what keeps the proxy serving locally through a later outage.
    _ensure_sessions_loaded()

    results: dict[str, dict] = {}
    for agent in agents:
        try:
            result = install_into_gateway(principal, agent)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        results[agent] = result

        # print, not log.info: this runs from the lifespan, where the boot summary
        # is the only place anyone looks, and `services.*` loggers are not wired to
        # a handler. Same convention as skill_installer.
        if result.get("ok"):
            if result.get("changed") is False:
                print(f"   Composio MCP: {agent} already current ({result.get('config_path')})")
            else:
                print(f"✅ Composio MCP installed for {agent}: {result.get('config_path')}")
        else:
            # Expected when an agent isn't provisioned on this host — its config
            # file simply doesn't exist. Not an error worth shouting about.
            print(f"⚠️ Composio MCP skipped for {agent}: {result.get('error')}")

    return results
