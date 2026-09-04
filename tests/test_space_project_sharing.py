from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class SpaceProjectSharingCompositionTests(unittest.TestCase):
    """The sharing surfaces are composed into the Files tab (projects view)
    through explicit seams: a PANELS entry, two HTML blocks in render(), the
    panel bind hook, and a stylesheet link. These assertions pin those seams
    so a refactor of projects.js cannot silently drop a surface."""

    def test_sharing_module_is_imported_with_a_cache_buster(self) -> None:
        projects = read("js/views/projects.js")
        m = re.search(r"from '\./projects_sharing\.js\?v=([\w-]+)'", projects)
        self.assertIsNotNone(m, "projects.js must import projects_sharing.js with ?v=")
        app = read("js/app.js")
        self.assertIn("./views/projects.js?v=20260904-sharing1", app)

    def test_panel_is_registered_and_bind_hook_is_called(self) -> None:
        projects = read("js/views/projects.js")
        self.assertIn("sharingPanel,", projects)
        self.assertIn("pn.bind(el,id)", projects)
        # a failed fetch must never wire controls onto an error message
        self.assertIn("if(res.ok&&typeof pn.bind==='function')", projects)

    def test_strip_and_inbox_render_in_both_paint_paths(self) -> None:
        projects = read("js/views/projects.js")
        self.assertIn("+sharingStripHTML()", projects)
        self.assertEqual(projects.count("sharedWithYouHTML()"), 3)  # render, renderRows, refreshSharingUI
        self.assertIn("startSharingPoll(refreshSharingUI)", projects)

    def test_module_talks_only_to_the_bff_routes(self) -> None:
        mod = read("js/views/projects_sharing.js")
        for path in (
            "/api/project-sharing/status",
            "/members",
            "/share",
            "/revoke",
            "/commits?limit=5",
        ):
            self.assertIn(path, mod)
        self.assertNotIn("/commits/poll", mod)  # the browser never talks to swarm

    def test_clone_command_targets_the_reported_projects_root(self) -> None:
        mod = read("js/views/projects_sharing.js")
        self.assertIn("status.projects_root", mod)
        # the literal default is only a fallback for a missing snapshot
        self.assertIn("||'~/xo-projects'", mod)

    def test_relay_status_is_the_only_source_of_shared(self) -> None:
        mod = read("js/views/projects_sharing.js")
        self.assertIn("if(memberState(id)==='live')fillMembers(id);", mod)
        for state in ("unknown", "disabled", "solo", "live"):
            self.assertIn("'" + state + "'", mod)

    def test_stylesheet_is_linked(self) -> None:
        html = read("index.html")
        self.assertIn('href="css/sharing.css?v=', html)
        css = read("css/sharing.css")
        for cls in (".shr-strip", ".shr-inbox", ".shr-panel", ".tchip.st-shared", ".tchip.st-available"):
            self.assertIn(cls, css)
