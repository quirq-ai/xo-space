"""The user's own Composio API key, stored locally on this pod.

Composio runs only when a key is configured here; there is no swarm fallback. The
key comes from ``COMPOSIO_BYO_API_KEY`` or, failing that, an owner-only file next
to the other Composio stores. It is never sent to XO and never returned in any
response. The ``user_id`` addressing the key's Composio project is ``XO_SPACE_ID``
(else a fixed default), so a store restored elsewhere reaches the same project.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from modules.connectors.composio import paths
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json

log = logging.getLogger(__name__)

ENV_VAR = "COMPOSIO_BYO_API_KEY"
DEFAULT_USER_ID = "xo-space-default"
STORE_VERSION = 1

_KEY_PATH = paths.store_dir() / "api_key.json"


class ComposioKeyRequired(RuntimeError):
    """No Composio API key is configured, so connectors are inactive."""

    authoritative = True


def fingerprint(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:16]


def _file_doc() -> dict:
    data = read_json(_KEY_PATH)
    return data if isinstance(data, dict) else {}


def _env_key() -> str:
    return (os.getenv(ENV_VAR) or "").strip()


def api_key() -> str:
    env = _env_key()
    if env:
        return env
    return str(_file_doc().get("api_key") or "").strip()


def source() -> Optional[str]:
    if _env_key():
        return "env"
    if str(_file_doc().get("api_key") or "").strip():
        return "file"
    return None


def configured() -> bool:
    return bool(api_key())


def require() -> str:
    key = api_key()
    if not key:
        raise ComposioKeyRequired("Add your Composio API key to activate connectors.")
    return key


def user_id() -> str:
    return (os.getenv("XO_SPACE_ID") or "").strip() or DEFAULT_USER_ID


def _persist(doc: dict) -> None:
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    write_json_atomic(_KEY_PATH, doc)
    try:
        _KEY_PATH.chmod(0o600)
    except OSError:
        pass


def save(key: str) -> None:
    key = (key or "").strip()
    with locked(_KEY_PATH):
        _persist({
            "version": STORE_VERSION,
            "api_key": key,
            "key_fingerprint": fingerprint(key),
            "auth_configs": {},
        })


def clear() -> None:
    try:
        _KEY_PATH.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("composio_byo: could not remove key file: %s", exc)


def load_auth_configs() -> dict[str, str]:
    """Cached auth-config ids, but only while they belong to the active key."""
    doc = _file_doc()
    if doc.get("key_fingerprint") != fingerprint(api_key()):
        return {}
    cache = doc.get("auth_configs")
    return {str(k): str(v) for k, v in cache.items()} if isinstance(cache, dict) else {}


def save_auth_config(toolkit_id: str, ac_id: str) -> None:
    with locked(_KEY_PATH):
        doc = _file_doc()
        fp = fingerprint(api_key())
        if doc.get("key_fingerprint") != fp:
            # Env-only key, or a changed key: start a cache scoped to the live key.
            doc = {"version": STORE_VERSION, "api_key": doc.get("api_key", ""),
                   "key_fingerprint": fp, "auth_configs": {}}
        configs = doc.get("auth_configs")
        if not isinstance(configs, dict):
            configs = {}
        configs[toolkit_id] = ac_id
        doc["auth_configs"] = configs
        _persist(doc)
