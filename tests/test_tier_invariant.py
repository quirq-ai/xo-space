"""The tier invariant, checked against a real watcher tick (syncplan T19).

R-TIER: ``.xo/`` is what a clone would want; ``~/.quirq/`` is everything else.
T19 is the move that makes that true for per-project state — ``stats.json``,
``timeline.jsonl``, ``sync.json`` and everything under ``sessions/`` left
``<project>/.xo/`` for ``~/.quirq/projects/<key>/``.

**Why a tick and not a grep.** ``tests/test_path_chokepoint_guard.py`` is the
static half: no file may hand-build a tier path. It cannot see a helper that
resolves to the wrong root, and every failure mode in this move is silent — a
read returns empty, a write lands where nothing looks. So the dynamic half is
here: drive one real ``Watcher.tick()`` over a real scaffolded project and
assert on what appeared on disk.

Three hazards get their own cases, because each one produces a working system
that quietly loses data:

* **Ordering.** The runtime key is ``project.json:pid``, and the identity sink
  is what mints it. Resolving the runtime home before ``fill_identity`` keys it
  by folder name for one tick and by pid forever after.
* **The pre-mint window.** A request thread — a chat started in the second
  between ``scaffold_project`` and the next tick — resolves before any pid
  exists and publishes under the fallback key. That state has to be adopted,
  not stranded.
* **Skip, don't conjure.** ``fill_identity`` refuses to mint for a project
  folder that does not exist, so a pid may legitimately never appear. Runtime
  resolution must answer "nothing" rather than create a home for a ghost.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.visualizer import watcher as watcher_module
from services.cowork_agent.visualizer.ingest.events import (
    MessageObserved,
    SessionFirstSeen,
    UsageObserved,
)
from services.cowork_agent.visualizer.sinks import project_json

#: The per-project state that left the project tree. Nothing under
#: ``<project>/.xo/`` may carry any of these names again.
MOVED = ("stats.json", "timeline.jsonl", "sync.json", "sessions")

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


class _TickCase(unittest.TestCase):
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
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        project_layout.scaffold_project(self.PROJECT)
        self.dirname = project_layout.resolve_project_dirname(self.PROJECT)
        self.xo = self.root / self.dirname / ".xo"

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
        w = watcher_module.Watcher.__new__(watcher_module.Watcher)
        w.sources = [_FakeSource(self.events() if events is None else events)]
        w.model_by_session = {}
        w.tick_count = 0
        w.tick()

    def pid(self) -> str | None:
        try:
            return json.loads((self.xo / "project.json").read_text())["pid"]
        except (OSError, ValueError, KeyError):
            return None

    def runtime_dirs(self) -> list[str]:
        base = self.state / "projects"
        return sorted(p.name for p in base.iterdir()) if base.is_dir() else []

    def project_tree(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root / self.dirname))
            for p in (self.root / self.dirname).rglob("*")
            if p.is_file()
        )


class TickInvariantTests(_TickCase):
    def test_a_tick_writes_no_moved_file_into_the_project_tree(self) -> None:
        self.tick()
        for name in MOVED:
            with self.subTest(name=name):
                self.assertFalse(
                    (self.xo / name).exists(),
                    f".xo/{name} is runtime state and must not be in the project tree",
                )

    def test_a_tick_writes_the_moved_files_into_the_runtime_home(self) -> None:
        self.tick()
        runtime = project_layout.runtime_dir_for_project(self.dirname)
        self.assertIsNotNone(runtime)
        for rel in ("stats.json", "timeline.jsonl", "sessions/sessions-augment.json"):
            with self.subTest(rel=rel):
                self.assertTrue((runtime / rel).is_file(), f"{rel} was not written")

    def test_the_project_tree_holds_only_the_synced_contract(self) -> None:
        """The whole point of the move, stated as a whitelist: after a tick the
        project folder holds documents a clone would want and nothing else."""
        self.tick()
        xo_files = sorted(
            str(p.relative_to(self.xo)) for p in self.xo.rglob("*") if p.is_file()
        )
        self.assertEqual(xo_files, ["peers.json", "project.json", "todos.json"])

    def test_a_tick_never_writes_into_the_project_tree_twice(self) -> None:
        """A second tick with no new events must not touch the synced tier at
        all — a derived write landing there is what the move removes."""
        self.tick()
        before = {
            p: p.stat().st_mtime_ns for p in (self.root / self.dirname).rglob("*")
            if p.is_file()
        }
        self.tick(events=[])
        after = {
            p: p.stat().st_mtime_ns for p in (self.root / self.dirname).rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)

    def test_the_runtime_home_is_outside_every_project_tree(self) -> None:
        self.tick()
        runtime = project_layout.runtime_dir_for_project(self.dirname)
        self.assertNotIn(str(self.root), str(runtime))
        self.assertTrue(str(runtime).startswith(str(self.state)))


class OrderingTests(_TickCase):
    """Identity first, then resolution — and never the other way round."""

    def test_one_tick_leaves_exactly_one_runtime_home_and_it_is_the_pid(self) -> None:
        """The hazard: ``xo_dir(project_id)`` was resolved one line before
        ``fill_identity`` minted the pid. Resolving in that order would leave a
        folder-name home holding this tick's writes and a pid home holding
        every later tick's."""
        self.assertIsNone(self.pid(), "the scaffold must not mint a pid")
        self.tick()
        pid = self.pid()
        self.assertIsNotNone(pid)
        self.assertEqual(self.runtime_dirs(), [pid])

    def test_a_source_reading_before_the_first_fill_identity_finds_nothing(self) -> None:
        """``discovery`` runs inside ``poll_events()``, i.e. before any
        ``fill_identity`` this tick. On a brand-new project it therefore reads
        a runtime home whose pid does not exist yet — that must be an empty
        read that creates nothing, not an error and not a directory."""
        self.assertIsNone(self.pid())
        self.assertEqual(session_index.read_session_index(self.dirname), {})
        self.assertEqual(self.runtime_dirs(), [])

    def test_a_pre_mint_write_is_adopted_once_the_pid_exists(self) -> None:
        """The request-thread half of the same window: a chat started before
        the watcher's first tick publishes under the fallback key. Stranding
        that row would make the session invisible forever, with no error."""
        self.assertIsNone(self.pid())
        self.assertTrue(
            session_index.write_session_row(
                self.dirname, "claude:demo:web:aaaa",
                {"sessionId": "sess-1", "nativeSessionId": "native-1",
                 "backend": "claude_code", "updatedAt": 1},
            )
        )
        fallback = project_layout.xo_runtime_root() / self.dirname
        self.assertTrue(fallback.is_dir(), "the pre-mint write went somewhere else")

        self.tick()

        pid = self.pid()
        self.assertEqual(self.runtime_dirs(), [pid], "the pre-mint home was stranded")
        self.assertEqual(
            list(session_index.read_session_index(self.dirname)),
            ["claude:demo:web:aaaa"],
        )

    def test_adoption_never_overwrites_a_row_written_after_the_mint(self) -> None:
        """A file already under the pid home is the newer of the two."""
        session_index.write_session_row(
            self.dirname, "a:k", {"sessionId": "sess-1", "marker": "pre-mint"}
        )
        project_json.fill_identity(self.xo, self.dirname)
        session_index.write_session_row(
            self.dirname, "a:k", {"sessionId": "sess-1", "marker": "post-mint"}
        )
        # A second resolution runs the adoption again; it must be a no-op.
        project_layout._PREMINT_ADOPTED.clear()
        self.assertEqual(
            session_index.read_session_index(self.dirname)["a:k"]["marker"],
            "post-mint",
        )


class SkipRatherThanConjureTests(_TickCase):
    def test_a_project_that_does_not_exist_resolves_to_nothing(self) -> None:
        self.assertIsNone(project_layout.runtime_dir_for_project("no-such-project"))
        self.assertIsNone(
            project_layout.runtime_dir_for_project("no-such-project", create=True)
        )
        self.assertEqual(self.runtime_dirs(), [])
        self.assertFalse((self.root / "no-such-project").exists())

    def test_the_sink_batch_skips_a_project_whose_folder_vanished(self) -> None:
        """``fill_identity`` returns False when the folder is gone, so no pid is
        ever minted for it. The tick must skip rather than mkdir a home."""
        events = [
            SessionFirstSeen(
                ts=TS, project_id="vanished", native_session_id="native-9",
                runtime="claude_code", cwd="/tmp",
            )
        ]
        self.tick(events=events)
        self.assertNotIn("vanished", self.runtime_dirs())
        self.assertFalse((self.root / "vanished").exists())

    def test_reading_a_runtime_path_creates_nothing(self) -> None:
        self.assertIsNotNone(project_layout.runtime_dir_for_project(self.dirname))
        project_layout.runtime_read_path(self.dirname, "stats.json")
        session_index.read_session_index(self.dirname)
        self.assertEqual(self.runtime_dirs(), [])


class MigrationTests(_TickCase):
    """Read-through + copy-on-first-write, the shape the ``~/.xo-cowork`` →
    ``~/.quirq`` move already used three times. No startup mover, so nothing
    races the first tick."""

    def test_a_pre_move_stats_file_is_carried_into_the_runtime_home(self) -> None:
        (self.xo / "stats.json").write_text(
            json.dumps({
                "schema": 1,
                "_session_totals": {
                    "native-0": {
                        "runtime": "claude_code",
                        "tokens": {"input": 100, "output": 50},
                        "by_model_tokens": {}, "tools": {}, "files": [],
                        "duration_ms": 0, "first_ts": TS, "last_ts": TS,
                    }
                },
            }),
            encoding="utf-8",
        )
        self.tick()

        runtime = project_layout.runtime_dir_for_project(self.dirname)
        carried = json.loads((runtime / "stats.json").read_text(encoding="utf-8"))
        self.assertIn("native-0", carried["_session_totals"])
        self.assertIn("native-1", carried["_session_totals"])

    def test_the_pre_move_file_is_never_written_after_the_move(self) -> None:
        (self.xo / "stats.json").write_text(json.dumps({"schema": 1}), encoding="utf-8")
        before = (self.xo / "stats.json").read_bytes()
        self.tick()
        self.assertEqual((self.xo / "stats.json").read_bytes(), before)

    def test_timeline_history_is_dropped_not_merged(self) -> None:
        """Open decision O3, default applied. ``timeline.jsonl`` is append-only
        with rotation (``timeline.<stamp>.jsonl``, five kept), so a read-through
        would have to reconcile that glob across two roots on every read and
        every prune. The runtime timeline starts empty."""
        (self.xo / "timeline.jsonl").write_text(
            json.dumps({"ts": "2020-01-01T00:00:00Z", "type": "session.started"}) + "\n",
            encoding="utf-8",
        )
        self.tick()
        runtime = project_layout.runtime_dir_for_project(self.dirname)
        lines = [
            json.loads(line)
            for line in (runtime / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(lines)
        self.assertNotIn("2020-01-01T00:00:00Z", [line["ts"] for line in lines])


class ScaffoldTests(_TickCase):
    def test_the_scaffold_creates_no_runtime_file_and_no_runtime_home(self) -> None:
        """The template lost the four runtime documents, and
        ``scaffold_project`` lost the line that re-created
        ``sessions/sessionslist.json`` after the template copy — which would
        have undone the deletion on every new project."""
        for name in MOVED:
            with self.subTest(name=name):
                self.assertFalse((self.xo / name).exists())
        self.assertEqual(self.runtime_dirs(), [])

    def test_scaffolding_twice_is_still_idempotent(self) -> None:
        before = self.project_tree()
        project_layout.scaffold_project(self.PROJECT)
        self.assertEqual(self.project_tree(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
