"""Reusable Manage Issues preserve the existing GitHub mirror contract."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ISSUES_JS = ROOT / "space_ui" / "js" / "core" / "project-issues.js"
MANAGE_JS = ROOT / "space_ui" / "js" / "views" / "project-management.js"
ISSUES_CSS = ROOT / "space_ui" / "css" / "project-management.css"
INDEX_HTML = ROOT / "space_ui" / "index.html"
APP_JS = ROOT / "space_ui" / "js" / "app.js"
MODELS_PY = ROOT / "api" / "xo_projects" / "_visualizer_models.py"
VISUALIZER_PY = ROOT / "api" / "xo_projects" / "visualizer.py"


class IssuesPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issues = ISSUES_JS.read_text(encoding="utf-8")

    def test_component_reads_the_existing_mirror_only_when_loaded(self) -> None:
        self.assertIn("/github/issues", self.issues)
        self.assertIn("function load({force=false,refresh=false}", self.issues)
        self.assertIn("createProjectIssues", MANAGE_JS.read_text())
        projects = (ROOT / "space_ui/js/views/projects.js").read_text()
        self.assertNotIn("/github/issues", projects)
        self.assertNotIn("data-project-tab", projects)

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
                self.assertIn(state, self.issues)
        # each non-ok state carries its own sentence, not a shared one
        empties = self.issues.split("const EMPTY={")[1].split("};")[0]
        for state in ("no_remote", "never_polled", "issues_disabled", "empty"):
            self.assertIn(state, empties)
        self.assertIn("Last poll failed:", self.issues)

    def test_filter_and_search_repaint_rows_without_replacing_controls(self) -> None:
        handlers = self.issues.split("$('.iss-q').addEventListener")[1]
        self.assertIn("paintRows()", handlers)
        self.assertEqual(handlers.count("load({force:true})"), 1)
        self.assertNotIn("innerHTML", handlers)
        self.assertNotIn("request(", handlers)

    def test_only_explicit_refresh_polls_and_can_supersede_an_old_read(self) -> None:
        self.assertIn("(force?'?refresh=1':'')", self.issues)
        self.assertIn("if(data&&!force&&!refresh)return Promise.resolve(data)", self.issues)
        self.assertIn("if(pending&&(!force||forcing))return pending", self.issues)
        self.assertIn("mine!==generation", self.issues)
        self.assertIn("readController?.abort()", self.issues)

    def test_a_bad_mirror_row_cannot_become_a_link(self) -> None:
        """Issue text is GitHub's, not ours. Titles and labels are escaped
        like everything else, and the href is only rendered for a real https
        URL — so a javascript: url degrades to plain text."""
        row = self.issues.split("function issueRow(issue){")[1].split("export function")[0]
        self.assertIn("url.protocol==='https:'", self.issues)
        self.assertIn("url.hostname==='github.com'", self.issues)
        self.assertIn("!url.username&&!url.password&&!url.port", self.issues)
        self.assertIn('rel="noopener noreferrer"', row)
        self.assertIn("esc(issue.title", row)
        self.assertIn("esc(label)", row)
        self.assertIn("esc(who)", row)
        self.assertNotIn("esc(assignee", row, "Assignee names are escaped once at rendering")

    def test_labels_absent_is_not_labels_empty(self) -> None:
        """The poller does not fetch labels, so it writes null — and an empty
        list would be the claim that the issue has none."""
        models = MODELS_PY.read_text(encoding="utf-8")
        self.assertIn("labels: Optional[list[str]] = None", models)
        self.assertIn("list(issue.labels)", self.issues)

    def test_the_live_row_keeps_the_accent(self) -> None:
        """in_progress is the only thing in this panel that is happening right
        now, so it is the only thing that gets the accent chip."""
        row = self.issues.split("function issueRow(issue){")[1]
        self.assertIn("iss-work is-active", row)
        self.assertIn("in progress", self.issues)
        self.assertIn("tracked", self.issues)

    def test_closed_says_why_it_may_be_empty(self) -> None:
        """The poller asks for OPEN issues only; a closed row exists only for
        an issue Space watched close. "None" must not read as "this repo has
        no closed issues"."""
        self.assertIn("watches it close", self.issues)
        poller = (ROOT / "services" / "cowork_agent" / "connectors" / "github"
                  / "issues.py").read_text(encoding="utf-8")
        self.assertIn("states: [OPEN]", poller)

    def test_each_project_component_owns_its_controls_and_read_lifecycle(self) -> None:
        self.assertIn("const element=document.createElement('section')", self.issues)
        self.assertIn("let data=null,state='open',query=''", self.issues)
        self.assertIn("destroy(){disposed=true;readController?.abort()", self.issues)
        self.assertIn("data?.project_id===id", self.issues)

    def test_rows_are_links_out_and_styled_as_links(self) -> None:
        css = ISSUES_CSS.read_text(encoding="utf-8")
        self.assertIn(".iss-row", css)
        self.assertIn("a.iss-row:hover", css)
        self.assertIn("a.iss-row:focus-visible", css)
        # the state dot, not a coloured row, carries open vs closed
        self.assertIn(".iss-dot", css)
        self.assertIn(".iss-row.is-closed .iss-dot", css)


class IssuesDocsTests(unittest.TestCase):
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
        # Relative to the api/xo_projects folder, which is the URL.
        self.assertIn('"/{project_id}/github/issues"', visualizer)
        self.assertIn("response_model=GithubIssuesResponse", visualizer)
        models = MODELS_PY.read_text(encoding="utf-8")
        issue = models.split("class GithubIssue(_ForbidExtra):")[1].split("class ")[0]
        for field in ("number", "title", "state", "url", "updated_at",
                      "adopted", "in_progress", "assignees", "labels"):
            self.assertIn(field, issue)


if __name__ == "__main__":
    unittest.main()
