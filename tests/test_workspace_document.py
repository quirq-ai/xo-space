"""Space's data files are real files, materialised by the watcher.

``space.json``, ``dashboard.json`` and ``sessions.json`` used to exist only as
route responses under ``/space/data/``, rebuilt per request behind a 30s
in-process cache: nothing on disk, nothing shared between processes, gone on
restart. They are files now, materialised by the watcher and served at
``/xo/*.json``.

Since syncplan T14 the view name and the file name are different things. The
graph is written to ``~/.quirq/workspace/graph.json`` — it is derived state,
and ``<XO root>/.xo/space.json`` is the durable Space record, which could not
survive sharing a path with a file two unlocked writers rebuild. The route
still answers ``GET /xo/space.json`` with the graph.

T20 finished that move for the other two. ``dashboard.json`` and
``sessions.json`` are derived from the same walk, so all three views now live
under ``~/.quirq/workspace/`` and ``<XO root>/.xo/`` is left holding only the
documents a clone would want. The pre-T20 copies are swept, because a stale
derived file in the synced tier is machine-local telemetry that T21 would
force-include in the backup tarball.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.workspace import views


ROOT = Path(__file__).resolve().parents[1]


def _env(tmp: str) -> dict:
    """Both roots, always. The graph now lands under the state root, so a
    test that pins only ``XO_PROJECTS_ROOT`` writes into the developer's
    real ``~/.quirq``."""
    return {
        "XO_PROJECTS_ROOT": str(Path(tmp) / "projects"),
        "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
    }


def _workspace(tmp: str) -> Path:
    root = Path(tmp) / "projects"
    for name in ("alpha", "beta"):
        (root / name / ".xo").mkdir(parents=True)
        (root / name / ".xo" / "project.json").write_text(
            json.dumps({"schema": 1, "name": name}), encoding="utf-8"
        )
        (root / name / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


class WorkspaceViewFileTests(unittest.TestCase):
    def test_each_view_is_its_own_file_in_the_runtime_tier(self) -> None:
        """Separate files, separate schemas — a reader after the session
        telemetry does not parse the 168 KB graph to reach it — and all three
        of them under ``~/.quirq/workspace/`` since T20.

        The ``<XO root>/.xo/`` glob is the other half of the claim: the views
        used to land there, and after the move the synced tier must be left
        holding nothing this sink writes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views._last_build = 0.0
                views.apply(force=True)
                runtime = views.view_path("dashboard").parent
                names = sorted(p.name for p in runtime.glob("*.json"))
                synced = sorted(p.name for p in (root / ".xo").glob("*.json"))
                graph = views.graph_path()
                graph_written = graph.is_file()   # before the tmp dir goes

        for expected in ("dashboard.json", "sessions.json"):
            self.assertIn(expected, names)

        # T14: the graph is derived, so it lives in the runtime tier and
        # leaves <XO root>/.xo/space.json for the Space record. T20 moved the
        # other two files to sit beside it.
        self.assertEqual(runtime, Path(tmp) / ".quirq" / "workspace")
        self.assertEqual(graph, runtime / "graph.json")
        self.assertTrue(graph_written)
        self.assertIn("graph.json", names)
        self.assertNotIn("space.json", names)

        # T20: none of the three views is written to the synced tier any more.
        for gone in ("space.json", "dashboard.json", "sessions.json"):
            self.assertNotIn(gone, synced)

    def test_the_view_files_all_live_under_the_workspace_runtime_dir(self) -> None:
        """``view_path`` is the one place that knows where a view lives, and
        every answer it gives is now in the runtime tier."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                runtime = project_layout.workspace_runtime_dir()
                self.assertEqual(runtime, Path(tmp) / ".quirq" / "workspace")
                for name in views.VIEWS:
                    self.assertEqual(views.view_path(name).parent, runtime)
                self.assertEqual(
                    project_layout.workspace_sessions_dir(), runtime / "sessions"
                )

    def test_scaffold_creates_the_files_before_the_first_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                for name in views.VIEWS:
                    self.assertTrue(views.view_path(name).is_file())
                # a placeholder reads as missing, so a route rebuilds it
                payload, _age = views.read("space")
                self.assertIsNone(payload)

    def test_a_failed_builder_leaves_the_previous_file(self) -> None:
        """Stale beats absent: a route can say how old a file is, it cannot
        invent one. The old route cache stored only successes, so an expired
        cache plus a broken builder served a 503."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views._last_build = 0.0
                views.apply(force=True)
                good, _ = views.read("space")
                self.assertIsNotNone(good)

                with patch(
                    "services.cowork_agent.visualizer.space_index.build_space_data",
                    side_effect=RuntimeError("scan exploded"),
                ):
                    views.apply(force=True)
                after, _ = views.read("space")

        self.assertEqual(after, good)

    def test_read_reports_staleness_so_the_route_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views._last_build = 0.0
                views.apply(force=True)
                fresh, age = views.read("space", max_age_s=3600)
                self.assertIsNotNone(fresh)
                self.assertIsNotNone(age)
                stale, _ = views.read("space", max_age_s=-1)
                self.assertIsNone(stale)

    def test_the_expensive_sink_self_throttles(self) -> None:
        """The watcher ticks every second; these views walk every mapped file
        in the workspace, so they must not rebuild on every tick.

        The window is pinned wide and HOME is pointed at the temp dir:
        ``apply()`` stamps ``_last_build`` before it builds, and the sessions
        view scans the session stores under the real ``$HOME``, so on a
        developer machine with a large ``~/.claude`` the first build could
        outlast the default 30 s window and make the second call due again —
        a wall-clock flake, and a hermeticity leak besides."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(
                os.environ,
                {
                    **_env(tmp),
                    "XO_VIEWS_REFRESH_S": "3600",
                    "HOME": tmp,
                },
                clear=False,
            ):
                # Relative to now, not 0.0: time.monotonic() is seconds since
                # boot on Linux/macOS, so on a freshly booted machine (a CI
                # runner) "now - 0.0" can be INSIDE the pinned window and the
                # first apply() gets throttled before it ever builds.
                views._last_build = time.monotonic() - 7200.0
                self.assertTrue(views.apply())     # first tick builds
                self.assertFalse(views.apply())    # second is not due

    def test_both_projections_come_from_one_scan(self) -> None:
        """dashboard.json used to call build_space_data() itself, so opening
        Dashboard and Graph paid for two full workspace walks."""
        source = (
            ROOT / "services" / "cowork_agent" / "visualizer" / "workspace"
            / "views.py"
        ).read_text(encoding="utf-8")
        self.assertIn("build_categorized_graph(source=space)", source)


class AbandonedWorkspaceStateTests(unittest.TestCase):
    """T20's other half: what the move leaves behind in the synced tier.

    A stale derived file in ``<XO root>/.xo/`` is not cosmetic. That directory
    is the synced tier — T21 force-includes it in the backup tarball — so a
    ``timeline.jsonl`` or session index abandoned there is this machine's
    telemetry travelling to every other machine and every restore, which is
    exactly what R-TIER exists to prevent. A frozen ``dashboard.json`` sitting
    where a reader used to look is the other failure: silent, and indefinite.

    Snapshots are deleted; **history is moved**. ``timeline.jsonl`` is
    append-only and is fed only by the live watcher tick, so unlinking it would
    drop every event recorded before the upgrade with nothing able to rebuild
    them — the project tier relocates the same file (T21) and the workspace
    tier now matches it.
    """

    def _dot_xo(self, tmp: str) -> Path:
        root = Path(tmp) / "projects" / ".xo"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _pre_t20_tree(self, tmp: str) -> Path:
        """Everything a pre-T20 build wrote into the workspace ``.xo/``,
        alongside the three records that stay."""
        xo = self._dot_xo(tmp)
        for name in ("dashboard.json", "sessions.json", "stats.json",
                     "space.json", "projects.json", "xo.json"):
            (xo / name).write_text(json.dumps({"schema": 1}), encoding="utf-8")
        (xo / "activity.json").write_text(
            json.dumps({"schema": 1, "open_sessions": []}), encoding="utf-8"
        )
        (xo / "timeline.jsonl").write_text("{}\n", encoding="utf-8")
        # sinks/timeline.py rotates to timeline.<stamp>.jsonl and keeps 5
        (xo / "timeline.20260825T120000Z.jsonl").write_text("{}\n", encoding="utf-8")
        (xo / "sessions").mkdir(exist_ok=True)
        (xo / "sessions" / "sessionslist.json").write_text("{}", encoding="utf-8")
        (xo / "sessions" / "sessions-augment.json").write_text(
            json.dumps({"schema": 2, "sessions": {}}), encoding="utf-8"
        )
        return xo

    def test_the_sweep_leaves_only_the_three_synced_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            xo = self._pre_t20_tree(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                removed = views.sweep_abandoned(force=True)
                left = sorted(p.name for p in xo.iterdir())

        self.assertEqual(left, ["projects.json", "space.json", "xo.json"])
        # activity.json is named by T20 explicitly: nothing has written it
        # since workspace_activity_path() moved to ~/.quirq/watcher/activity/.
        self.assertIn("activity.json", removed)
        self.assertIn("sessions/", removed)
        # Moved, not unlinked — reported as such so a reader of the log can
        # tell the two outcomes apart.
        self.assertIn("timeline.20260825T120000Z.jsonl -> runtime tier", removed)
        self.assertIn("timeline.jsonl -> runtime tier", removed)

    def test_a_tick_sweeps_and_the_records_survive_it(self) -> None:
        """``apply`` is the watcher's entry point, so the sweep has to happen
        there — and it must not touch ``space.json``, which since T14 is the
        durable Space *record* sharing a directory with the views' old home."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            xo = self._pre_t20_tree(tmp)
            (xo / "space.json").write_text(
                json.dumps({"schema": 2, "pid": "keep-me"}), encoding="utf-8"
            )
            with patch.dict(os.environ, _env(tmp), clear=False):
                views._SWEPT.discard(str(xo))
                views._last_build = 0.0
                views.apply(force=True)
                left = sorted(p.name for p in xo.iterdir())
                record = json.loads((xo / "space.json").read_text(encoding="utf-8"))

        self.assertEqual(left, ["projects.json", "space.json", "xo.json"])
        self.assertEqual(record["pid"], "keep-me")

    def test_the_sweep_moves_history_instead_of_dropping_it(self) -> None:
        """The regression this guards: ``timeline.jsonl`` was swept with the
        snapshots. Nothing rebuilds the workspace timeline — the watcher only
        appends events as they happen — so deleting it silently truncated
        every pre-upgrade event out of ``GET /api/xo-projects/timeline``."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            xo = self._pre_t20_tree(tmp)
            (xo / "timeline.jsonl").write_text(
                json.dumps({"ts": "2026-01-01T00:00:00Z", "type": "session.first_seen"})
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, _env(tmp), clear=False):
                views._SWEPT.discard(str(xo))
                views.sweep_abandoned(force=True)
                moved = views.workspace_runtime_dir() / "timeline.jsonl"
                self.assertTrue(moved.is_file(), "history was dropped, not moved")
                self.assertIn("2026-01-01T00:00:00Z", moved.read_text(encoding="utf-8"))
                self.assertFalse((xo / "timeline.jsonl").exists())

                # Destination wins, exactly as the project-tier migration does:
                # a stale copy reappearing must not overwrite the newer log.
                (xo / "timeline.jsonl").write_text(
                    json.dumps({"ts": "stale", "type": "x"}) + "\n", encoding="utf-8"
                )
                views.sweep_abandoned(force=True)
                self.assertNotIn("stale", moved.read_text(encoding="utf-8"))
                self.assertFalse((xo / "timeline.jsonl").exists())

    def test_the_sweep_is_idempotent_and_runs_once_per_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            xo = self._pre_t20_tree(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views._SWEPT.discard(str(xo))
                first = views.sweep_abandoned()
                second = views.sweep_abandoned()
                third = views.sweep_abandoned(force=True)

        self.assertTrue(first)
        self.assertEqual(second, [])   # remembered, so not even stat'ed again
        self.assertEqual(third, [])    # forced, but there is nothing left

    def test_a_missing_workspace_dir_is_not_remembered_as_swept(self) -> None:
        """A fresh install has no ``<XO root>/.xo/`` at all. Marking that root
        done would mean a restore that drops a pre-T20 tree there later in the
        same process is never cleaned."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                self.assertEqual(views.sweep_abandoned(), [])
                xo = self._pre_t20_tree(tmp)
                self.assertIn("dashboard.json", views.sweep_abandoned())
                left = sorted(p.name for p in xo.iterdir())

        self.assertEqual(left, ["projects.json", "space.json", "xo.json"])


class XoDataRouteTests(unittest.TestCase):
    def test_routes_serve_the_files_and_never_scan_in_the_event_loop(self) -> None:
        router = (ROOT / "routers" / "xo_data.py").read_text(encoding="utf-8")

        for name in ("space", "dashboard", "sessions"):
            self.assertIn(f'@router.get("/{name}.json")', router)
        self.assertIn('APIRouter(prefix="/xo"', router)
        self.assertIn("views.read", router)
        self.assertIn("asyncio.to_thread(views.build", router)
        # an allowlist, not a static mount of the whole state directory
        self.assertNotIn("StaticFiles", router)

        server = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("xo_data_router", server)

        # the generated endpoints are gone from /space/data
        space_router = (ROOT / "routers" / "space.py").read_text(encoding="utf-8")
        for name in ("space", "dashboard", "sessions"):
            self.assertNotIn(f'@router.get("/data/{name}.json")', space_router)
        # session_prompts is a per-session lookup, not a workspace file
        self.assertIn('@router.get("/data/session_prompts.json")', space_router)

    def test_the_ui_loads_from_xo_not_from_space_data(self) -> None:
        for rel in (
            "space_ui/js/views/atlas.js",
            "space_ui/js/views/tree.js",
            "space_ui/js/views/sessions.js",
            "space_ui/js/core/workspace.js",
        ):
            source = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("/xo/", source, rel)
            # the three workspace payloads no longer come from /space/data.
            # session_prompts.json stays there on purpose: it is a
            # per-session lookup with query parameters, not a workspace file.
            for gone in ("data/space.json", "data/dashboard.json",
                         "data/sessions.json"):
                self.assertNotIn(f"apiFetch('{gone}", source, rel)
                self.assertNotIn(f"url:'{gone}", source, rel)


if __name__ == "__main__":
    unittest.main()
