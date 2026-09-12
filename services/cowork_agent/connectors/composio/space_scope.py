"""What this workspace may reach — the per-workspace half of connector isolation.

Composio connections are **account-wide**, so "which workspace is this?" does not answer
"what can it touch?". This store does.

Two decisions per toolkit, both scoped to this workspace:

* ``enabled`` — whether the toolkit reaches the agent at all. Becomes the session's
  ``toolkits: {"enable": [...]}`` allowlist, which Composio checks *before* it looks up a
  connection.
* ``connected_account_ids`` — which of the account's connections back it. Becomes the
  session's ``connected_accounts`` pin, which Composio treats as an exact override with
  no fallback.

**Fail closed.** A toolkit with no entry here is off. Without a pin Composio resolves the
*most recently connected* active account at execution time, so connecting a second Gmail
in another workspace would silently repoint this one. The exception is the OAuth callback:
it runs in this pod, so the workspace that performed the connect enables and pins it
immediately; every other workspace starts empty and opts in.

**Pod-local, and therefore not durable.** A rebuilt workspace comes back with nothing
enabled and the user re-picks.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from services.cowork_agent.connectors.composio import paths, state
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json

log = logging.getLogger(__name__)

_SCOPE_PATH = paths.store_dir() / "space_scope.json"
# The store's name before the space_id rename. Moved on first access, so a toolkit a user
# had enabled does not silently read as off.
_LEGACY_SCOPE_PATHS = (paths.store_dir() / "workspace_scope.json",)

STORE_VERSION = 1


def _store_path() -> Path:
    return _SCOPE_PATH


def _migrate() -> None:
    """Move a ``workspace_scope.json`` left by an older build to ``space_scope.json``.

    Routed through ``_store_path()`` rather than ``_SCOPE_PATH`` because that function is
    the seam tests redirect, so migration follows the redirect with them. No mode: the
    scope is not a secret.
    """
    paths.migrate_legacy(_store_path(), _LEGACY_SCOPE_PATHS)


def _coerce_entry(raw: object) -> Dict[str, object]:
    if not isinstance(raw, dict):
        return {"enabled": False, "connected_account_ids": []}
    ids = raw.get("connected_account_ids")
    return {
        "enabled": bool(raw.get("enabled")),
        "connected_account_ids": [
            str(cid) for cid in ids if isinstance(cid, str) and cid
        ] if isinstance(ids, list) else [],
    }


def load() -> Dict[str, Dict[str, object]]:
    """Every toolkit this workspace has an opinion about. Absent means off."""
    _migrate()
    data = read_json(_store_path())
    if not isinstance(data, dict):
        return {}
    toolkits = data.get("toolkits")
    if not isinstance(toolkits, dict):
        return {}
    return {
        tk: _coerce_entry(entry)
        for tk, entry in toolkits.items()
        if isinstance(tk, str)
    }


def is_enabled(toolkit_id: str) -> bool:
    return bool(load().get(toolkit_id, {}).get("enabled"))


def enabled_toolkits() -> List[str]:
    """Sorted, so a session's config is stable across restarts and easy to diff."""
    return sorted(tk for tk, entry in load().items() if entry.get("enabled"))


def pins() -> Dict[str, List[str]]:
    """The ``connected_accounts`` map for the session.

    Only enabled toolkits with at least one pinned id appear. An enabled toolkit with no
    pin is deliberately omitted rather than sent as an empty list: Composio reads an empty
    pin as "no account is permitted", which would present as a confusing execution-time
    failure instead of simply falling back to the account's own default.
    """
    out: Dict[str, List[str]] = {}
    for tk, entry in load().items():
        if not entry.get("enabled"):
            continue
        ids = list(entry.get("connected_account_ids") or [])
        if ids:
            out[tk] = ids
    return out


def _write(mutate) -> Dict[str, Dict[str, object]]:
    """Lock, re-read, mutate, atomically replace.

    Stamps the ``space_id`` this scope was written under (:func:`state.space_stamp`), as
    ``sessions.json`` does. Informational only: :func:`load` never compares it.
    """
    path = _store_path()
    # Before the lock: the sentinel is keyed on the store's absolute path.
    _migrate()
    with locked(path):
        current = load()
        mutate(current)
        write_json_atomic(path, {
            "version": STORE_VERSION,
            "space_id": state.space_stamp(read_json(path)),
            "toolkits": current,
        })
    return current


def set_toolkit(
    toolkit_id: str,
    *,
    enabled: Optional[bool] = None,
    connected_account_ids: Optional[Iterable[str]] = None,
    max_accounts: int = 1,
) -> Dict[str, object]:
    """Set this workspace's opinion about one toolkit. Partial: None leaves a field alone.

    ``max_accounts`` mirrors the session's multi-account cap. Composio rejects a session
    that pins more accounts than the cap allows — and a non-multi-account session caps at
    one — so the excess is dropped here, where it can be reported, rather than at session
    creation where it would take every other toolkit down with it.
    """
    result: Dict[str, object] = {}

    def _mutate(current: Dict[str, Dict[str, object]]) -> None:
        entry = dict(current.get(toolkit_id) or {"enabled": False,
                                                 "connected_account_ids": []})
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        if connected_account_ids is not None:
            ids: List[str] = []
            for cid in connected_account_ids:
                cid = (cid or "").strip()
                if cid and cid not in ids:
                    ids.append(cid)
            if len(ids) > max_accounts:
                log.info(
                    "composio_scope: %s pinned %d accounts but the session allows %d; "
                    "keeping the first %d.",
                    toolkit_id, len(ids), max_accounts, max_accounts,
                )
                ids = ids[:max_accounts]
            entry["connected_account_ids"] = ids
        current[toolkit_id] = entry
        result.update(entry)

    _write(_mutate)
    return result


def unlink_account(toolkit_id: str, connected_account_id: str) -> Dict[str, object]:
    """Drop one connected account from this workspace, leaving it connected elsewhere.

    The account itself is untouched in Composio — this is "not here", not "delete". A
    toolkit left with no pins is switched off, so it stops appearing to the agent rather
    than silently reverting to Composio's most-recently-connected default.
    """
    result: Dict[str, object] = {}

    def _mutate(current: Dict[str, Dict[str, object]]) -> None:
        entry = dict(current.get(toolkit_id) or {})
        ids = [
            cid for cid in (entry.get("connected_account_ids") or [])
            if cid != connected_account_id
        ]
        entry["connected_account_ids"] = ids
        if not ids:
            entry["enabled"] = False
        current[toolkit_id] = entry
        result.update(entry)

    _write(_mutate)
    return result


def prune_to(live_account_ids: Iterable[str]) -> bool:
    """Drop pins whose connected account no longer exists. Returns True if anything went.

    Load-bearing, and it runs before every session create and update. Composio requires a
    pinned account to exist and be enabled; **one stale id fails the whole session**, not
    just its toolkit. Since a connection deleted from another workspace cannot reach into
    this pod's store, this is what makes that deletion self-heal here.
    """
    live = {str(cid) for cid in live_account_ids if cid}
    changed = False

    def _mutate(current: Dict[str, Dict[str, object]]) -> None:
        nonlocal changed
        for toolkit_id, entry in list(current.items()):
            ids = list(entry.get("connected_account_ids") or [])
            kept = [cid for cid in ids if cid in live]
            if kept == ids:
                continue
            changed = True
            log.info(
                "composio_scope: dropping %d stale pin(s) for %s; the connection was "
                "deleted elsewhere.", len(ids) - len(kept), toolkit_id,
            )
            entry["connected_account_ids"] = kept
            if not kept:
                entry["enabled"] = False
            current[toolkit_id] = entry

    # Read first so the common case — nothing stale — takes no lock and no write.
    snapshot = load()
    if not any(
        cid not in live
        for entry in snapshot.values()
        for cid in (entry.get("connected_account_ids") or [])
    ):
        return False

    _write(_mutate)
    return changed


def adopt_connection(toolkit_id: str, connected_account_id: str, *,
                     max_accounts: int = 1) -> Dict[str, object]:
    """Enable a toolkit here and pin the account that was just connected.

    Called from the OAuth callback, which runs in this pod: the workspace that performed
    the connect gets it without a second step, while every other workspace of the account
    still has to opt in.
    """
    existing = list(
        load().get(toolkit_id, {}).get("connected_account_ids") or []
    )
    if connected_account_id not in existing:
        # Newest first: with a cap of one this replaces the previous pin.
        existing.insert(0, connected_account_id)
    return set_toolkit(
        toolkit_id,
        enabled=True,
        connected_account_ids=existing,
        max_accounts=max_accounts,
    )
