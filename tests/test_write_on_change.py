"""T26 — the six unconditional writers only write when something changed.

Before this task an idle watcher rewrote **N + 5 files every second**: one
``activity.json`` per project (``sinks/activity.py``) plus the five
once-per-tick workspace documents (``workspace/{projects_json,stats,activity,
sessionslist,sessions_augment}.py``). Nothing had changed on any of those
ticks; the documents were byte-identical but for a ``updated_at`` stamp the
writers minted themselves.

The acceptance criterion (docs/syncplan.md §10, T26) is a whole-system one,
so the headline test is a whole-system test: build a temp workspace, run a
real ``Watcher.tick()`` several times with no activity at all, and count the
``os.replace`` calls. The answer must be **exactly one per tick, and it must
be the heartbeat.**

The heartbeat is the other half of the criterion. ``activity.json``'s
freshness used to be the only way to tell a live watcher from a dead one, and
write-on-change is precisely what destroys that signal — which is why T22 had
to land first. So these tests assert not only that the six go quiet but that
``~/.quirq/watcher/heartbeat.json`` keeps beating on every single tick.

Three properties below are about the *baseline* rather than the comparison,
and each one is a way write-on-change fails silently — by never writing again:

* a **restart** must not rewrite an identical file (the baseline comes off
  disk when this process has none yet);
* a **deleted** file must come back on the next tick, not on the next content
  change — ``rm -rf ~/.quirq`` is a documented clean reset (syncplan §4);
* a **root switch** must not answer from the old root's baseline, which is
  why every baseline here is keyed by target path. ``XO_PROJECTS_ROOT`` and
  ``QUIRQ_STATE_ROOT`` are re-read on every call, so this is one
  ``patch.dict`` away in any test and one env change away in production.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from services.cowork_agent.engine import sessions_io
from services.cowork_agent.project_layout import (
    runtime_dir_for_project,
    workspace_runtime_dir,
    workspace_sessions_dir,
)
from services.cowork_agent.visualizer import state, watcher
from services.cowork_agent.visualizer.sinks import activity as activity_sink
from services.cowork_agent.visualizer.workspace import activity as ws_activity
from services.cowork_agent.visualizer.workspace import projects_json
from services.cowork_agent.visualizer.workspace import (
    sessions_augment as ws_sessions_augment,
)
from services.cowork_agent.visualizer.workspace import sessionslist as ws_sessionslist
from services.cowork_agent.visualizer.workspace import stats as ws_stats
from services.cowork_agent.visualizer.workspace_index import list_project_ids

PROJECTS = ("alpha", "beta", "gamma")

#: Every module T26 touched, so a helper that resets one resets them all.
_GATED_MODULES = (
    activity_sink,
    projects_json,
    ws_stats,
    ws_activity,
    ws_sessionslist,
    ws_sessions_augment,
)


def _reset_all() -> None:
    for module in _GATED_MODULES:
        module.reset_caches()


def _seed_project(root: Path, name: str) -> None:
    (root / name / ".xo").mkdir(parents=True, exist_ok=True)
    (root / name / ".xo" / "project.json").write_text(
        json.dumps({"schema": 1, "name": name}), encoding="utf-8"
    )


def _seed_workspace(tmp: Path, names=PROJECTS) -> dict[str, str]:
    """A projects root plus a state root, and the env that selects them."""
    root = tmp / "projects"
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        _seed_project(root, name)
    return {
        "XO_PROJECTS_ROOT": str(root),
        "QUIRQ_STATE_ROOT": str(tmp / ".quirq"),
    }


class _ReplaceSpy:
    """Counts every atomic write in the process, by destination.

    ``os.replace`` is the last instruction of every atomic write in this
    codebase (``visualizer/atomic_write.py``), so patching the attribute on
    the ``os`` module catches writes from modules this test does not import
    and does not know about — which is the point. A seventh unconditional
    writer added later shows up here as a failure rather than as another
    file rewritten every second forever.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []
        self._real = os.replace

    def __call__(self, src, dst):
        self.paths.append(str(dst))
        return self._real(src, dst)

    def patch(self):
        return patch.object(os, "replace", self)


class IdleWatcherWritesOnlyTheHeartbeatTests(unittest.TestCase):
    """The T26 acceptance criterion, measured on real ticks."""

    def setUp(self) -> None:
        _reset_all()
        self.addCleanup(_reset_all)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        env = patch.dict(os.environ, _seed_workspace(self.tmp), clear=False)
        env.start()
        self.addCleanup(env.stop)
        # No adapter source: "idle" means no events and no presence, which
        # is exactly what a source-free watcher produces every tick.
        with patch.object(watcher, "try_load_capability", return_value=None):
            self.watcher = watcher.Watcher()

    def _tick(self) -> list[str]:
        spy = _ReplaceSpy()
        with spy.patch():
            self.watcher.tick()
        return spy.paths

    def test_the_cold_tick_writes_all_six_and_every_later_tick_writes_none(
        self,
    ) -> None:
        """The measurement, both halves.

        The first tick has to write — there is nothing on disk yet — and
        asserting *what* it writes is what proves the later assertion is
        about the six writers rather than about a workspace that was never
        populated in the first place.
        """
        first = self._tick()
        heartbeat = str(state.watcher_heartbeat_path())

        # N per-project snapshots …
        self.assertEqual(
            sorted(p for p in first if "/watcher/activity/projects/" in p),
            sorted(
                str(state.project_activity_path(name)) for name in PROJECTS
            ),
        )
        # … plus the five once-per-tick workspace documents.
        sessions_dir = workspace_sessions_dir()
        for target in (
            projects_json.path(),
            workspace_runtime_dir() / "stats.json",
            state.workspace_activity_path(),
            sessions_dir / "sessionslist.json",
            sessions_dir / "sessions-augment.json",
        ):
            self.assertIn(str(target), first, f"cold tick never wrote {target}")

        # Nothing happened in between. Several real ticks, and the only
        # file that moves is the heartbeat.
        for n in range(2, 7):
            written = self._tick()
            self.assertEqual(
                written, [heartbeat], f"tick {n} wrote more than the heartbeat"
            )

    def test_the_heartbeat_is_still_unconditional(self) -> None:
        """Write-on-change removed the liveness signal ``activity.json``'s
        freshness used to carry; the heartbeat is what replaces it, so it
        must move on a tick where nothing else does."""
        self._tick()
        beat_path = state.watcher_heartbeat_path()
        first = json.loads(beat_path.read_text(encoding="utf-8"))
        stamp = beat_path.stat().st_mtime_ns

        for _ in range(3):
            self._tick()

        last = json.loads(beat_path.read_text(encoding="utf-8"))
        self.assertEqual(first["tick_count"], 1)
        self.assertEqual(last["tick_count"], 4)
        self.assertNotEqual(beat_path.stat().st_mtime_ns, stamp)

    def test_the_stale_stamps_on_disk_are_the_deliberate_trade(self) -> None:
        """``updated_at`` stops tracking the tick — state it, so nobody
        reintroduces mtime freshness as a health check."""
        self._tick()
        activity_path = state.project_activity_path("alpha")
        before = activity_path.stat().st_mtime_ns
        for _ in range(3):
            self._tick()
        self.assertEqual(activity_path.stat().st_mtime_ns, before)
        # And the heartbeat is the reason that is acceptable.
        beat = json.loads(
            state.watcher_heartbeat_path().read_text(encoding="utf-8")
        )
        self.assertEqual(beat["tick_count"], 4)

    def test_a_real_change_still_lands_on_the_next_tick(self) -> None:
        """A gate that never writes again would pass every test above."""
        for _ in range(3):
            self._tick()
        _seed_project(Path(os.environ["XO_PROJECTS_ROOT"]), "delta")

        written = self._tick()
        self.assertIn(str(projects_json.path()), written)
        self.assertIn(str(state.project_activity_path("delta")), written)
        registry = json.loads(projects_json.path().read_text(encoding="utf-8"))
        self.assertIn("delta", registry["projects"])


class PerProjectActivitySinkTests(unittest.TestCase):
    """``sinks/activity.py`` — the ``N`` of the idle ``N + 5``."""

    ROWS = [
        {
            "session_id": "s-1",
            "runtime": "demo_runtime",
            "started_at_ms": 1_757_000_000_000,
            "updated_at_ms": 1_757_000_060_000,
        }
    ]
    MODELS = {"s-1": "model-a", "s-2": "model-b"}

    def setUp(self) -> None:
        _reset_all()
        self.addCleanup(_reset_all)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patcher = patch.object(
            activity_sink, "_resolve_user_id", return_value="local-user"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _apply(self, path: Path, rows=None) -> bool:
        return activity_sink.apply(
            path,
            self.ROWS if rows is None else rows,
            model_by_session=self.MODELS,
        )

    def test_an_unchanged_snapshot_is_written_once_and_never_again(self) -> None:
        path = self.tmp / "activity" / "alpha.json"
        self.assertTrue(self._apply(path))
        stamp = path.stat().st_mtime_ns
        for _ in range(5):
            self.assertFalse(self._apply(path))
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_a_session_appearing_or_leaving_is_a_change(self) -> None:
        path = self.tmp / "activity" / "alpha.json"
        self._apply(path)
        joined = self.ROWS + [
            {"session_id": "s-2", "runtime": "demo_runtime", "updated_at_ms": 1}
        ]
        self.assertTrue(self._apply(path, joined))
        self.assertFalse(self._apply(path, joined))
        self.assertTrue(self._apply(path, []))

    def test_each_project_has_its_own_baseline(self) -> None:
        """One shared baseline would let the first project written in a tick
        suppress every other project's identical snapshot."""
        alpha = self.tmp / "activity" / "alpha.json"
        beta = self.tmp / "activity" / "beta.json"
        self.assertTrue(self._apply(alpha))
        self.assertTrue(self._apply(beta))
        self.assertTrue(beta.is_file())
        self.assertEqual(
            json.loads(alpha.read_text(encoding="utf-8"))["open_sessions"],
            json.loads(beta.read_text(encoding="utf-8"))["open_sessions"],
        )

    def test_a_restart_does_not_rewrite_an_identical_file(self) -> None:
        path = self.tmp / "activity" / "alpha.json"
        self._apply(path)
        stamp = path.stat().st_mtime_ns
        activity_sink.reset_caches()  # as a fresh process
        self.assertFalse(self._apply(path))
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_a_deleted_file_comes_back_on_the_next_apply(self) -> None:
        """``rm -rf ~/.quirq`` is a documented clean reset (syncplan §4). An
        in-memory baseline alone would keep the file missing until the
        presence rows happened to change — possibly never."""
        path = self.tmp / "activity" / "alpha.json"
        self._apply(path)
        path.unlink()
        self.assertTrue(self._apply(path))
        self.assertTrue(path.is_file())

    def test_a_corrupt_file_is_repaired_rather_than_merged(self) -> None:
        """Full ownership: there is nothing in this document to preserve,
        so the declared behaviour (syncplan §3) is overwrite, not raise."""
        path = self.tmp / "activity" / "alpha.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ truncated", encoding="utf-8")
        self.assertTrue(self._apply(path))
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["schema"], 1
        )


class WorkspaceWriterTests(unittest.TestCase):
    """The five once-per-tick workspace documents, one table, one fixture."""

    def setUp(self) -> None:
        _reset_all()
        self.addCleanup(_reset_all)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        env = patch.dict(os.environ, _seed_workspace(self.tmp), clear=False)
        env.start()
        self.addCleanup(env.stop)
        self._seed_inputs()

    # ── fixture ──────────────────────────────────────────────────────────

    def _seed_inputs(self, *, bump: int = 0) -> None:
        """Give every writer something non-trivial to aggregate."""
        for name in PROJECTS:
            runtime = runtime_dir_for_project(name, create=True)
            self.assertIsNotNone(runtime)
            (runtime / "stats.json").write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "updated_at": "2026-09-07T12:00:00Z",
                        "rolling": {
                            "7d": {
                                "tokens": {"input": 10 + bump, "output": 2},
                                "by_model": {},
                                "by_tool": {},
                                "files_edited": 0,
                                "sessions": 1,
                                "active_minutes": 3,
                            }
                        },
                        "by_session": {f"{name}-s1": {"tokens": 12}},
                        "by_runtime": {},
                        "by_day": {},
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "sessions").mkdir(parents=True, exist_ok=True)
            (runtime / "sessions" / "sessions-augment.json").write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "updated_at": "2026-09-07T12:00:00Z",
                        "sessions": {f"{name}-s1": {"title": f"t{bump}"}},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = state.project_activity_path(name)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "updated_at": "2026-09-07T12:00:00Z",
                        "open_sessions": [
                            {
                                "session_id": f"{name}-s1",
                                "runtime": "demo_runtime",
                                "agent": f"model-{bump}",
                                "user_id": "local-user",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            sessions_io.write_session_row(
                name,
                f"{name}-s1-0000abcd",
                {"id": f"{name}-s1", "directory": str(self.tmp / "projects" / name),
                 "title": f"t{bump}"},
            )

    def _writers(self) -> list[tuple[str, object, Callable[[], Path]]]:
        sessions_dir = workspace_sessions_dir()
        return [
            ("projects_json", projects_json, lambda: projects_json.path()),
            ("stats", ws_stats, lambda: workspace_runtime_dir() / "stats.json"),
            ("activity", ws_activity, state.workspace_activity_path),
            (
                "sessionslist",
                ws_sessionslist,
                lambda: sessions_dir / "sessionslist.json",
            ),
            (
                "sessions_augment",
                ws_sessions_augment,
                lambda: sessions_dir / "sessions-augment.json",
            ),
        ]

    # ── the properties, once per writer ──────────────────────────────────

    def test_an_unchanged_workspace_is_written_once_and_never_again(self) -> None:
        ids = list_project_ids()
        for label, module, target in self._writers():
            with self.subTest(writer=label):
                self.assertTrue(module.apply(ids), "cold apply must write")
                stamp = target().stat().st_mtime_ns
                for _ in range(5):
                    self.assertFalse(module.apply(ids))
                self.assertEqual(target().stat().st_mtime_ns, stamp)

    def test_a_change_in_the_inputs_still_lands(self) -> None:
        """The gate must not be a permanent mute."""
        ids = list_project_ids()
        for _, module, _ in self._writers():
            module.apply(ids)
        _seed_project(Path(os.environ["XO_PROJECTS_ROOT"]), "delta")
        self._seed_inputs(bump=1)
        ids = list_project_ids()
        for label, module, _ in self._writers():
            with self.subTest(writer=label):
                self.assertTrue(module.apply(ids))

    def test_a_restart_does_not_rewrite_an_identical_file(self) -> None:
        ids = list_project_ids()
        for label, module, target in self._writers():
            with self.subTest(writer=label):
                module.apply(ids)
                stamp = target().stat().st_mtime_ns
                module.reset_caches()  # as a fresh process
                self.assertFalse(module.apply(ids))
                self.assertEqual(target().stat().st_mtime_ns, stamp)

    def test_a_deleted_file_comes_back_on_the_next_apply(self) -> None:
        ids = list_project_ids()
        for label, module, target in self._writers():
            with self.subTest(writer=label):
                module.apply(ids)
                target().unlink()
                self.assertTrue(module.apply(ids))
                self.assertTrue(target().is_file())

    def test_a_baseline_never_answers_for_a_different_root(self) -> None:
        """The baselines are keyed by target path because both roots are
        re-read from the environment on every call. A single global would
        let an identical workspace under a *second* root skip its very
        first write — a file that never gets created at all.

        ``projects_json`` is excluded on purpose: it records
        ``projects_root`` in its payload, so its document can never be
        identical across two roots and the property is unobservable there.
        """
        ids = list_project_ids()
        writers = [w for w in self._writers() if w[0] != "projects_json"]
        for _, module, _ in writers:
            module.apply(ids)

        second = Path(self._tmp.name) / "second"
        second.mkdir()
        with patch.dict(os.environ, _seed_workspace(second), clear=False):
            self._seed_inputs()  # byte-identical inputs under the new root
            ids = list_project_ids()
            for label, module, target in writers:
                with self.subTest(writer=label):
                    self.assertTrue(module.apply(ids))
                    self.assertTrue(target().is_file())


class NoUngatedWriterRemainsTests(unittest.TestCase):
    """A source guard: T26's six may not reach for the raw writer again."""

    FILES = (
        "services/cowork_agent/visualizer/sinks/activity.py",
        "services/cowork_agent/visualizer/workspace/projects_json.py",
        "services/cowork_agent/visualizer/workspace/stats.py",
        "services/cowork_agent/visualizer/workspace/activity.py",
        "services/cowork_agent/visualizer/workspace/sessionslist.py",
        "services/cowork_agent/visualizer/workspace/sessions_augment.py",
    )

    def test_none_of_the_six_calls_write_json_atomic_directly(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in self.FILES:
            with self.subTest(module=relative):
                source = (root / relative).read_text(encoding="utf-8")
                self.assertNotIn(
                    "write_json_atomic(",
                    source,
                    "an ungated write is a file rewritten every second",
                )
                self.assertIn("write_json_atomic_if_changed(", source)

    def test_every_one_of_the_six_can_drop_its_baseline(self) -> None:
        """``reset_caches`` is how a test — and a process whose root moved —
        gets back to a known state."""
        for module in _GATED_MODULES:
            with self.subTest(module=module.__name__):
                self.assertTrue(callable(getattr(module, "reset_caches", None)))


if __name__ == "__main__":
    unittest.main()
