"""The Space data files — the three payloads ``/xo/*.json`` serves.

These three used to exist only as route responses, rebuilt per request behind a
30s in-process cache. Now they are real files, materialised by the watcher and
read by the routes.

Each file keeps its own name and its own schema — a reader that wants the
session telemetry does not parse the 168 KB graph to get it.

**The view name and the file name are no longer the same thing** (syncplan
§5.3, T14). ``space`` — the d3 graph — is written to
``~/.quirq/workspace/graph.json``, not to ``<XO root>/.xo/space.json``. Two
reasons, and only the first is about tiering:

* the graph is 100% derived from a workspace walk, so R-TIER puts it in the
  machine-local runtime tier with the rest of the derived state;
* ``<XO root>/.xo/space.json`` is needed for the durable Space *record*
  (``workspace/space_json.py``), and it cannot hold identity while it is this
  file: the graph is rebuilt by two unlocked writers — this module from the
  watcher tick, and ``build`` from a request thread — so anything written
  beside it is destroyed on the next rebuild.

``GET /xo/space.json`` still serves the graph, so no consumer changed; the URL
simply stopped mirroring the path.

**T20 finished the move the graph started.** ``dashboard`` and ``sessions``
were the last two views left in ``<XO root>/.xo/``; they are derived from the
same walk as the graph, and the second bullet above applies to them word for
word. All three now live under ``project_layout.workspace_runtime_dir()``, and
``view_path`` is still the one place that knows which file a view is.

Cadence: the views walk every mapped file in every project, so they are rebuilt
at most every ``XO_VIEWS_REFRESH_S`` (default 30s — the freshness the routes
already served). The watcher tick runs inside ``asyncio.to_thread``
(watcher.run), so that walk never blocks the event loop.

Each view is built under its own guard: a failing session-telemetry provider
must not cost the graph its refresh, and a failed rebuild leaves the previous
file in place rather than truncating it. Stale beats absent — a route can say
how old a file is, it cannot invent one.

The two projections come from ONE scan. ``build_categorized_graph`` used to
call ``build_space_data`` itself, so a client that opened Dashboard and Graph
paid for two full workspace walks.
"""

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

# view name -> the builder that fills it. The name is the route's, and
# since T14 it is not necessarily the file's; see :func:`view_path`.
VIEWS = ("space", "dashboard", "sessions")

# The one view whose file is not named after it. ``space.json`` at the
# workspace root is the durable Space *record* (§5.3), so the graph took a
# different name rather than a different directory — and kept it through T20.
_VIEW_FILENAMES = {"space": "graph.json"}

_last_build = 0.0  # monotonic; a clock jump must not pin the views stale

# ── Single-flight ─────────────────────────────────────────────────────────────
#
# A rebuild is a full workspace walk plus a ``git log`` per project plus an
# Argus SQLite scan. Before this, N clients arriving together on a stale view
# started N of them: ``build`` consulted no throttle, and stamping
# ``_last_build`` up front did not help because every thread stamped it and
# then built anyway. Measured: 8 concurrent requests -> 8 rebuilds.
#
# ``build`` runs in a threadpool worker (``asyncio.to_thread`` from
# ``routers/xo_data.py``), so this has to be thread-safe, not merely
# coroutine-safe. One thread per key does the work; the rest wait on its Event
# and share the result. The slot is removed in a ``finally`` so a build that
# raises cannot poison the key for the life of the process.
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
    """``~/.quirq/workspace/graph.json`` — the derived d3 graph.

    Runtime tier: entirely rebuildable from a workspace walk, and it must
    not occupy ``<XO root>/.xo/space.json``, which is the durable Space
    record. Served unchanged at ``GET /xo/space.json``.
    """
    return view_path("space")


# ── The T20 sweep of the abandoned workspace views ────────────────────────────
#
# What T20 moved is pure derived state, so there is nothing to migrate: the
# next tick rebuilds every one of these files in the runtime tier. What is left
# behind is not harmless, though. ``<XO root>/.xo/`` is the SYNCED tier — T21
# force-includes it in the backup tarball — so a stale ``timeline.jsonl`` or
# session index there is machine-local telemetry that would travel to every
# other machine and every restore, which is precisely what R-TIER forbids. And
# a frozen ``dashboard.json`` sitting where a reader used to look is the silent
# failure this whole phase is written against.
#
# ``activity.json`` is on the list for a different reason: nothing has written
# it since ``workspace_activity_path()`` moved to ``~/.quirq/watcher/activity/``
# (syncplan T20), so every copy on disk is already dead.
#
# ``space.json``, ``projects.json`` and ``xo.json`` are deliberately absent —
# they are the three documents the synced workspace tier keeps.

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

# Roots already swept by this process. Keyed rather than a bare flag so a test
# suite churning through temp roots — and a process that sees its projects root
# repointed — still sweeps each one exactly once. Bounded the same way as
# project_layout's caches: a long-lived process only ever sees one root.
_SWEPT: set[str] = set()
_SWEPT_MAX = 64


def sweep_abandoned(*, force: bool = False) -> list[str]:
    """Delete the workspace views T20 moved out of ``<XO root>/.xo/``.

    Idempotent, and once per root: a repeat pass is a handful of wasted
    ``stat`` calls on every tick for a directory that can only be repopulated
    by a build of the pre-T20 code. Returns what it removed, which is what the
    tests assert on.

    Never raises. This is a tidy-up on the front of a watcher tick; a
    permission error on someone else's file must not cost the tick its views.
    """
    root = workspace_xo_dir()
    marker = str(root)
    if marker in _SWEPT and not force:
        return []
    removed: list[str] = []
    try:
        if not root.is_dir():
            # Nothing to sweep, and deliberately not remembered: the synced
            # workspace directory does not exist until something writes a
            # record into it, and a restore can put a pre-T20 tree there
            # later in the life of this process. The pass cost one ``stat``.
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
    """Create the three files if they are missing, so the directories
    always have the shape the UI expects — even before the first build."""
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
    """Build the requested views, collapsing concurrent callers into one build.

    Callers that arrive while a build for the same key is running wait for it
    and get its result rather than starting their own.
    """
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
    # something actually built. It used to be stamped here, up front, for two
    # jobs at once: resetting the watcher throttle after a route-triggered
    # rebuild (syncplan T25, correct), and stopping a concurrent tick starting
    # a second build (now the single-flight's job, and it never worked here —
    # every racing thread stamped and built anyway). Conflating them meant a
    # build that failed *entirely* still silenced the watcher's next tick,
    # leaving graph.json on its scaffold placeholder.
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

    # Partial progress counts. Stamping only on a *complete* build would make
    # one permanently-broken view (telemetry down, say) rebuild the other two
    # on every single tick; not stamping at all after a total failure is what
    # keeps the watcher retrying instead of going quiet.
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
#
# A view's age comes from the document's own ``generated_at``, never from
# the file's mtime. The mtime is a fact about the *file*: it moves when the
# data did not (a restore, a copy, a `touch`) and stands still when the data
# is current (a write skipped because the content did not change — which is
# exactly what syncplan T26 is about to make the common case). Either way the
# routes' 120-second staleness test reads the wrong thing.
#
# ``meta.mappedOn`` is not a substitute: it is ``date.today()`` rendered for
# humans, so at best it says "today".
#
# All three views stamp ``meta.generated_at``: the graph
# (``space_index``), the dashboard (``categorized_graph``) and the session
# telemetry (``session_telemetry``, which already did). A top-level
# ``generated_at`` is accepted too — that is the shape ``scaffold`` writes.


def _generated_at(payload: dict) -> Any:
    meta = payload.get("meta")
    if isinstance(meta, dict) and meta.get("generated_at") is not None:
        return meta.get("generated_at")
    return payload.get("generated_at")


def _parse_stamp(stamp: Any) -> Optional[datetime]:
    """Parse an ISO-8601 ``generated_at``; ``None`` when it is unusable.

    Tolerates the trailing ``Z`` on every Python this runs on, and reads a
    naive stamp as UTC — the three writers all emit UTC.
    """
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
    """Seconds since the document says it was generated, or ``None``.

    ``None`` means *unknown*, and unknown is treated as stale everywhere —
    see :func:`is_stale`.
    """
    parsed = _parse_stamp(_generated_at(payload))
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def is_stale(age: Optional[float], max_age_s: Optional[float]) -> bool:
    """Fail **closed**: an unknown age is stale.

    The polarity used to be inverted. ``age is not None and age > max_age_s``
    let an unreadable timestamp through as fresh — served forever, no matter
    how old — while a perfectly good payload that happened to be old was
    thrown away (syncplan T25).
    """
    if max_age_s is None:
        return False
    return age is None or age > max_age_s


def read(
    name: str,
    *,
    max_age_s: Optional[float] = None,
    stale_ok: bool = False,
):
    """Return ``(payload, age_seconds)`` from the file, or ``(None, age)``.

    ``age`` is derived from the document's own ``generated_at``; it is
    ``None`` when the file carries no usable stamp, and ``None`` counts as
    stale (:func:`is_stale`).

    A scaffold placeholder that has never been built always reads as
    missing. A document older than ``max_age_s`` reads as missing too,
    unless ``stale_ok`` — with which the caller gets the stale-but-valid
    payload *and* its age, so it can rebuild and still have something
    correct to serve if that rebuild fails. That is what
    ``routers/xo_data.py`` does; the default keeps the older contract for
    every other reader.
    """
    path = view_path(name)
    try:
        payload = read_json(path)
    except OSError:
        # Fail closed. ``read_json`` swallows this itself today, but the
        # guard is the point: an unreadable view must read as missing, not
        # as fresh.
        logger.warning("workspace views: could not read %s", path)
        return None, None
    if not payload or payload.get(name, "__") is None:
        return None, None
    age = age_seconds(payload)
    if is_stale(age, max_age_s) and not stale_ok:
        return None, age
    return payload, age
