"""The Space data files — the three payloads ``/xo/*.json`` serves."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent.project_layout import (
    workspace_runtime_dir,
    workspace_xo_dir,
)
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json

logger = logging.getLogger(__name__)

# view name -> the builder that fills it. The name is the route's, and since
# T14 it is not necessarily the file's; see :func:`view_path`.
VIEWS = ("space", "dashboard", "sessions")

# The one view whose file is not named after it.
_VIEW_FILENAMES = {"space": "graph.json"}

_last_build = 0.0  # monotonic; a clock jump must not pin the views stale

# ── Single-flight ─────────────────────────────────────────────────────────────
# A rebuild is a full workspace walk plus a ``git log`` per project plus an
# Argus SQLite scan.
_flight_lock = threading.Lock()
_in_flight: dict[str, "_Flight"] = {}


class _Flight:
    """One in-progress build other callers can wait on."""

    __slots__ = ("done", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: dict = {}


def refresh_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("XO_VIEWS_REFRESH_S", "30")))
    except ValueError:
        return 30.0


def view_path(name: str) -> Path:
    """The file one view is written to, under ``~/.quirq/workspace/``."""
    if name not in VIEWS:
        raise ValueError(f"unknown view {name!r}")
    return workspace_runtime_dir() / _VIEW_FILENAMES.get(name, f"{name}.json")


def graph_path() -> Path:
    """``~/.quirq/workspace/graph.json`` — the derived d3 graph."""
    return view_path("space")


# ── The T20 sweep of the abandoned workspace views ────────────────────────────
# What T20 moved is pure derived state, so there is nothing to migrate: the
# next tick rebuilds every one of these files in the runtime tier.

_ABANDONED_FILES = (
    "dashboard.json",
    "sessions.json",
    "stats.json",
    "timeline.jsonl",
    "activity.json",
)

# The rotated timeline segments (``sinks/timeline.py`` renames to
# ``timeline.<stamp>.jsonl``). Same argument as the live file.
_ABANDONED_GLOBS = ("timeline.*.jsonl",)

# Wholly derived, and now written under ``workspace_sessions_dir()``.
_ABANDONED_DIRS = ("sessions",)

# Roots already swept by this process.
_SWEPT: set[str] = set()
_SWEPT_MAX = 64


def sweep_abandoned(*, force: bool = False) -> list[str]:
    """Delete the workspace views T20 moved out of ``<XO root>/.xo/``."""
    root = workspace_xo_dir()
    marker = str(root)
    if marker in _SWEPT and not force:
        return []
    removed: list[str] = []
    try:
        if not root.is_dir():
            # Nothing to sweep, and deliberately not remembered: the synced
            # workspace directory does not exist until something writes a
            # record into it, and a restore can put a pre-T20 tree there later
            # in the life of this process.
            return removed
        candidates = [root / name for name in _ABANDONED_FILES]
        for pattern in _ABANDONED_GLOBS:
            candidates.extend(sorted(root.glob(pattern)))
        for path in candidates:
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(path.name)
            except OSError:
                logger.warning("workspace views: could not remove %s", path)
        for name in _ABANDONED_DIRS:
            target = root / name
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                    removed.append(name + "/")
            except OSError:
                logger.warning("workspace views: could not remove %s", target)
    except OSError:
        logger.warning("workspace views: could not sweep %s", root)
        return removed
    if len(_SWEPT) >= _SWEPT_MAX:
        _SWEPT.clear()
    _SWEPT.add(marker)
    if removed:
        logger.info(
            "workspace views: removed pre-T20 derived state from %s: %s",
            root,
            ", ".join(removed),
        )
    return removed


def scaffold() -> None:
    """
    Create the three files if they are missing, so the directories always have
    the shape the UI expects — even before the first build.
    """
    workspace_runtime_dir().mkdir(parents=True, exist_ok=True)
    for name in VIEWS:
        path = view_path(name)
        if not path.exists():
            # write_json_atomic creates the parent, which is
            # ~/.quirq/workspace/ on a machine that has never built one.
            write_json_atomic(path, {"schema": 1, "generated_at": None, name: None})


def _write(name: str, payload: dict) -> None:
    write_json_atomic(view_path(name), payload)


def build(name: str) -> Optional[dict]:
    """Build one view and write its file. Returns the payload, or ``None``."""
    return _build_all(only=name).get(name)


def _build_all(only: Optional[str] = None) -> dict:
    """Build the requested views, collapsing concurrent callers into one build."""
    key = only or "*"
    with _flight_lock:
        flight = _in_flight.get(key)
        if flight is not None:
            leader = False
        else:
            flight = _Flight()
            _in_flight[key] = flight
            leader = True

    if not leader:
        flight.done.wait()
        return flight.result

    try:
        flight.result = _build_all_locked(only)
        return flight.result
    finally:
        with _flight_lock:
            _in_flight.pop(key, None)
        # Set last: a follower must not wake to a half-populated result.
        flight.done.set()


def _build_all_locked(only: Optional[str] = None) -> dict:
    global _last_build

    from services.cowork_agent.visualizer.categorized_graph import (
        build_categorized_graph,
    )
    from services.cowork_agent.visualizer.session_telemetry import (
        build_session_telemetry,
    )
    from services.cowork_agent.visualizer.space_index import build_space_data

    # ``_last_build`` is stamped AFTER the work, at the bottom, and only when
    # something actually built.
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
            dashboard = build_categorized_graph(source=space)
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

    # Partial progress counts.
    if out:
        _last_build = time.monotonic()

    return out


def apply(*, force: bool = False) -> bool:
    """Watcher entry point. Self-throttles; returns True when it rebuilt."""
    global _last_build
    scaffold()
    sweep_abandoned()
    now = time.monotonic()
    if not force and (now - _last_build) < refresh_seconds():
        return False
    _last_build = now
    _build_all()
    return True


# ── Freshness (syncplan T25) ──────────────────────────────────────────────────
# A view's age comes from the document's own ``generated_at``, never from the
# file's mtime.


def _generated_at(payload: dict) -> Any:
    meta = payload.get("meta")
    if isinstance(meta, dict) and meta.get("generated_at") is not None:
        return meta.get("generated_at")
    return payload.get("generated_at")


def _parse_stamp(stamp: Any) -> Optional[datetime]:
    """Parse an ISO-8601 ``generated_at``; ``None`` when it is unusable."""
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    text = stamp.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_seconds(payload: dict) -> Optional[float]:
    """Seconds since the document says it was generated, or ``None``."""
    parsed = _parse_stamp(_generated_at(payload))
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def is_stale(age: Optional[float], max_age_s: Optional[float]) -> bool:
    """Fail **closed**: an unknown age is stale."""
    if max_age_s is None:
        return False
    return age is None or age > max_age_s


def read(
    name: str,
    *,
    max_age_s: Optional[float] = None,
    stale_ok: bool = False,
):
    """Return ``(payload, age_seconds)`` from the file, or ``(None, age)``."""
    path = view_path(name)
    try:
        payload = read_json(path)
    except OSError:
        # Fail closed. ``read_json`` swallows this itself today, but the guard
        # is the point: an unreadable view must read as missing, not as fresh.
        logger.warning("workspace views: could not read %s", path)
        return None, None
    if not payload or payload.get(name, "__") is None:
        return None, None
    age = age_seconds(payload)
    if is_stale(age, max_age_s) and not stale_ok:
        return None, age
    return payload, age
