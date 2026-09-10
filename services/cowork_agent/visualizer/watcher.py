"""Watcher main loop — drains the runtime source(s), fans events to
sinks, refreshes the workspace tier, and persists offsets.

Started from FastAPI lifespan in ``server.py`` (next to the existing
``usage_sync`` task). Non-fatal: failure to start, or any exception
inside a tick, logs and continues — the BFF endpoints keep serving
whatever data is on disk.

Tick budget for v1: ~1 s. Source poll + sinks + workspace tier per
tick. The blocking I/O work runs in ``asyncio.to_thread`` so the
event loop stays responsive to other FastAPI traffic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.registry.agent_registry import all_agents, get_active_agent
from services.cowork_agent.project_layout import runtime_dir_for_project, xo_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    TaskCreated,
    TaskStatusChanged,
    UsageObserved,
)
from services.cowork_agent.visualizer.sinks import (
    activity,
    project_json,
    sessions_augment,
    stats,
    timeline,
)
from services.cowork_agent.visualizer.state import (
    project_activity_path,
    watcher_heartbeat_path,
)
from services.cowork_agent.visualizer.workspace import (
    activity as ws_activity,
)
from services.cowork_agent.visualizer.workspace import (
    sessions_augment as ws_sessions_augment,
)
from services.cowork_agent.visualizer.workspace import (
    sessionslist as ws_sessionslist,
)
from services.cowork_agent.visualizer.workspace import (
    stats as ws_stats,
)
from services.cowork_agent.visualizer.workspace import (
    timeline as ws_timeline,
)
from services.cowork_agent.visualizer.workspace import (
    projects_json,
    space_json,
    views as ws_views,
)
from services.cowork_agent.visualizer.workspace_index import (
    list_project_ids,
    project_index_scope,
)
from utils.commands import scheduler

logger = logging.getLogger(__name__)

def _now_iso() -> str:
    """UTC, second granularity — the same stamp format the sinks write."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _poll_interval_seconds() -> float:
    raw = (os.getenv("QUIRQ_WATCHER_INTERVAL_SECONDS", "1") or "1").strip()
    try:
        interval = float(raw)
    except ValueError:
        interval = 1.0
    return min(60.0, max(0.25, interval))


POLL_INTERVAL_S = _poll_interval_seconds()


def _sink_events(events: list) -> list:
    """Drop the task family before the sinks see it."""
    return [
        ev for ev in events
        if not isinstance(ev, (TaskCreated, TaskStatusChanged))
    ]


class Watcher:
    """Single-tick orchestrator.

    Holds the source(s) and any in-process caches sinks need. State
    that survives restarts lives on disk (the source's offset store,
    the sinks' own files). Only in-memory state here is
    ``model_by_session`` — a cache of the last assistant model id
    seen per session, used by the activity sink to satisfy the
    schema's required ``agent`` field. Lost on restart; refilled
    from the next assistant turn (acceptable per
    docs/watcher-design.md §8.2).
    """

    def __init__(self) -> None:
        # Local Docker can watch every mounted native store at once; hosted
        # deployments retain the historical active-backend-only default.
        # Discovery still goes through the one capability-loader seam, so a
        # new manifest + adapter source needs no watcher edit.
        offsets = jsonl_tail.OffsetStore()
        source_mode = (
            os.getenv("QUIRQ_WATCHER_SOURCE_MODE", "active").strip().lower()
        )
        manifests = (
            all_agents()
            if source_mode == "all"
            else [get_active_agent()]
        )
        self.sources = []
        for manifest in manifests:
            mod = try_load_capability("visualizer_source", agent=manifest.name)
            if mod is None or not hasattr(mod, "Source"):
                continue
            source = mod.Source(offsets=offsets)
            assert source.name == manifest.name, (
                f"visualizer_source.Source.name {source.name!r} does not match "
                f"manifest {manifest.name!r}"
            )
            self.sources.append(source)
        self.model_by_session: dict[str, str] = {}
        # Monotonically increasing count of ticks executed since start;
        # published in the heartbeat so a reader can tell a watcher that is
        # ticking from one whose file merely happens to be recent.
        self.tick_count = 0
        # What the command scheduler did on the last tick (ids only), or
        # None before the first tick; published in the heartbeat.
        self.last_scheduler_report: Optional[dict] = None

    # ── One tick ────────────────────────────────────────────────────────

    def tick(self) -> None:
        """
        One pass: drain sources, fan to sinks, refresh the workspace tier,
        beat.
        """
        with project_index_scope():
            self._tick_body()

    def _tick_body(self) -> None:
        tick_started = time.monotonic()

        # 1. Drain every source.
        events: list = []
        for src in self.sources:
            try:
                events.extend(src.poll_events())
            except Exception:
                logger.exception("source %s failed; continuing with others", src.name)

        # 2. Maintain the model cache from UsageObserved (model id is
        # attached there for assistant turns).
        for ev in events:
            if isinstance(ev, UsageObserved) and ev.model:
                self.model_by_session[ev.native_session_id] = ev.model

        # 3. Group by project_id.
        events_by_project: dict[str, list] = defaultdict(list)
        for ev in events:
            if ev.project_id:
                events_by_project[ev.project_id].append(ev)

        # 4. Per-project sinks. Run project_json first so identity is
        # filled before any sink writes a record referencing it; run
        # timeline last so its emitted lines can be fanned to the
        # workspace timeline.
        for project_id, project_events in events_by_project.items():
            x = xo_dir(project_id)
            sink_events = _sink_events(project_events)
            try:
                # Identity FIRST, then resolve the runtime home.
                project_json.fill_identity(x, project_id)
                rt = runtime_dir_for_project(project_id, create=True)
                if rt is None:
                    continue
                sessions_augment.apply(rt, sink_events, legacy_root=x)
                stats.apply(rt, sink_events, legacy_root=x)
                timeline_lines = timeline.apply(rt, sink_events)
            except Exception:
                logger.exception("sink batch failed for project %s", project_id)
                continue

            # Workspace timeline gets the same rendered lines, tagged.
            if timeline_lines:
                try:
                    ws_timeline.apply(timeline_lines, project_id=project_id)
                except Exception:
                    logger.exception("workspace timeline failed for %s", project_id)

        # 5. Activity sink — driven by presence snapshot, not events.
        # Runs for every project (even those with no events this tick)
        # so a session that exited gets evicted from the machine-local
        # presence snapshot under ~/.quirq/watcher/activity/.
        presence: list[dict] = []
        for src in self.sources:
            try:
                presence.extend(src.poll_presence())
            except Exception:
                logger.exception("presence poll failed for %s", src.name)
        presence_by_project: dict[str, list] = defaultdict(list)
        for row in presence:
            pid = row.get("project_id")
            if isinstance(pid, str) and pid:
                presence_by_project[pid].append(row)

        # Resolved once, here, and threaded through the workspace tier below.
        # Inside the scope this is the walk every other caller in this tick
        # reuses.
        project_ids = list_project_ids()

        for pid in project_ids:
            # Identity fill is idempotent (no-ops once _template is cleared).
            # Running it here — alongside the per-project activity sink that
            # already iterates every known project — closes the gap where a
            # scaffolded project that has not yet produced events would
            # otherwise sit on `_template: true` indefinitely. Matches the
            # documented intent of the project_json sink (see its docstring).
            try:
                project_json.fill_identity(xo_dir(pid), pid)
            except Exception:
                logger.exception("identity fill failed for %s", pid)
            try:
                activity.apply(
                    project_activity_path(pid),
                    presence_by_project.get(pid, []),
                    model_by_session=self.model_by_session,
                )
            except Exception:
                logger.exception("activity sink failed for %s", pid)

        # 6. Workspace tier — re-aggregated every tick. All of these are
        # cheap (small JSON, one iterdir) except ws_views, which walks every
        # mapped file in the workspace and therefore throttles itself to
        # XO_VIEWS_REFRESH_S. Timeline is append-only, handled in step 4.
        try:
            projects_json.apply(project_ids)
            space_json.apply()  # self-throttled; the Space record barely moves
            ws_views.apply()   # self-throttled; the only expensive sink here
            ws_stats.apply(project_ids)
            ws_activity.apply(project_ids)
            ws_sessionslist.apply(project_ids)
            ws_sessions_augment.apply(project_ids)
        except Exception:
            logger.exception("workspace tier failed")

        # 7. Scheduled commands. This loop is only the scheduler's clock:
        # the scheduler owns the policy and the state, and its tick only
        # launches jobs (it never waits for one), so this step costs two
        # small reads when nothing is due.
        self._scheduler_step()

        # 8. Liveness beat — last, so duration_ms covers the real tick.
        self._write_heartbeat(tick_started)

    def _scheduler_step(self) -> None:
        """Give the command scheduler its once-per-tick call. Never raises:
        a scheduler bug must not stop telemetry ingestion."""
        try:
            report = scheduler.tick()
        except Exception:
            logger.exception("scheduler tick failed (non-fatal)")
            self.last_scheduler_report = {"error": "scheduler tick raised; see log"}
            return
        self.last_scheduler_report = report.as_dict()
        if not report.quiet:
            logger.info("scheduler: %s", self.last_scheduler_report)

    def _write_heartbeat(self, tick_started: float) -> None:
        """Persist the once-per-tick liveness beat. Never raises."""
        self.tick_count += 1
        try:
            write_json_atomic(
                watcher_heartbeat_path(),
                {
                    "last_tick_at": _now_iso(),
                    "tick_count": self.tick_count,
                    "duration_ms": int(
                        round((time.monotonic() - tick_started) * 1000)
                    ),
                    "scheduler": self.last_scheduler_report,
                },
            )
        except Exception:
            logger.exception("heartbeat write failed (non-fatal)")

    # ── Async runner ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Long-running coroutine for the lifespan task.

        Each tick's blocking I/O happens in ``asyncio.to_thread`` so
        the FastAPI loop stays responsive. Per-tick exceptions are
        swallowed (logged) so one bad tick doesn't stop the watcher
        — only ``CancelledError`` ends the loop.
        """
        logger.info("Watcher started; polling every %.1fs", POLL_INTERVAL_S)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.tick)
                except Exception:
                    logger.exception("watcher tick failed (non-fatal)")
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            logger.info("Watcher shutting down")
            raise


# ── Public entry-point used by ``server.py`` lifespan ────────────────────────


_watcher: Optional[Watcher] = None


async def start_watcher() -> None:
    """Construct (once) and run the watcher loop. Designed to be
    spawned as an asyncio task from FastAPI's lifespan handler.
    """
    global _watcher
    if _watcher is None:
        _watcher = Watcher()
    await _watcher.run()
