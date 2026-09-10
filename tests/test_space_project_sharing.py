from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class SpaceProjectSharingCompositionTests(unittest.TestCase):
    """Project sharing in the Space UI is ONE surface: the Sharing lens of the
    Files tab (views/sharing.js painting, views/sharing_data.js talking to the
    BFF). The List lens carries none of it. These assertions pin the seams so
    a refactor cannot quietly grow a second sharing surface, drop a control,
    or route a call around the BFF."""

    def test_list_lens_carries_no_sharing_surface(self) -> None:
        projects = read("js/views/projects.js")
        for needle in ("sharing_data.js", "projects_sharing.js", "sharingPanel", "shr-",
                       "sharingStripHTML", "sharedWithYouHTML", "startSharingPoll", "data-shr-row"):
            self.assertNotIn(needle, projects, needle)
        # the one hand-off stays: a Sharing row's "Open in List" opens the drawer
        self.assertIn("space:open-project", projects)
        self.assertFalse((UI / "js" / "views" / "projects_sharing.js").exists())

    def test_data_module_talks_only_to_the_bff_routes(self) -> None:
        mod = read("js/views/sharing_data.js")
        for path in (
            "/api/project-sharing/status",
            "/api/project-sharing/check",
            "/api/xo-projects",
            "/members",
            "/share",
            "/revoke",
            "/apply",
            "/commits?limit=",
        ):
            self.assertIn(path, mod)
        self.assertNotIn("/commits/poll", mod)   # the browser never talks to swarm
        self.assertNotIn("document.", mod)       # data half: no DOM
        # the pane never fetches on its own
        pane = read("js/views/sharing.js")
        self.assertNotIn("apiFetch(", pane)
        self.assertNotIn("fetch(", pane)
        self.assertIn("from './sharing_data.js?v=", pane)

    def test_relay_status_is_the_only_source_of_shared(self) -> None:
        mod = read("js/views/sharing_data.js")
        self.assertIn("export function memberState(projectId)", mod)
        for state in ("unknown", "disabled", "solo", "live"):
            self.assertIn("'" + state + "'", mod)
        self.assertIn("function others(e)", mod)
        self.assertIn("typeof m==='number'", mod)
        # the owner row never leaves the swarm group: a repo whose last
        # member was revoked still reads shared with a count of 1, and that
        # is "not shared" to a person — it must leave the rail
        self.assertIn("mine:!!r.project&&!!r.shared&&others(r)!==0", mod)
        pane = read("js/views/sharing.js")
        self.assertIn("if(memberState(id)==='live'&&!members.has(id))loadMembers(id);", pane)
        self.assertIn("if(st!=='live')return", pane)

    def test_pane_is_rail_plus_detail_with_the_composer_swapped_in(self) -> None:
        """Direction A: rail (inbox + shared projects, work waiting first) and
        a detail panel; the share composer takes the panel's place."""
        pane = read("js/views/sharing.js")
        css = read("css/sharing.css")
        for fn in ("function railHTML", "function inboxRow", "function railRow",
                   "function detailHTML", "function composerHTML", "function emptyCardsHTML"):
            self.assertIn(fn, pane)
        self.assertIn("composer?composerHTML():r?detailHTML(r)", pane)
        self.assertIn("data-act=\"select\"", pane)
        self.assertIn("urgency(b)-urgency(a)", pane)   # work waiting first
        for cls in (".shl-split", ".shl-rail", ".shl-inbox", ".shl-row.is-sel", ".shl-detail",
                    ".shl-composer", ".shl-pick", ".shl-empty-card", ".shr-strip"):
            self.assertIn(cls, css)
        # every state the inbox can be in has a row
        for state in ("needs_auth", "no_access", "exists", "cloning", "available"):
            self.assertIn("'" + state + "'", pane)
        self.assertEqual(pane.count("data-act=\"connect\""), 1)

    def test_seamless_sync_controls(self) -> None:
        """Apply (fast-forward), Check now, copy invite: the three controls
        that turn a copy-pasted command and a chat back-and-forth into one
        click each."""
        pane = read("js/views/sharing.js")
        data = read("js/views/sharing_data.js")
        self.assertIn("data-act=\"apply\"", pane)
        self.assertIn("async function doApply(id)", pane)
        self.assertIn("'applied '+plural(n,'commit')", pane)
        self.assertIn("data-act=\"check\"", pane)
        self.assertIn("async function doCheck(btn)", pane)
        self.assertIn("copy invite", pane)
        self.assertIn("export function inviteText()", data)
        self.assertIn("export function applyCmd(path,branch)", data)
        self.assertIn("merge --ff-only origin/", data)
        # the by-hand command never leaves: Apply can be refused by git
        self.assertIn("or by hand", pane)

    def test_members_are_polished(self) -> None:
        pane = read("js/views/sharing.js")
        row = pane.split("function memberRow")[1].split("function membersHTML")[0]
        self.assertIn(">you<", row)
        self.assertNotIn("this workspace", row)
        self.assertIn("data-copy=", row)
        self.assertIn("shortId(m.workspace_id)", row)
        self.assertIn('title="\'+esc(m.workspace_id)', row)
        self.assertIn("memberRank", pane)                 # owner first
        self.assertIn("shr-confirm-yes", row)             # inline revoke confirm
        self.assertIn("data-act=\"revoke-no\"", row)
        self.assertIn("Not shared with anyone yet", pane)
        self.assertIn("prj-skel", pane)                   # shaped loading state

    def test_clone_command_targets_the_reported_projects_root(self) -> None:
        mod = read("js/views/sharing_data.js")
        self.assertIn("status.projects_root", mod)
        self.assertIn("||'~/xo-projects'", mod)

    def test_poll_is_shared_and_edit_safe(self) -> None:
        data = read("js/views/sharing_data.js")
        pane = read("js/views/sharing.js")
        self.assertIn("const subscribers=new Set()", data)
        self.assertIn("projects-sharing-fast", data)      # faster poll only while cloning
        # a poll tick never wipes a half-typed composer or a pending revoke
        self.assertIn("const editing=()=>!!composer||!!confirmRevoke;", pane)
        self.assertIn("if(editing()){", pane)

    def test_stylesheet_and_module_are_cache_busted(self) -> None:
        html = read("index.html")
        self.assertIn('href="css/sharing.css?v=', html)
        app = read("js/app.js")
        m = re.search(r"from '\./views/sharing\.js\?v=([\w-]+)'", app)
        self.assertIsNotNone(m, "app.js must import sharing.js with ?v=")
        pane = read("js/views/sharing.js")
        m2 = re.search(r"from '\./sharing_data\.js\?v=([\w-]+)'", pane)
        self.assertIsNotNone(m2, "sharing.js must import sharing_data.js with ?v=")


if __name__ == "__main__":
    unittest.main()
