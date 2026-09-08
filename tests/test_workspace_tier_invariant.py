"""The workspace half of the tier invariant, checked against a real tick (T20).

``tests/test_tier_invariant.py`` is the per-project half: T19 moved
``stats.json``, ``timeline.jsonl``, ``sync.json`` and ``sessions/`` out of
``<project>/.xo/``. T20 does the same thing one level up — the workspace
rollups (``graph.json``, ``dashboard.json``, ``sessions.json``, ``stats.json``,
``timeline.jsonl`` and everything under ``sessions/``) leave
``<XO root>/.xo/`` for ``~/.quirq/workspace/``, and the synced workspace tier
is left holding only the three documents a clone would want: ``space.json``,
``projects.json`` and ``xo.json``.

**Why a tick and not a grep.** Every failure mode in this move is silent. A
sink that still resolves the old root writes a file nobody reads; a reader that
still resolves the old root reads a file nobody writes. Neither raises, and
both look fine in a diff — so the assertion has to be on what a real
``Watcher.tick()`` actually leaves on disk.

The second case is the one a static test cannot see at all: this is a *move*,
so the old copies exist on every box that has ever run the watcher, and
``<XO root>/.xo/`` is the SYNCED tier. T21 force-includes that directory in the
backup tarball, which would make an abandoned ``timeline.jsonl`` this machine's
telemetry travelling to every other machine and every restore.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout, scopes
from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.visualizer import watcher as watcher_module
from services.cowork_agent.visualizer.ingest.events import (
    MessageObserved,
    SessionFirstSeen,
    UsageObserved,
)
from services.cowork_agent.visualizer.workspace import space_json, views

#: What the synced workspace tier is allowed to hold after a tick.
#: ``space.json`` is the durable Space record (§5.3), ``projects.json`` the
#: registry (§5.2), ``xo.json`` the frontend manifest, which §4 keeps put.
SYNCED_CONTRACT = ["projects.json", "space.json", "xo.json"]

#: What a pre-T20 build wrote into ``<XO root>/.xo/`` and no longer may.
MOVED = (
    "dashboard.json",
    "sessions.json",
    "stats.json",
    "timeline.jsonl",
    "sessions",
    # never written since ``workspace_activity_path()`` moved to
    # ``~/.quirq/watcher/activity/``; T20 names it explicitly.
    "activity.json",
)

TS = "2026-09-07T12:00:00Z"


class _FakeSource:
    """One backend's source, replaying a fixed batch. Named ``claude_code``
    only because the watcher asserts a source's name matches its manifest."""

    name = "claude_code"

    def __init__(self, events):
        self._events = events

    def poll_events(self):
        return list(self._events)

    def poll_presence(self):
        return []


class WorkspaceTierTests(unittest.TestCase):
    PROJECT = "Demo Project"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "xo-projects"
        self.root.mkdir(parents=True)
        self.state = self.tmp / "state"
        self.state.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
                "XO_PROJECT_TEMPLATE": "",
                "AGENT_NAME": "claude_code",
                # The telemetry view scans the session stores under $HOME.
                "HOME": str(self.tmp),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        project_layout.scaffold_project(self.PROJECT)
        self.dirname = project_layout.resolve_project_dirname(self.PROJECT)
        self.ws_xo = self.root / ".xo"
        self.ws_runtime = self.state / "workspace"
        # Both workspace sinks self-throttle on module state, and the views
        # sink also remembers which roots it has swept. All of it outlives one
        # test, so a tick driven without this reset silently does nothing.
        views._last_build = 0.0
        views._SWEPT.clear()
        space_json._last_build = 0.0

    # ── helpers ──────────────────────────────────────────────────────────

    def events(self):
        return [
            SessionFirstSeen(
                ts=TS, project_id=self.dirname, native_session_id="native-1",
                runtime="claude_code", cwd=str(self.root / self.dirname),
            ),
            MessageObserved(
                ts=TS, project_id=self.dirname, native_session_id="native-1",
                runtime="claude_code", role="assistant",
            ),
            UsageObserved(
                ts=TS, project_id=self.dirname, native_session_id="native-1",
                runtime="claude_code", input_tokens=10, output_tokens=5, model="m",
            ),
        ]

    def tick(self, events=None) -> None:
        space_json._last_build = 0.0
        w = watcher_module.Watcher.__new__(watcher_module.Watcher)
        w.sources = [_FakeSource(self.events() if events is None else events)]
        w.model_by_session = {}
        w.tick_count = 0
        w.tick()

    def pre_t20_tree(self) -> None:
        """Recreate what a pre-T20 watcher left in ``<XO root>/.xo/``."""
        self.ws_xo.mkdir(parents=True, exist_ok=True)
        for name in ("dashboard.json", "sessions.json", "stats.json",
                     "activity.json", "xo.json"):
            (self.ws_xo / name).write_text(
                json.dumps({"schema": 1}), encoding="utf-8"
            )
        (self.ws_xo / "timeline.jsonl").write_text("{}\n", encoding="utf-8")
        (self.ws_xo / "timeline.20260825T120000Z.jsonl").write_text(
            "{}\n", encoding="utf-8"
        )
        sessions = self.ws_xo / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / "sessionslist.json").write_text("{}", encoding="utf-8")
        (sessions / "sessions-augment.json").write_text(
            json.dumps({"schema": 2, "sessions": {}}), encoding="utf-8"
        )

    def synced_names(self) -> list[str]:
        return sorted(p.name for p in self.ws_xo.iterdir())

    def runtime_names(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.ws_runtime))
            for p in self.ws_runtime.rglob("*")
            if p.is_file()
        )

    # ── the invariant ────────────────────────────────────────────────────

    def test_a_tick_writes_the_workspace_views_into_the_runtime_tier(self) -> None:
        self.tick()
        for rel in ("graph.json", "dashboard.json", "stats.json",
                    "timeline.jsonl", "sessions/sessionslist.json",
                    "sessions/sessions-augment.json"):
            with self.subTest(rel=rel):
                self.assertTrue(
                    (self.ws_runtime / rel).is_file(), f"{rel} was not written"
                )

    def test_a_tick_writes_no_moved_file_into_the_synced_tier(self) -> None:
        self.tick()
        for name in MOVED:
            with self.subTest(name=name):
                self.assertFalse(
                    (self.ws_xo / name).exists(),
                    f"<XO root>/.xo/{name} is derived state and must not be "
                    "in the tier a clone gets",
                )

    def test_the_synced_workspace_tier_holds_only_its_contract(self) -> None:
        """The point of the move, stated as a whitelist. ``xo.json`` is seeded
        by the manifest builder, not the watcher, so the test writes it."""
        (self.ws_xo).mkdir(parents=True, exist_ok=True)
        (self.ws_xo / "xo.json").write_text(
            json.dumps({"schema": 1}), encoding="utf-8"
        )
        self.tick()
        self.assertEqual(self.synced_names(), SYNCED_CONTRACT)

    def test_a_tick_sweeps_a_pre_t20_workspace_clean(self) -> None:
        """The upgrade path: every box that has run the watcher already has
        these files, and nothing else in the plan removes them before T21
        force-includes ``<XO root>/.xo/`` in the backup tarball."""
        self.pre_t20_tree()
        self.tick()
        self.assertEqual(self.synced_names(), SYNCED_CONTRACT)

    def test_the_space_record_survives_the_sweep(self) -> None:
        """``space.json`` shares the directory the views were swept out of and
        is the one file there that cannot be rebuilt (T14/§5.3)."""
        self.tick()
        record = json.loads(
            (self.ws_xo / "space.json").read_text(encoding="utf-8")
        )
        self.pre_t20_tree()
        views._SWEPT.clear()
        views._last_build = 0.0
        self.tick(events=[])
        self.assertEqual(
            json.loads((self.ws_xo / "space.json").read_text(encoding="utf-8")),
            record,
        )

    def test_the_workspace_scope_reads_what_the_tick_wrote(self) -> None:
        """The half a write-side test cannot see: 7 BFF routes reach these
        files through ``WorkspaceVisualizerScope``, and a reader left on the
        old root answers empty rather than raising."""
        # The session index is adapter-written, not watcher-written, so the
        # union has nothing to roll up unless a row exists first.
        session_index.write_session_row(
            self.dirname,
            "claude_code:native-1",
            {"sessionId": "sess-1", "nativeSessionId": "native-1",
             "backend": "claude_code"},
        )
        self.tick()
        workspace = scopes.resolve_scope("xo-workspace-visualizer")
        self.assertIsNotNone(workspace.read_stats())
        self.assertTrue(workspace.read_timeline(limit=50))
        self.assertIn("claude_code:native-1", workspace.read_sessionslist())


if __name__ == "__main__":
    unittest.main()
