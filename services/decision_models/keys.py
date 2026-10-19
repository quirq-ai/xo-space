"""Decision-model API keys: ``<state>/secrets/router.env`` (design §0.81,
ch 17 §2.6). Beside the secrets store, persisted outside the install, mode
0600, written atomically under a lock; never ``secrets.env``, never
``os.environ``, never ``/api/secrets/env`` or ``/providers/status``. The env
whitelist reads this file's key names to keep them from every agent
(``adapters/env_whitelist.py``), so it stays ``KEY=VALUE`` lines.

A provider reads its key here per call, so a key connected or removed takes
effect from the next routing decision. Nothing here returns a key to a
caller other than the provider that sends it.
"""

from __future__ import annotations

from pathlib import Path

from services.cowork_agent.registry.agent_env import parse_env_file, serialize_env_file
from services.storage import flock
from services.storage.atomic_write import write_text_durable
from services.storage.layout import secrets_dir


def path() -> Path:
    return secrets_dir() / "router.env"


def _entries() -> list[dict]:
    try:
        return parse_env_file(path().read_text(errors="replace"))
    except FileNotFoundError:
        return []


def read(name: str) -> str | None:
    """The key stored under ``name``, or None. Blocking."""
    found = [e["value"] for e in _entries() if e["key"] == name and e["value"]]
    return found[-1] if found else None


def write(name: str, value: str | None) -> None:
    """Store (or, with None, remove) the key under ``name``. Blocking."""
    target = path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with flock.locked(target):
        kept = [e for e in _entries() if e["key"] != name]
        if value is not None:
            kept.append({"key": name, "value": value})
        write_text_durable(target, serialize_env_file(kept))
