"""``~/xo-projects/.xo/{space,dashboard,sessions}.json`` — the Space data files.

These three used to exist only as route responses, rebuilt per request behind a
30s in-process cache. Now they are real files in the workspace ``.xo``
directory, next to the rest of the workspace's state: one location that holds
everything, materialised by the watcher and read by the routes.

Each file keeps its own name and its own schema — a reader that wants the
session telemetry does not parse the 168 KB graph to get it.

Cadence: the views walk every mapped file in every project, so they are rebuilt
at most every ``XO_VIEWS_REFRESH_S`` (default 30s — the freshness the routes
already served). The watcher tick runs inside ``asyncio.to_thread``
(watcher.run), so that walk never blocks the event loop.

Each view is built under its own guard: a failing session-telemetry provider
must not cost the graph its refresh, and a failed rebuild leaves the previous
file in place rather than truncating it. Stale beats absent — a route can say
how old a file is, it cannot invent one.

The two projections come from ONE scan. The dashboard builder used to call
``build_space_data`` itself, so a client that opened Dashboard and Graph paid
for two full workspace walks; ``build_dashboard_regions(source=space)`` hands
it the graph the space view already built.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import workspace_xo_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json

logger = logging.getLogger(__name__)

# filename -> the builder that fills it
VIEWS = ("space", "dashboard", "sessions")

_last_build = 0.0  # monotonic; a clock jump must not pin the views stale


def refresh_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("XO_VIEWS_REFRESH_S", "30")))
    except ValueError:
        return 30.0


def view_path(name: str) -> Path:
    if name not in VIEWS:
        raise ValueError(f"unknown view {name!r}")
    return workspace_xo_dir() / f"{name}.json"


def scaffold() -> None:
    """Create the three files if they are missing, so the ``.xo`` directory
    always has the shape the UI expects — even before the first build."""
    wxo = workspace_xo_dir()
    wxo.mkdir(parents=True, exist_ok=True)
    for name in VIEWS:
        path = wxo / f"{name}.json"
        if not path.exists():
            write_json_atomic(path, {"schema": 1, "generated_at": None, name: None})


def _write(name: str, payload: dict) -> None:
    write_json_atomic(view_path(name), payload)


def build(name: str) -> Optional[dict]:
    """Build one view and write its file. Returns the payload, or ``None``."""
    return _build_all(only=name).get(name)


def _build_all(only: Optional[str] = None) -> dict:
    from services.cowork_agent.visualizer.dashboard_regions import (
        build_dashboard_regions,
    )
    from services.cowork_agent.visualizer.session_telemetry import (
        build_session_telemetry,
    )
    from services.cowork_agent.visualizer.space_index import build_space_data

    out: dict = {}
    space = None
    # space is built whenever the dashboard is wanted: the projection is
    # derived from it, and building it twice is the duplicate scan this
    # module exists to remove.
    if only in (None, "space", "dashboard"):
        try:
            space = build_space_data()
            if only in (None, "space"):
                _write("space", space)
                out["space"] = space
        except Exception:
            logger.exception("workspace views: space build failed")

    if only in (None, "dashboard"):
        try:
            dashboard = build_dashboard_regions(source=space)
            _write("dashboard", dashboard)
            out["dashboard"] = dashboard
        except Exception:
            logger.exception("workspace views: dashboard build failed")

    if only in (None, "sessions"):
        try:
            sessions = build_session_telemetry()
            _write("sessions", sessions)
            out["sessions"] = sessions
        except Exception:
            logger.exception("workspace views: sessions build failed")

    return out


def apply(*, force: bool = False) -> bool:
    """Watcher entry point. Self-throttles; returns True when it rebuilt."""
    global _last_build
    scaffold()
    now = time.monotonic()
    if not force and (now - _last_build) < refresh_seconds():
        return False
    _last_build = now
    _build_all()
    return True


def read(name: str, *, max_age_s: Optional[float] = None):
    """Return ``(payload, age_seconds)`` from the file, or ``(None, age)``.

    A file older than ``max_age_s``, or a scaffold placeholder that has never
    been built, reads as missing so the caller rebuilds it.
    """
    path = view_path(name)
    payload = read_json(path)
    if not payload or payload.get(name, "__") is None:
        return None, None
    age = None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        age = None
    if max_age_s is not None and age is not None and age > max_age_s:
        return None, age
    return payload, age
