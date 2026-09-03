from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json

log = logging.getLogger(__name__)

# connectors/composio/ → connectors/ → cowork_agent/ → services/ → repo root.
_PREFS_PATH = Path(__file__).resolve().parents[4] / "data" / "composio_action_prefs.json"


def _store_path() -> Path:
    return _PREFS_PATH


def _require_user_id(user_id: str | None) -> str:
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("action_prefs: a real user_id is required.")
    return uid


def _coerce_toolkit_map(entry: object) -> Dict[str, bool]:
    if not isinstance(entry, dict):
        return {}
    return {
        slug: bool(enabled)
        for slug, enabled in entry.items()
        if isinstance(slug, str)
    }


def load_all() -> Dict[str, Dict[str, Dict[str, bool]]]:
    data = read_json(_store_path())
    if not isinstance(data, dict):
        return {}

    if data.get("version") == 2 or "users" in data:
        users = data.get("users")
        if not isinstance(users, dict):
            return {}
        return {
            str(uid): {
                tk: _coerce_toolkit_map(entry)
                for tk, entry in per_user.items()
                if isinstance(tk, str)
            }
            for uid, per_user in users.items()
            if isinstance(per_user, dict)
        }

    if data:
        log.warning(
            "action_prefs: ignoring pre-v2 install-wide prefs document at %s "
            "(no owning user). Re-toggle actions per user; the file is "
            "rewritten in the per-user shape on the next write.",
            _store_path(),
        )
    return {}


def load_prefs(user_id: str) -> Dict[str, Dict[str, bool]]:
    return load_all().get(_require_user_id(user_id), {})


def get_toolkit_prefs(toolkit_id: str, user_id: str) -> Dict[str, bool]:
    return dict(load_prefs(user_id).get(toolkit_id, {}))


def is_action_enabled(toolkit_id: str, slug: str, user_id: str) -> bool:
    return load_prefs(user_id).get(toolkit_id, {}).get(slug, True) is True


def bulk_set(
    toolkit_id: str, updates: Dict[str, bool], user_id: str,
) -> Dict[str, bool]:
    uid = _require_user_id(user_id)
    path = _store_path()
    with locked(path):
        current = load_all()
        user_map = dict(current.get(uid, {}))
        toolkit_map = dict(user_map.get(toolkit_id, {}))
        for slug, enabled in updates.items():
            if not isinstance(slug, str):
                continue
            if enabled:
                toolkit_map.pop(slug, None)
            else:
                toolkit_map[slug] = False
        if toolkit_map:
            user_map[toolkit_id] = toolkit_map
        else:
            user_map.pop(toolkit_id, None)
        if user_map:
            current[uid] = user_map
        else:
            current.pop(uid, None)
        write_json_atomic(path, {"version": 2, "users": current})
        return toolkit_map
