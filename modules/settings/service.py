"""The settings module's facade: what the routes, the CLI commands and other
modules call. Knows nothing about HTTP: a failure is a
:class:`services.errors.ServiceError` carrying its status, and reaches the
wire through the app's service error handler with the body the routers it
replaced always answered.

Four subjects:

* **Runtime settings and roots** (``runtime_config``): the effective
  settings (what this process runs with), the saved ones (``runtime.env``),
  the roots (``roots.env``), the restart state and the Setup status payload
  (:func:`runtime_status`). Every function delegates to ``runtime_config``
  at call time, so a patch through ``services.cowork_agent.runtime_config``
  (the same module object) is seen here too. A refused save is a 400 with
  the bare message, as ``PUT /api/runtime-config`` has always answered.
* **Secrets**: :class:`SecretsScope`, the one handle over the write-only
  store (``registry/agent_env.py``; moved here from
  ``services.cowork_agent.scopes``, which still exports it), and the curated
  reads and writes behind ``/api/secrets``: a masked listing, the reveal of
  one key, a bulk replace, a single upsert, a delete. The rules: a key is
  ``^[A-Z_][A-Z0-9_]*$``, a value carries no newline or null byte, and the
  runtime controls (:data:`HIDDEN_KEYS`, the ``runtime.env`` keys) are
  hidden from the store: they have their own typed API above, so an
  advanced variable can never silently override the setup panel on the next
  restart. ``routers/cowork_agent/bff/filters.py`` states the same rules for
  the other BFF routers.
* **Onboarding** (``xo_cowork_state``): whether the first-run flow was
  completed, on ``settings/onboarding.json``.
* **Setup status** (``setup_status``): the identity checks behind
  ``GET /space/setup/status`` (the route stays in ``routers/space.py``).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from services.cowork_agent.registry import agent_env
from services.errors import NotFound, ServiceError

from . import runtime_config, xo_cowork_state
from . import setup_status as _setup_status
from .runtime_config import (  # noqa: F401  (re-exported: the keys and the install one-liner)
    INSTALL_COMMAND,
    ROOT_CONFIG_KEYS,
    RUNTIME_CONFIG_KEYS,
)

__all__ = [
    "INSTALL_COMMAND", "ROOT_CONFIG_KEYS", "RUNTIME_CONFIG_KEYS", "HIDDEN_KEYS", "MASK",
    "SecretsScope", "applied_roots", "complete_onboarding", "configured_settings",
    "delete_secret", "effective_settings", "env_entries", "env_keys", "is_valid_key",
    "is_valid_value", "list_secrets", "native_restart_pid", "onboarding_state",
    "onboarding_status", "overview", "patch_secret", "preview_value", "put_secrets",
    "restart_mode", "restart_reasons", "restart_required", "reveal_secret", "root_settings",
    "runtime_sources", "runtime_status", "save_env_entries", "save_root_settings",
    "save_settings", "saved_settings", "secrets_scope", "setup_status", "update_onboarding",
    "validate_root_settings", "validate_settings",
]

logger = logging.getLogger(__name__)


# ── Runtime settings and roots ───────────────────────────────────────────────


def _refused(exc: Exception) -> ServiceError:
    """A save the validator or the disk refused. The runtime routes have
    always answered ``{"detail": "<why>"}`` with 400 (a bare message, no
    code); a :class:`ServiceError` without a code keeps that wire shape."""
    return ServiceError(None, str(exc), 400)


def effective_settings() -> dict[str, Any]:
    """What this process runs with: the environment it started from."""
    return runtime_config.effective_settings()


def saved_settings() -> Optional[dict[str, Any]]:
    """What ``runtime.env`` says, or ``None`` when nothing was saved."""
    return runtime_config.saved_settings()


def configured_settings() -> dict[str, Any]:
    """The saved settings, else the effective ones."""
    return runtime_config.configured_settings()


def validate_settings(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return runtime_config.validate_settings(payload)
    except ValueError as exc:
        raise _refused(exc) from exc


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and write ``runtime.env`` (owner-only); the clean settings."""
    try:
        return runtime_config.save_settings(payload)
    except (OSError, ValueError) as exc:
        raise _refused(exc) from exc


def root_settings() -> dict[str, Any]:
    """Applied and configured roots, whether a restart is due, the apply command."""
    return runtime_config.root_settings()


def applied_roots() -> dict[str, str]:
    return runtime_config.applied_roots()


def validate_root_settings(payload: dict[str, Any]) -> dict[str, str]:
    try:
        return runtime_config.validate_root_settings(payload)
    except ValueError as exc:
        raise _refused(exc) from exc


def save_root_settings(payload: dict[str, Any]) -> dict[str, str]:
    """Validate and write ``roots.env`` (owner-only); the clean roots."""
    try:
        return runtime_config.save_root_settings(payload)
    except (OSError, ValueError) as exc:
        raise _refused(exc) from exc


def restart_mode() -> str:
    """``managed``, ``native`` or ``foreground``: how this server can be restarted."""
    return runtime_config.restart_mode()


def native_restart_pid() -> Optional[int]:
    return runtime_config.native_restart_pid()


def restart_required() -> bool:
    return runtime_config.restart_required()


def restart_reasons() -> list[str]:
    """Which of ``runtime``, ``secrets`` and ``roots`` changed since startup."""
    return runtime_config.restart_reasons()


def runtime_status() -> dict[str, Any]:
    """The ``GET /api/runtime-config`` payload: ``configured`` (saved or
    effective), ``applied`` (effective), ``restart_required``,
    ``restart_reasons``, ``restart_mode``, ``roots``, ``agents``, ``paths``,
    ``network``, ``usage_reporting``."""
    return runtime_config.runtime_status()


def runtime_sources() -> list[dict[str, Any]]:
    return runtime_config.runtime_sources()


def overview() -> dict[str, Any]:
    """The effective settings beside the saved ones and the restart state
    (the CLI's ``settings`` command)."""
    return {
        "effective": effective_settings(),
        "saved": saved_settings(),
        "restart_required": restart_required(),
        "restart_reasons": restart_reasons(),
        "restart_mode": restart_mode(),
    }


# ── Secrets ──────────────────────────────────────────────────────────────────


# POSIX shell convention: a leading uppercase letter or underscore, then
# uppercase letters, digits and underscores.
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Characters that would break .env round-tripping if present in a value.
_FORBIDDEN_VALUE_CHARS = ("\n", "\r", "\x00")
#: The runtime controls never pass through the generic store: they have
#: their own typed, validated API (``runtime.env``).
HIDDEN_KEYS: frozenset[str] = RUNTIME_CONFIG_KEYS
#: The fixed mask a listing shows for a set value: neither the value, its
#: length nor a fragment of it crosses the wire.
MASK = "••••••"


def is_valid_key(key: str) -> bool:
    return bool(_ENV_KEY_RE.match(key))


def is_valid_value(value: str) -> bool:
    return not any(ch in value for ch in _FORBIDDEN_VALUE_CHARS)


def preview_value(value: str) -> Optional[str]:
    """The fixed mask, or ``None`` for an empty value."""
    return MASK if value else None


class SecretsScope:
    """Handle exposing the secret store via agent_env helpers only.

    A route only ever sees this object, never a raw Path, so it cannot
    accidentally read or write the underlying .env file directly. Future
    migrations (e.g. moving secrets out of .env into a real secret store)
    only need to swap this class's implementation.
    """

    def load(self) -> list[dict]:
        """Return current entries as [{key, value}, ...]."""
        return agent_env.load_env_entries()

    def save(self, items: list[dict]) -> None:
        """Bulk-replace the entire store."""
        agent_env.save_env_entries(items)

    def upsert(self, key: str, value: str) -> None:
        """Insert or update a single key (preserves comments/ordering)."""
        agent_env.upsert_env_entry(key, value)

    def delete(self, key: str) -> bool:
        """Remove a single key. Returns True if it was present."""
        entries = agent_env.load_env_entries()
        before = len(entries)
        kept = [e for e in entries if e.get("key") != key]
        if len(kept) == before:
            return False
        agent_env.save_env_entries(kept)
        return True


def secrets_scope() -> SecretsScope:
    return SecretsScope()


def _unavailable(exc: Exception) -> ServiceError:
    return ServiceError("scope_unavailable", "Secrets store is not accessible.", 500, log=str(exc))


def _key_not_found() -> NotFound:
    return NotFound("key_not_found", "Secret not found.")


def _require_valid_key(key: str) -> None:
    if not is_valid_key(key):
        raise ServiceError("invalid_key", "Key must match ^[A-Z_][A-Z0-9_]*$.")


def _require_valid_value(value: str) -> None:
    if not is_valid_value(value):
        raise ServiceError("invalid_value", "Value must not contain newline or null bytes.")


def _require_writable_key(key: str) -> None:
    _require_valid_key(key)
    if key in HIDDEN_KEYS:
        raise ServiceError("invalid_key", "Key is reserved and cannot be modified.")


def _shape(entries: list[dict]) -> list[dict]:
    """Filter on the way out: drop hidden keys, drop malformed keys (logged),
    and reduce every entry to ``{key, is_set, preview}``."""
    out: list[dict] = []
    for entry in entries:
        key = (entry.get("key") or "").strip()
        if not key:
            continue
        if not is_valid_key(key):
            logger.warning("Skipping malformed key in secrets store: %r", key)
            continue
        if key in HIDDEN_KEYS:
            continue
        value = entry.get("value") or ""
        if not is_valid_value(value):
            # Lenient on read: surface as is_set false rather than refusing the listing.
            out.append({"key": key, "is_set": False, "preview": None})
            continue
        is_set = bool(value.strip())
        out.append({"key": key, "is_set": is_set, "preview": preview_value(value) if is_set else None})
    out.sort(key=lambda row: row["key"])
    return out


def list_secrets() -> dict[str, Any]:
    """``{items: [{key, is_set, preview}], total}``; never a value."""
    try:
        entries = secrets_scope().load()
    except OSError as exc:
        raise _unavailable(exc) from exc
    items = _shape(entries)
    return {"items": items, "total": len(items)}


def reveal_secret(key: str) -> dict[str, str]:
    """``{key, value}`` for one key; a hidden or absent key is 404."""
    _require_valid_key(key)
    if key in HIDDEN_KEYS:
        raise _key_not_found()
    try:
        entries = secrets_scope().load()
    except OSError as exc:
        raise _unavailable(exc) from exc
    entry = next((e for e in entries if e.get("key") == key), None)
    if entry is None:
        raise _key_not_found()
    return {"key": key, "value": entry.get("value") or ""}


def put_secrets(items: list[dict]) -> dict[str, Any]:
    """Replace the whole store with ``items`` (``[{key, value}]``); the listing after."""
    seen: set[str] = set()
    cleaned: list[dict] = []
    for item in items:
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") if item.get("value") is not None else "")
        _require_writable_key(key)
        _require_valid_value(value)
        if key in seen:
            raise ServiceError("duplicate_key", f"Duplicate key in items: {key}")
        seen.add(key)
        cleaned.append({"key": key, "value": value})
    scope = secrets_scope()
    try:
        scope.save(cleaned)
        entries = scope.load()
    except OSError as exc:
        raise _unavailable(exc) from exc
    shaped = _shape(entries)
    return {"items": shaped, "total": len(shaped)}


def patch_secret(key: str, value: str) -> dict[str, Any]:
    """Set one key (a line-level edit that keeps the rest of the file); its summary."""
    _require_writable_key(key)
    _require_valid_value(value)
    try:
        secrets_scope().upsert(key, value)
    except OSError as exc:
        raise _unavailable(exc) from exc
    is_set = bool(value.strip())
    return {"key": key, "is_set": is_set, "preview": preview_value(value) if is_set else None}


def delete_secret(key: str) -> dict[str, Any]:
    """Remove one key; idempotent. ``{key, deleted}``; a hidden key is never deleted."""
    _require_valid_key(key)
    if key in HIDDEN_KEYS:
        return {"key": key, "deleted": False}
    try:
        deleted = secrets_scope().delete(key)
    except OSError as exc:
        raise _unavailable(exc) from exc
    return {"key": key, "deleted": deleted}


def _store_failed(exc: Exception) -> ServiceError:
    """The legacy whole-file routes answered ``{"detail": "<why>"}`` with 500."""
    return ServiceError(None, str(exc), 500)


def env_entries() -> list[dict]:
    """The whole store as ``[{key, value}]`` (the legacy Env Vars view)."""
    try:
        return agent_env.load_env_entries()
    except Exception as exc:  # noqa: BLE001 - the legacy route reported any failure as a 500 message
        raise _store_failed(exc) from exc


def env_keys() -> list[str]:
    """Only the keys with a non-empty value: no secret material."""
    return [entry["key"] for entry in env_entries() if (entry.get("value") or "").strip()]


def save_env_entries(entries: list[dict]) -> None:
    """Overwrite the whole store with ``entries`` (comments and blank lines are not kept)."""
    try:
        agent_env.save_env_entries(entries)
    except Exception as exc:  # noqa: BLE001 - as above
        raise _store_failed(exc) from exc


# ── Onboarding ───────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def onboarding_state() -> dict[str, Any]:
    """The whole onboarding document."""
    return xo_cowork_state.get_state()


def update_onboarding(patch: dict[str, Any]) -> dict[str, Any]:
    return xo_cowork_state.update_state(patch)


def onboarding_status() -> dict[str, Any]:
    """``{completed, completed_at}``, the ``GET /api/onboarding`` payload."""
    state = onboarding_state()
    return {
        "completed": bool(state.get("onboarding_completed")),
        "completed_at": state.get("onboarding_completed_at"),
    }


def complete_onboarding() -> dict[str, Any]:
    """Mark the first-run flow done, now; the status after."""
    xo_cowork_state.update_state({
        "onboarding_completed": True,
        "onboarding_completed_at": _now(),
    })
    return onboarding_status()


# ── Setup status ─────────────────────────────────────────────────────────────


async def setup_status() -> dict[str, Any]:
    """Workspace metadata and verified account status, without credential values."""
    return await _setup_status.snapshot()
