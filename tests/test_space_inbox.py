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
    three route families (the inbox rows, connections, and scheduled jobs).
    These assertions pin those seams so a refactor cannot silently
    drop the tab, its badge, or its stylesheet."""

    def test_view_is_imported_and_registered_with_a_cache_buster(self) -> None:
        app = read("js/app.js")
        self.assertRegex(
            app,
            r"import \{createInboxViews,initInboxBadge\} from './views/inbox\.js\?v=\d{8}-[a-z0-9]+';",
        )
        self.assertIn("createInboxViews().forEach(registerView);", app)
        # Inbox sits between Agents and the Setup route family in the primary nav.
        self.assertLess(app.index("createAgentViews().forEach(registerView);"), app.index("createInboxViews().forEach(registerView);"))
        self.assertLess(app.index("createInboxViews().forEach(registerView);"), app.index("createSetupViews(connectorsView).forEach(registerView);"))

    def test_badge_starts_after_the_registry_in_its_own_bulkhead(self) -> None:
        app = read("js/app.js")
        self.assertIn("try{initInboxBadge();}catch(err)", app)
        # the badge paints onto #tab-inbox, which only exists once
        # startRegistry has built the tab buttons
        self.assertLess(app.index("startRegistry("), app.index("initInboxBadge();"))

    def test_stylesheet_is_linked_and_the_shell_stamp_moved(self) -> None:
        html = read("index.html")
        self.assertIn('<link rel="stylesheet" href="css/inbox.css?v=20260916-jobs2">', html)
        self.assertLess(html.index("css/sharing.css?v="), html.index("css/inbox.css?v="))
        self.assertRegex(html, r'src="js/app\.js\?v=\d{8}-[a-z0-9]+"')
        # the registry creates #view-inbox itself; no section markup needed
        self.assertNotIn('id="view-inbox"', html)

    def test_view_contract_head(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn("export function createInboxViews(", src)
        self.assertIn("INBOX_PAGES.filter(page=>page.section==='inbox').map", src)
        self.assertIn("show()", src)
        self.assertIn("hide:hideInbox", src)

    def test_module_uses_inbox_connections_and_schedules_routes(self) -> None:
        src = read("js/views/inbox.js")
        paths = re.findall(r"API_BASE\+'([^']*)'", src)
        self.assertTrue(paths, "inbox.js makes no API calls")
        # every API_BASE+ is followed by a literal, and every literal is the
        # inbox or the connections/jobs sections it shows above the rows
        self.assertEqual(len(paths), src.count("API_BASE+"))
        for path in paths:
            self.assertTrue(path.startswith(("/api/inbox", "/api/connections", "/api/schedules")), path)
        self.assertIn("'/api/inbox?status=open&limit=1'", src)          # the badge
        self.assertIn("'/api/inbox?status='+encodeURIComponent(filter)+'&limit=200'", src)
        self.assertIn("'/api/inbox/'+encodeURIComponent(id)", src)      # PATCH and DELETE
        self.assertIn("method:'PATCH'", src)
        self.assertIn("method:'DELETE'", src)
        self.assertIn("'/api/connections'", src)                        # the section
        self.assertIn("'/api/connections/'+encodeURIComponent(toolkit)+'/poll'", src)   # Poll now
        # never raw fetch: apiFetch forwards the page query and classifies failures
        self.assertNotIn("fetch(", src.replace("apiFetch(", ""))

    def test_badge_and_poll_use_slotted_intervals(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn("setSlottedInterval('inbox-badge'", src)
        self.assertIn("setSlottedInterval('inbox-poll'", src)
        self.assertIn("clearSlottedInterval('inbox-poll')", src)
        self.assertIn("export function initInboxBadge()", src)
        self.assertIn("export async function refreshInboxBadge(", src)
        # the badge is coerced, never interpolated from a server string, and
        # is a node appended beside the label the registry painted, so the
        # label itself is never rewritten here
        self.assertIn("Number(n)", src)
        self.assertIn("badge=document.createElement('b');badge.className='inb-badge';b.appendChild(badge);", src)
        self.assertIn("badge.textContent=String(n);", src)
        self.assertNotIn("'Inbox<b", src)
        self.assertIn("document.getElementById('tab-inbox')", src)
        css = read("css/inbox.css")
        self.assertIn(".inb-badge", css)
        self.assertIn("#view-inbox{overflow-y:auto}", css)

    def test_failures_and_empty_state_read_like_the_other_tabs(self) -> None:
        src = read("js/views/inbox.js")
        # the failure wording is core's (failText in core/api.js), so every
        # tab says the same thing; the view imports it rather than restating it
        self.assertIn("import {API_BASE,apiFetch,failText} from '../core/api.js';", src)
        self.assertIn("failText(failed)", src)
        api = read("js/core/api.js")
        self.assertIn("export function failText(res)", api)
        self.assertIn("xo-space is unreachable", api)
        self.assertIn("not available for the active agent", api)
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
            "switchTo('projects/data/list');\n    dispatchEvent(new CustomEvent('space:preview-file'",
            src,
        )
        self.assertIn("switchTo(l.view==='projects'?'projects/data/list':l.view)", src)

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
