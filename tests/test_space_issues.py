"""The Files drawer's Issues panel — the GitHub issue mirror, on screen.

The backend already had every piece: services/cowork_agent/github_poller.py
polls each project with a github.com remote, visualizer/github_mirror.py
writes ~/.quirq/projects/<id>/github/issues.json, and
GET /api/xo-projects/{id}/github/issues serves it with a `state` field that
says which empty an empty answer is. Nothing rendered it. These tests pin the
UI side of that contract the way test_space_wiki.py pins the rest of Space:
by reading the source, because the panel is plain ES modules with no bundler
and no DOM to drive here.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROJECTS_JS = ROOT / "space_ui" / "js" / "views" / "projects.js"
PROJECTS_CSS = ROOT / "space_ui" / "css" / "projects.css"
INDEX_HTML = ROOT / "space_ui" / "index.html"
APP_JS = ROOT / "space_ui" / "js" / "app.js"
WIKI_JS = ROOT / "space_ui" / "js" / "views" / "wiki.js"
MODELS_PY = ROOT / "routers" / "cowork_agent" / "bff" / "_visualizer_models.py"
VISUALIZER_PY = ROOT / "routers" / "cowork_agent" / "bff" / "visualizer.py"


class IssuesPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projects = PROJECTS_JS.read_text(encoding="utf-8")

    def test_panel_reads_the_mirror_endpoint(self) -> None:
        """One more drawer panel, wired the way the other four are."""
        self.assertIn("key:'issues'", self.projects)
        self.assertIn("/github/issues", self.projects)
        # it is a PANELS entry, so it inherits the drawer's per-panel fetch
        # and per-panel failure — not a fifth barrier on the row grid
        panels = self.projects.split("const PANELS=[")[1].split("];")[0]
        self.assertIn("render:rIssues", panels)
        self.assertIn("bind:bindIssues", panels)

    def test_every_empty_state_the_endpoint_can_report_is_rendered(self) -> None:
        """`state` exists so the panel can say WHY it is showing nothing; a
        single grey "no issues" would throw that away."""
        literal = MODELS_PY.read_text(encoding="utf-8").split(
            "class GithubIssuesResponse"
        )[1].split("class ")[0]
        # the wire vocabulary, straight from the response model
        for state in ("ok", "empty", "never_polled", "issues_disabled",
                      "no_remote", "error"):
            self.assertIn(f'"{state}"', literal)
            if state != "ok":
                self.assertIn(state, self.projects)
        # each non-ok state carries its own sentence, not a shared one
        empties = self.projects.split("const ISS_EMPTY={")[1].split("};")[0]
        for state in ("no_remote", "never_polled", "issues_disabled", "empty"):
            self.assertIn(state, empties)
        self.assertIn("Last poll failed:", self.projects)

    def test_filter_and_search_never_refetch(self) -> None:
        """One response holds every mirror row, so narrowing is a repaint.
        A refetching filter would be slower AND would spend GitHub budget to
        answer a question already on screen."""
        bind = self.projects.split("function bindIssues")[1].split("\nconst PANELS")[0]
        self.assertIn("repaint", bind)
        # only Refresh may reach the network
        self.assertIn("issRefresh.add(id)", bind)
        self.assertEqual(bind.count("fillPanel("), 1)
        # the list is repainted, never the head: rebuilding the head would
        # destroy the filter input mid-keystroke (renderRows' lesson)
        self.assertIn(".iss-list", bind)
        self.assertNotIn("iss-head", bind)

    def test_refresh_is_a_one_shot_flag(self) -> None:
        """Every other fetch of this panel reads the mirror for free."""
        self.assertIn("const issRefresh=new Set()", self.projects)
        self.assertIn("issRefresh.delete(id)", self.projects)
        self.assertIn("refresh=1", self.projects)

    def test_a_bad_mirror_row_cannot_become_a_link(self) -> None:
        """Issue text is GitHub's, not ours. Titles and labels are escaped
        like everything else, and the href is only rendered for a real https
        URL — so a javascript: url degrades to plain text."""
        row = self.projects.split("function issRow(it){")[1].split("\nlet issDeb")[0]
        self.assertIn("/^https:\\/\\//i.test(String(it.url||''))", row)
        self.assertIn('rel="noopener noreferrer"', row)
        self.assertIn("esc(it.title", row)
        self.assertIn("esc(l)", row)
        # escaping once at the source and again at a use site is how &amp;lt;
        # ends up on screen
        self.assertNotIn("esc(who)", row.split("const who=")[1].split("\n")[0])

    def test_labels_absent_is_not_labels_empty(self) -> None:
        """The poller does not fetch labels, so it writes null — and an empty
        list would be the claim that the issue has none."""
        models = MODELS_PY.read_text(encoding="utf-8")
        self.assertIn("labels: Optional[list[str]] = None", models)
        self.assertIn("(it.labels||[])", self.projects)

    def test_the_live_row_keeps_the_accent(self) -> None:
        """in_progress is the only thing in this panel that is happening right
        now, so it is the only thing that gets the accent chip."""
        row = self.projects.split("function issRow(it){")[1]
        self.assertIn("st-in_progress", row)
        self.assertIn("in progress", self.projects)
        self.assertIn("tracked", self.projects)

    def test_closed_says_why_it_may_be_empty(self) -> None:
        """The poller asks for OPEN issues only; a closed row exists only for
        an issue Space watched close. "None" must not read as "this repo has
        no closed issues"."""
        self.assertIn("watches it close", self.projects)
        poller = (ROOT / "services" / "cowork_agent" / "connectors" / "github"
                  / "issues.py").read_text(encoding="utf-8")
        self.assertIn("states: [OPEN]", poller)

    def test_panel_width_is_a_property_not_a_key_check(self) -> None:
        """Two wide panels now, so the drawer asks the panel, not its name."""
        self.assertIn("pn.wide?' prj-panel-wide':''", self.projects)
        self.assertNotIn("pn.key==='files'?' prj-panel-wide'", self.projects)
        self.assertIn("pn.skel||1", self.projects)

    def test_rows_are_links_out_and_styled_as_links(self) -> None:
        css = PROJECTS_CSS.read_text(encoding="utf-8")
        self.assertIn(".iss-row", css)
        self.assertIn("a.iss-row:hover", css)
        self.assertIn("a.iss-row:focus-visible", css)
        # the state dot, not a coloured row, carries open vs closed
        self.assertIn(".iss-dot", css)
        self.assertIn(".iss-row.is-closed .iss-dot", css)


class IssuesDocsTests(unittest.TestCase):
    def test_the_wiki_files_guide_describes_the_panel(self) -> None:
        wiki = WIKI_JS.read_text(encoding="utf-8")
        files = wiki.split("files:{")[1].split("timeline:{")[0]
        self.assertIn("Issues panel", files)
        self.assertIn("/github/issues", files)
        # the two facts a reader gets wrong without being told
        self.assertIn("mirror", files)
        self.assertIn("Refresh", files)

    def test_cache_stamps_were_bumped_for_this_change(self) -> None:
        """A stale stamp ships the new markup against the old stylesheet."""
        index = INDEX_HTML.read_text(encoding="utf-8")
        app = APP_JS.read_text(encoding="utf-8")
        self.assertNotIn("css/projects.css?v=20260824-treecam1", index)
        self.assertNotIn("views/projects.js?v=20260910-sharingpane1", app)
        self.assertNotIn("js/app.js?v=20260911-sharingfix1", index)


class IssuesEndpointTests(unittest.TestCase):
    """The seam this UI stands on, pinned so a rename fails here loudly."""

    def test_route_and_response_shape_are_unchanged(self) -> None:
        visualizer = VISUALIZER_PY.read_text(encoding="utf-8")
        self.assertIn(
            '"/api/xo-projects/{project_id}/github/issues"', visualizer
        )
        self.assertIn("response_model=GithubIssuesResponse", visualizer)
        models = MODELS_PY.read_text(encoding="utf-8")
        issue = models.split("class GithubIssue(_ForbidExtra):")[1].split("class ")[0]
        for field in ("number", "title", "state", "url", "updated_at",
                      "adopted", "in_progress", "assignees", "labels"):
            self.assertIn(field, issue)


if __name__ == "__main__":
    unittest.main()
