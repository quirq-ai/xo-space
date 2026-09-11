from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class SpaceInboxCompositionTests(unittest.TestCase):
    """The Inbox tab is composed into the shell through explicit seams: one
    import and one registerView in app.js, one badge starter after the
    registry, one stylesheet link, and a view module that talks to exactly
    one route family. These assertions pin those seams so a refactor cannot
    silently drop the tab, its badge, or its stylesheet."""

    def test_view_is_imported_and_registered_with_a_cache_buster(self) -> None:
        app = read("js/app.js")
        self.assertIn(
            "import inboxView,{initInboxBadge} from './views/inbox.js?v=20260911-connections1';",
            app,
        )
        self.assertIn("registerView(inboxView);", app)
        # Inbox sits between Sessions and Wiki in the nav; the registration
        # order mirrors that so the import list reads like the tab bar.
        self.assertLess(app.index("registerView(sessionsView);"), app.index("registerView(inboxView);"))
        self.assertLess(app.index("registerView(inboxView);"), app.index("registerView(wikiView);"))

    def test_badge_starts_after_the_registry_in_its_own_bulkhead(self) -> None:
        app = read("js/app.js")
        self.assertIn("try{initInboxBadge();}catch(err)", app)
        # the badge paints onto #tab-inbox, which only exists once
        # startRegistry has built the tab buttons
        self.assertLess(app.index("startRegistry("), app.index("initInboxBadge();"))

    def test_stylesheet_is_linked_and_the_shell_stamp_moved(self) -> None:
        html = read("index.html")
        self.assertIn('<link rel="stylesheet" href="css/inbox.css?v=20260911-connections1">', html)
        self.assertLess(html.index("css/sharing.css?v="), html.index("css/inbox.css?v="))
        self.assertIn('src="js/app.js?v=20260911-connections1"', html)
        # the registry creates #view-inbox itself; no section markup needed
        self.assertNotIn('id="view-inbox"', html)

    def test_view_contract_head(self) -> None:
        src = read("js/views/inbox.js")
        head = src[src.index("export default") : src.index("mount(")]
        self.assertIn("id:'inbox',label:'Inbox',order:5", head)
        self.assertNotIn("nav:false", head)
        self.assertIn("show()", src)
        self.assertIn("hide()", src)

    def test_module_talks_only_to_the_inbox_route(self) -> None:
        src = read("js/views/inbox.js")
        paths = re.findall(r"API_BASE\+'([^']*)'", src)
        self.assertTrue(paths, "inbox.js makes no API calls")
        # every API_BASE+ is followed by a literal, and every literal is the inbox
        self.assertEqual(len(paths), src.count("API_BASE+"))
        for path in paths:
            self.assertTrue(path.startswith("/api/inbox"), path)
        self.assertIn("'/api/inbox?status=open&limit=1'", src)          # the badge
        self.assertIn("'/api/inbox?status='+encodeURIComponent(filter)+'&limit=200'", src)
        self.assertIn("'/api/inbox/'+encodeURIComponent(id)", src)      # PATCH and DELETE
        self.assertIn("method:'PATCH'", src)
        self.assertIn("method:'DELETE'", src)
        # never raw fetch: apiFetch forwards the page query and classifies failures
        self.assertNotIn("fetch(", src.replace("apiFetch(", ""))

    def test_badge_and_poll_use_slotted_intervals(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn("setSlottedInterval('inbox-badge'", src)
        self.assertIn("setSlottedInterval('inbox-poll'", src)
        self.assertIn("clearSlottedInterval('inbox-poll')", src)
        self.assertIn("export function initInboxBadge()", src)
        self.assertIn("export async function refreshInboxBadge(", src)
        # the badge is coerced, never interpolated from a server string
        self.assertIn("Number(n)", src)
        self.assertIn("'Inbox<b class=\"inb-badge\">'+n+'</b>':'Inbox'", src)
        self.assertIn("document.getElementById('tab-inbox')", src)
        css = read("css/inbox.css")
        self.assertIn(".inb-badge", css)
        self.assertIn("#view-inbox{overflow-y:auto}", css)

    def test_failures_and_empty_state_read_like_the_other_tabs(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn("xo-space is unreachable", src)
        self.assertIn("not available for the active agent", src)
        self.assertIn("Nothing in the inbox.", src)
        self.assertIn("Sessions, todos and shares arriving in the workspace land here.", src)

    def test_rows_are_buttons_and_every_field_is_escaped(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn('<button class="inb-row-head" type="button"', src)
        for field in ("it.id", "it.status", "it.kind", "it.title", "it.project_id", "it.body"):
            self.assertIn("esc(" + field + ")", src)
        self.assertIn('<pre class="inb-text">', src)
        self.assertIn("white-space:pre-wrap", read("css/inbox.css"))

    def test_open_action_switches_before_previewing(self) -> None:
        src = read("js/views/inbox.js")
        # the previewer closes on any non-Files view, so the switch comes first
        self.assertIn(
            "switchTo('projects');\n    dispatchEvent(new CustomEvent('space:preview-file'",
            src,
        )
        self.assertIn("switchTo(l.view)", src)

    def test_mark_all_seen_is_page_bounded_and_seen_is_patched_once(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn("it.status==='new').map(it=>it.id)", src)
        self.assertIn("if(!expanded.has(id)||it.status!=='new'||busy.has(id))return;", src)
        self.assertIn("busy.add(id)", src)

    def test_new_files_carry_no_dashes(self) -> None:
        # en dash (U+2013) and em dash (U+2014) are banned in this repo;
        # spelled as escapes so this file passes its own check
        dashes = "[\u2013\u2014]"
        for rel in ("js/views/inbox.js", "css/inbox.css"):
            self.assertNotRegex(read(rel), dashes, rel)
        self.assertNotRegex(Path(__file__).read_text(encoding="utf-8"), dashes)


if __name__ == "__main__":
    unittest.main()
