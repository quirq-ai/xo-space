"""Space's data files live in the workspace ``.xo`` directory.

``space.json``, ``dashboard.json`` and ``sessions.json`` used to exist only as
route responses under ``/space/data/``, rebuilt per request behind a 30s
in-process cache: nothing on disk, nothing shared between processes, gone on
restart. They are files in ``<XO root>/.xo/`` now — one location that holds
everything — materialised by the watcher and served at ``/xo/*.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer.workspace import views


ROOT = Path(__file__).resolve().parents[1]


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
    def test_each_view_is_its_own_file_in_dot_xo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}, clear=False):
                views._last_build = 0.0
                views.apply(force=True)
                xo = root / ".xo"
                names = sorted(p.name for p in xo.glob("*.json"))

        # separate files, separate schemas: a reader after the session
        # telemetry does not parse the 168 KB graph to reach it
        for expected in ("space.json", "dashboard.json", "sessions.json"):
            self.assertIn(expected, names)

    def test_scaffold_creates_the_files_before_the_first_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}, clear=False):
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
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}, clear=False):
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
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}, clear=False):
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
                    "XO_PROJECTS_ROOT": str(root),
                    "XO_VIEWS_REFRESH_S": "3600",
                    "HOME": tmp,
                },
                clear=False,
            ):
                views._last_build = 0.0
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
