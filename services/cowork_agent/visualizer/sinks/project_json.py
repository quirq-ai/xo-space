"""``project.json`` sink — one-shot identity fill.

The bundled template ships ``project.json`` with ``_template: true``
and null identity fields. On first sight of a project (the first
``SessionFirstSeen`` event the watcher processes for it, or simply
on the first tick where the project is discovered) this sink:

* generates a UUID for ``pid`` if missing
* sets ``name`` to the project id (the user can rename via the UI
  later; the watcher doesn't override an explicit name)
* sets ``owner_user_id`` from the auth state (or ``"local"``)
* sets ``created_at`` to the current ISO timestamp
* removes ``_template`` so subsequent ticks no-op

Idempotent. Runs to completion or no-ops; never partially writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_user_id() -> str:
    """Pull the local user id from the auth state, falling back to
    ``"local"`` (see docs/watcher-design.md §8.1).

    Imported lazily because ``routers.auth`` triggers FastAPI app
    construction at import time in some test paths.
    """
    try:
        from routers.auth.auth import get_auth_state
        return (get_auth_state().get("user_id") or "local")
    except Exception:
        return "local"


def fill_identity(xo_dir: Path, project_id: str) -> bool:
    """Run the one-shot identity fill if needed.

    Returns ``True`` iff ``project.json`` was rewritten.
    """
    if not xo_dir.parent.is_dir():
        # The folder *is* the project (see
        # ``project_layout.resolve_project_dirname``). If the id doesn't
        # point at a directory that exists, writing project.json here would
        # mkdir -p a ghost project rather than describe a real one — an
        # empty folder that then shows up in the UI as its own project.
        return False

    path = xo_dir / "project.json"
    current = read_json(path) or {}

    if not current.get("_template", False) and current.get("pid"):
        # Already filled — no-op.
        return False

    new = {
        "schema":        1,
        "pid":           current.get("pid") or str(uuid.uuid4()),
        "name":          current.get("name") or project_id,
        "owner_user_id": current.get("owner_user_id") or _resolve_user_id(),
        "created_at":    current.get("created_at") or _now_iso(),
    }
    write_json_atomic(path, new)
    return True
