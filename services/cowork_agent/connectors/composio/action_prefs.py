"""Which individual Composio actions this workspace has switched off.

One document per pod, and a pod is one workspace — so there is no user or workspace level
in the shape. The v2 document had a ``users`` map because prefs were keyed by the
composed ``<account>__ws__<workspace>`` tenant key; that key is retired (see
:mod:`.state`) and the map only ever held one row, so v3 drops it.

Only *disabled* slugs are ever stored, which is what makes an action added to a toolkit
later default to on.

Feeds the ``tools`` key of the Composio session (``{toolkit: {"disable": [...]}}``).
Toolkit-level on/off and connected-account pinning are a different question, answered by
:mod:`.workspace_scope`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from services.cowork_agent.connectors.composio import paths
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json

log = logging.getLogger(__name__)

# Outside the checkout, alongside the sessions store — see paths.py.
_PREFS_PATH = paths.store_dir() / "action_prefs.json"
_LEGACY_PREFS_PATHS = (paths.legacy_checkout_path("composio_action_prefs.json"),)

STORE_VERSION = 3


def _store_path() -> Path:
    return _PREFS_PATH


def _migrate() -> None:
    """Move a prefs document left in the checkout. No mode: prefs are not a secret.

    Routed through ``_store_path()`` rather than ``_PREFS_PATH`` because that function is
    the seam tests redirect, so migration follows the redirect with them.
    """
    paths.migrate_legacy(_store_path(), _LEGACY_PREFS_PATHS)


def _coerce_toolkit_map(entry: object) -> Dict[str, bool]:
    if not isinstance(entry, dict):
        return {}
    return {
        slug: bool(enabled)
        for slug, enabled in entry.items()
        if isinstance(slug, str)
    }


def _coerce_toolkits(raw: object) -> Dict[str, Dict[str, bool]]:
    if not isinstance(raw, dict):
        return {}
    return {
        tk: _coerce_toolkit_map(entry)
        for tk, entry in raw.items()
        if isinstance(tk, str)
    }


def load_prefs() -> Dict[str, Dict[str, bool]]:
    """Every disabled slug on this pod, keyed by toolkit.

    A v2 document is read by collapsing its ``users`` map. That map was keyed by the
    retired tenant key, and a pod only ever wrote one row of it, so taking the single row
    is lossless. A document with several rows means a store restored from elsewhere; the
    rows are merged rather than guessed between, which can only ever *disable* more than
    intended — the safe direction.
    """
    _migrate()
    data = read_json(_store_path())
    if not isinstance(data, dict):
        return {}

    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0

    if version >= STORE_VERSION:
        return _coerce_toolkits(data.get("toolkits"))

    if version == 2 or "users" in data:
        users = data.get("users")
        if not isinstance(users, dict):
            return {}
        merged: Dict[str, Dict[str, bool]] = {}
        for per_user in users.values():
            for tk, entry in _coerce_toolkits(per_user).items():
                merged.setdefault(tk, {}).update(entry)
        return merged

    if data:
        log.warning(
            "action_prefs: ignoring pre-v2 prefs document at %s. Re-toggle actions; "
            "the file is rewritten in the current shape on the next write.",
            _store_path(),
        )
    return {}


def get_toolkit_prefs(toolkit_id: str) -> Dict[str, bool]:
    return dict(load_prefs().get(toolkit_id, {}))


def disabled_slugs(toolkit_id: str) -> frozenset[str]:
    """The disabled slugs for one toolkit, in a single read.

    Only *disabled* slugs are ever stored, so anything absent from this set is enabled.

    Deliberately set-shaped rather than a per-slug predicate: the one caller classifies a
    whole tool listing at once, and a per-slug helper meant re-reading the entire store
    for each of up to 200 tools.
    """
    return frozenset(load_prefs().get(toolkit_id, {}))


def bulk_set(toolkit_id: str, updates: Dict[str, bool]) -> Dict[str, bool]:
    # Before the lock, for the same reason as the sessions store: the lock sentinel is
    # keyed on the absolute path.
    _migrate()
    path = _store_path()
    with locked(path):
        current = load_prefs()
        toolkit_map = dict(current.get(toolkit_id, {}))
        for slug, enabled in updates.items():
            if not isinstance(slug, str):
                continue
            if enabled:
                toolkit_map.pop(slug, None)
            else:
                toolkit_map[slug] = False
        if toolkit_map:
            current[toolkit_id] = toolkit_map
        else:
            current.pop(toolkit_id, None)
        write_json_atomic(path, {"version": STORE_VERSION, "toolkits": current})
    return toolkit_map
