from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"
# Shared core modules and connector styles retain the account-chip stamp.
# Inbox imports and styles advanced for the Jobs and command-results UI.
STAMP = "20260914-accounts1"
RESULTS_STAMP = "20260914-results1"
AGENTS = ("claude_code", "openclaw", "hermes", "codex", "antigravity")
# en dash (U+2013) and em dash (U+2014) are banned in this repo; spelled as
# escapes so this file passes its own check
DASHES = "[\\u2013\\u2014]"


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def slice_between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


class InboxSourceFilterTests(unittest.TestCase):
    """Source pills filter the loaded page in the browser: six labels from
    one SOURCES table, one data-src attribute per pill (painted by core's
    pills()), and a setSource that repaints without a fetch."""

    def setUp(self) -> None:
        self.src = read("js/views/inbox.js")

    def test_six_source_pills_are_rendered_with_data_src(self) -> None:
        table = slice_between(self.src, "const SOURCES=[", "];")
        for key, label in (
            ("all", "All"),
            ("issues", "Issues"),
            ("connections", "Connections"),
            ("workspace", "Workspace"),
            ("sharing", "Sharing"),
            ("agents", "Agents"),
        ):
            self.assertIn("{id:'" + key + "',label:'" + label + "',sources:[", table)
        self.assertIn("const SOURCE_PILLS=SOURCES.map(s=>[s.id,s.label]);", self.src)
        # the strip is core's pill builder; it paints the wrapper and data-src
        self.assertIn("pills(SOURCE_PILLS,srcFilter,'src','Filter by source','inb-src')", self.src)
        ui = read("js/core/ui.js")
        self.assertIn("'<div class=\"'+esc(cls||'pills-'+attr)+'\" role=\"group\" aria-label=\"'+esc(ariaLabel)+'\">'", ui)
        self.assertIn("'<button type=\"button\" data-'+attr+'=\"'+esc(k)+'\"'", ui)
        self.assertIn("button[data-src]", self.src)

    def test_source_mapping_matches_the_feeder_names(self) -> None:
        # the feeder names live in the table, one row per pill
        table = slice_between(self.src, "const SOURCES=[", "];")
        self.assertIn("{id:'issues',label:'Issues',sources:['issues']}", table)
        self.assertIn("{id:'connections',label:'Connections',sources:['connections']}", table)
        self.assertIn("{id:'sharing',label:'Sharing',sources:['sharing']}", table)
        self.assertIn("{id:'workspace',label:'Workspace',sources:['timeline','todos']}", table)
        self.assertIn("{id:'agents',label:'Agents',sources:[]}", table)
        body = slice_between(self.src, "function sourceOf(it){", "const matchesSource")
        self.assertIn("SOURCES.find(r=>r.sources.includes(s))", body)
        self.assertIn("return row?row.id:'agents';", body)
        self.assertIn("srcFilter==='all'||sourceOf(it)===srcFilter", self.src)

    def test_picking_a_source_never_fetches(self) -> None:
        body = slice_between(self.src, "function setSource(k){", "/* Expanding a new item")
        self.assertIn("render();", body)
        self.assertNotIn("load(", body)
        self.assertNotIn("apiFetch(", body)
        # the rows are filtered from the loaded page, not re-requested
        self.assertIn("const sourceItems=all.filter(matchesSource);", self.src)
        self.assertIn("Nothing from this source on this page.", self.src)
        # the unfiltered empty state is unchanged
        self.assertIn("Nothing in the inbox.", self.src)


class InboxOpenLinkTests(unittest.TestCase):
    def test_open_link_is_an_anchor_with_noopener_and_an_http_only_href(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn(
            "const safeUrl=u=>typeof u==='string'&&/^https?:\\/\\//i.test(u)?u:'';", src
        )
        self.assertIn("const url=safeUrl(it.url);", src)
        self.assertIn(
            '<a class="inb-btn" href="\'+esc(url)+\'" target="_blank" rel="noopener noreferrer">Open link</a>',
            src,
        )
        # rendered only when the check passed
        self.assertIn("+(url?'<a class=\"inb-btn\"", src)
        self.assertIn("a.inb-btn{text-decoration:none", read("css/inbox.css"))


class InboxConnectionsSectionTests(unittest.TestCase):
    """The Connections page reads /api/connections on its own token and
    never blocks Items; Poll now updates both snapshots."""

    def setUp(self) -> None:
        self.src = read("js/views/inbox.js")

    def test_section_reads_the_connections_route_without_a_session_header(self) -> None:
        self.assertIn("apiFetch(API_BASE+'/api/connections')", self.src)
        self.assertIn(
            "apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkit)+'/poll',{method:'POST'})",
            self.src,
        )
        for m in re.finditer(r"apiFetch\(API_BASE\+'/api/connections[^\n]*", self.src):
            self.assertNotIn("sessionHeaders", m.group(0))
            self.assertNotIn("headers", m.group(0))
        # Every call joins API_BASE: inbox rows, connections, or the Jobs
        # section's schedules listing and its Run now. No other API families
        # are used here.
        self.assertNotRegex(self.src, r"apiFetch\('/")
        for path in re.findall(r"API_BASE\+'([^']*)'", self.src):
            self.assertTrue(path.startswith("/api/inbox") or path.startswith("/api/connections")
                            or path in ("/api/schedules", "/api/schedules/"), path)
        self.assertIn("apiFetch(API_BASE+'/api/schedules/'+encodeURIComponent(id)+'/run',{method:'POST'})", self.src)

    def test_section_has_its_own_token_and_failure_line(self) -> None:
        body = slice_between(self.src, "async function loadConns(){", "/* Poll now")
        self.assertIn("const mine=++connsToken;", body)
        self.assertIn("if(mine!==connsToken)return;", body)
        self.assertIn("connsFailed=res", body)
        # a failed read is one muted line, not a blocker
        self.assertIn("Connections: '+esc(failText(connsFailed))", self.src)
        self.assertIn("renderConns();", body)
        # Mount shares a controller; only entering Connections starts its read.
        mount = slice_between(self.src, "async mount(el,ctx){", "show(){")
        self.assertNotIn("loadConns();", mount)
        self.assertNotIn("await load();", mount)
        show = slice_between(self.src, "function showInboxPage(page){", "function hideInbox(){")
        self.assertRegex(show, r"if\(page==='connections'\)\{\s*loadConns\(\);")
        self.assertIn("setSlottedInterval('inbox-conns-poll',loadConns,30000)", show)
        self.assertIn("clearSlottedInterval('inbox-conns-poll')", self.src)
        render = slice_between(self.src, "function renderConns(){", "function connsHTML(){")
        self.assertIn("querySelector('.inb-connections-page')", render)
        self.assertNotIn(".inb-items-page", render)

    def test_rows_and_empty_state(self) -> None:
        self.assertIn(
            "No updates yet. Open Setup → Connectors to connect an app and turn on polling.",
            self.src,
        )
        self.assertIn("c.configured||c.connected_here", self.src)
        self.assertIn("<span>Polled apps</span><b>'+rows.length+'</b>", self.src)
        self.assertIn('data-act="conn-poll" data-toolkit="\'+tk+\'"', self.src)
        self.assertIn('data-act="conn-config" data-toolkit="\'+tk+\'"', self.src)
        self.assertIn(">Poll now</button>", self.src)
        self.assertIn(">Configure</button>", self.src)
        self.assertIn("case'conn-config':switchTo('setup/connectors');break;", self.src)
        # the wording is core/connections.js's, shared with the Connectors
        # drawer so one payload never reads two ways
        self.assertIn("import {collectorLabels,every,pollLine} from '../core/connections.js';", self.src)
        self.assertIn("const line=pollLine(c);", self.src)
        conn = read("js/core/connections.js")
        self.assertIn("'no collectors'", conn)
        self.assertIn("'never polled'", conn)
        self.assertIn('<span class="inb-conn-meta is-error"', self.src)
        # every server string is escaped on the way into innerHTML
        for expr in ("c.toolkit", "c.display_name||c.toolkit", "line.error", "line.text", "collectorLabels(c)"):
            self.assertIn("esc(" + expr + ")", self.src)

    def test_connections_page_opens_rows_and_retains_explicit_collapse(self) -> None:
        self.assertIn("let connsOpen=null;", self.src)
        self.assertIn(
            "const connsIsOpen=()=>connsOpen!==false;",
            self.src,
        )
        self.assertIn("case'conns-toggle':connsOpen=!connsIsOpen();renderConns();break;", self.src)
        self.assertIn('data-act="conns-toggle" aria-expanded=', self.src)

    def test_poll_now_reloads_section_and_rows_together(self) -> None:
        body = slice_between(self.src, "async function pollConn(toolkit){", "\n}\n")
        self.assertIn("connBusy.add(toolkit);renderConns();", body)
        self.assertIn("connBusy.delete(toolkit);", body)
        self.assertIn("await Promise.all([loadConns(),load()]);", body)

    def test_stylesheet_carries_the_new_classes(self) -> None:
        css = read("css/inbox.css")
        for cls in (".inb-src", ".inb-conns", ".inb-conns-head", ".inb-conn-row",
                    ".inb-conn-meta", ".inb-conn-meta.is-error", ".inb-conn-acts"):
            self.assertIn(cls, css)


class ConnectorsPollingDrawerTests(unittest.TestCase):
    """The Polling drawer on the Connectors tab, and every upstream behaviour
    it must not disturb."""

    def setUp(self) -> None:
        self.view = read("js/views/connectors.js")
        self.drawer = slice_between(self.view, "/* ---------- polling ---------- */", "function setAlert(")

    def test_upstream_pins_still_hold(self) -> None:
        # the same pins test_space_wiki keeps, restated here so this feature
        # cannot be the change that breaks them
        self.assertIn("id:'connectors',label:'Connectors'", self.view)
        self.assertNotIn("sessionHeaders", self.view)
        self.assertIn("event.origin!==location.origin", self.view)
        self.assertIn("connector-auth-complete", self.view)
        self.assertIn("connection_request_id=", self.view)
        self.assertNotIn("refresh-gateway", self.view)
        self.assertNotIn("conn-gateway", self.view)
        self.assertNotIn("/api/runtime-config", self.view)
        for agent in AGENTS:
            self.assertNotIn(agent, self.view)
        # the Actions drawer is untouched
        self.assertIn("else if(button.dataset.action==='actions')toggleDrawer(id);", self.view)
        self.assertIn("function renderActions(toolkitId){", self.view)

    def test_polling_button_only_on_cards_connected_and_enabled_here(self) -> None:
        card = slice_between(self.view, "function renderCard(t){", "/* ---------- polling")
        self.assertIn("const polling=openPolling===t.id;", card)
        self.assertIn(
            "+(connected&&(enabled||polling)\n        ?'<button class=\"conn-secondary\" data-action=\"polling\">'",
            card,
        )
        self.assertIn("(polling?'Hide polling':'Polling')", card)
        # the drawer itself needs a connection, not "enabled here": the
        # auto-open after connect runs before the workspace turned it on
        self.assertIn("+(polling&&connected?renderPolling(t,enabled):'')", card)
        self.assertIn("Turn it on here first", self.drawer)

    def test_drawer_form_fields_and_interval_menu(self) -> None:
        self.assertIn(
            "const INTERVALS=[[300,'5 min'],[900,'15 min'],[1800,'30 min'],[3600,'1 hour'],\n"
            "  [21600,'6 hours'],[86400,'24 hours']];",
            self.view,
        )
        self.assertIn('data-poll="enabled"', self.drawer)
        self.assertIn("Collect into Inbox", self.drawer)
        self.assertIn('Every <select data-poll="interval">', self.drawer)
        self.assertIn('data-poll="collector" value="\'+esc(a.id)+\'"', self.drawer)
        self.assertIn("No collectors available for this toolkit yet.", self.drawer)
        self.assertIn('data-action="poll-save">Save</button>', self.drawer)
        self.assertIn('data-action="poll-now"', self.drawer)
        self.assertIn(">Poll now</button>", self.drawer)
        # "Never polled" is core/connections.js's line, capitalised here
        self.assertIn("import {pollLine} from '../core/connections.js';", self.view)
        self.assertIn("const line=pollLine(c);", self.drawer)
        self.assertIn("esc(cap(line.text))", self.drawer)
        self.assertIn("'never polled'", read("js/core/connections.js"))
        self.assertIn('<div class="conn-poll-status is-error">', self.drawer)
        # defaults are checked when the connection is not configured yet
        self.assertIn("available.filter(a=>a.default).map(a=>a.id)", self.drawer)
        # server strings go through esc before innerHTML
        for expr in ("a.id", "a.label||a.id", "line.error", "t.id"):
            self.assertIn("esc(" + expr + ")", self.drawer)
        self.assertIn("CSS.escape(toolkitId)", self.drawer)

    def test_connections_calls_carry_no_session_header(self) -> None:
        # every call goes through API_BASE like the rest of the UI
        self.assertIn("apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId))", self.view)
        self.assertIn(
            "apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId),{method:'PUT',body})",
            self.view,
        )
        self.assertIn(
            "apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId)+'/poll',{method:'POST'})",
            self.view,
        )
        self.assertNotRegex(self.view, r"apiFetch\('/")
        calls = re.findall(r"apiFetch\(API_BASE\+'/api/connections[^\n]*", self.view)
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertNotIn("sessionHeaders", call)
            self.assertNotIn("headers", call)
        # BYO key: the Composio routes carry no session header either
        self.assertIn("const BASE=API_BASE+'/api/connectors/composio';", self.view)
        self.assertIn("apiFetch(BASE+'/toolkits')", self.view)

    def test_save_sends_the_three_fields_and_disables_while_in_flight(self) -> None:
        save = slice_between(self.view, "async function savePolling(", "async function pollNow(")
        self.assertIn("setBusy(button,true);", save)
        self.assertIn("setBusy(button,false);", save)
        self.assertIn("cardError(toolkitId,res.error||'Could not save polling settings.')", save)
        form = slice_between(self.view, "function readPollForm(", "async function savePolling(")
        self.assertIn("enabled:enabled.checked,", form)
        self.assertIn("interval_s:Number(interval.value),", form)
        self.assertIn("collectors:[...drawer.querySelectorAll('input[data-poll=\"collector\"]:checked')]", form)

    def test_poll_now_refetches_and_shows_the_new_events_count(self) -> None:
        poll = slice_between(self.view, "async function pollNow(", "/* Turning a toolkit on or off")
        self.assertIn("'Polled just now: '+(Number(r.new_events)||0)+' new'", poll)
        self.assertIn("await loadPolling(toolkitId);", poll)
        self.assertIn("if(openPolling===toolkitId)renderGrid();", poll)

    def test_drawer_auto_opens_after_a_successful_connect(self) -> None:
        active = slice_between(self.view, "if(status==='ACTIVE'){", "if(status==='FAILED'){")
        self.assertIn("openPolling=toolkitId;", active)
        # set before loadAll(): it re-renders the grid
        self.assertLess(active.index("openPolling=toolkitId;"), active.index("await loadAll();"))
        self.assertIn("await loadPolling(toolkitId);", active)
        self.assertIn("Nothing is persisted until Save.", active)

    def test_drawer_closes_with_the_toolkit(self) -> None:
        self.assertIn("if(!enabled&&openPolling===toolkitId)openPolling=null;", self.view)
        self.assertIn("if(openPolling===toolkitId)openPolling=null;", self.view)
        self.assertIn("delete pollCache[toolkitId];", self.view)

    def test_stylesheet_carries_the_drawer_classes(self) -> None:
        css = read("css/connectors.css")
        for cls in (".conn-poll{", ".conn-poll-row{", ".conn-poll-status{",
                    ".conn-poll-status.is-error", ".conn-poll-acts{", ".conn-poll-note{"):
            self.assertIn(cls, css)


class ConnectedAccountTests(unittest.TestCase):
    """The two fields every entry of GET /api/connections gained
    (account_label, account_checked_at) and POST
    /api/connections/{toolkit}/account: read through core/connections.js,
    painted in the Inbox row and on the Connectors card, requested without
    a session header. The behaviour pins live in
    test_space_pr97_ui.AccountLabelTests; these hold the contract."""

    def test_core_reads_account_label_and_both_views_paint_it(self) -> None:
        core = read("js/core/connections.js")
        self.assertIn("export function accountLabel(c){", core)
        self.assertIn("c.account_label", core)
        inbox = read("js/views/inbox.js")
        self.assertIn("import {accountLabel} from '../core/connections.js';", inbox)
        self.assertIn("'<span class=\"inb-conn-acct\">'+esc(acct)+'</span>'", inbox)
        view = read("js/views/connectors.js")
        self.assertIn("import {accountLabel,accountLine} from '../core/connections.js';", view)
        self.assertIn(
            "'<span class=\"conn-fact conn-account\" title=\"the account this workspace uses\">'+esc(acct)+'</span>'",
            view,
        )
        self.assertIn("account_checked_at:c.account_checked_at||null", view)

    def test_account_route_is_posted_without_a_session_header(self) -> None:
        view = read("js/views/connectors.js")
        accounts = slice_between(view, "/* ---------- accounts ---------- */", "function renderActions(")
        self.assertIn("const path=API_BASE+'/api/connections/'+encodeURIComponent(toolkitId)+'/account';", accounts)
        self.assertIn("apiFetch(path,{method:'POST'})", accounts)
        self.assertIn("const path=API_BASE+'/api/connections';", accounts)
        self.assertNotIn("sessionHeaders", accounts)
        self.assertNotIn("headers", accounts)
        # the route answers 200 with error set on a provider failure: no label, no change to the card
        self.assertIn("if(!label)return;", accounts)
        for agent in AGENTS:
            self.assertNotIn(agent, accounts)
        self.assertNotRegex(accounts, DASHES)


class CacheBusterTests(unittest.TestCase):
    """Changed views and styles advance their stamps; index.html's app.js
    stamp makes a browser re-import the per-view URLs. The core modules every
    view imports bare (api.js, ui.js, connections.js) are stamped once, in
    index.html's import map, so every importer shares one fresh instance."""

    def test_app_js_imports(self) -> None:
        app = read("js/app.js")
        self.assertIn("import workView,{initWorkBadge} from './views/work.js?v=20260919-work4';", app)
        self.assertNotIn("./views/inbox.js", app)
        self.assertIn("import connectorsView from './views/connectors.js?v=20260917-byok1';", app)
        # both views import core/api.js bare: the stamp is the import map's
        self.assertIn("import {API_BASE,apiFetch,failText} from '../core/api.js';", read("js/views/inbox.js"))
        self.assertIn("import {API_BASE,apiFetch} from '../core/api.js';", read("js/views/connectors.js"))
        html = read("index.html")
        for name in ("api.js", "ui.js", "connections.js"):
            stamp = "20260919-work4" if name == "api.js" else STAMP
            self.assertIn('"./js/core/' + name + '":"./js/core/' + name + "?v=" + stamp + '"', html)

    def test_index_html_links(self) -> None:
        html = read("index.html")
        self.assertIn('<link rel="stylesheet" href="css/inbox.css?v=20260916-jobs3">', html)
        # Connectors now shares the Setup shell and its updated styles.
        self.assertIn('<link rel="stylesheet" href="css/connectors.css?v=20260915-typesync1">', html)
        self.assertRegex(html, r'src="js/app\.js\?v=\d{8}-[a-z0-9]+"')
        # the import map is read before app.js is, or it rewrites nothing
        self.assertLess(html.index('<script type="importmap">'), html.index('<script type="module" src="js/app.js'))


class HygieneTests(unittest.TestCase):
    def test_no_dashes_in_new_code(self) -> None:
        for rel in ("js/views/inbox.js", "css/inbox.css"):
            self.assertNotRegex(read(rel), DASHES, rel)
        # connectors.js and .css are upstream files with their own prose; only
        # the slices this feature added are held to the rule
        view = read("js/views/connectors.js")
        self.assertNotRegex(
            slice_between(view, "/* Polling drawer (spec", "export default {"), DASHES
        )
        self.assertNotRegex(
            slice_between(view, "/* ---------- polling ---------- */", "function setAlert("), DASHES
        )
        css = read("css/connectors.css")
        self.assertNotRegex(css[css.index("/* Polling drawer:"):], DASHES)
        self.assertNotRegex(Path(__file__).read_text(encoding="utf-8"), DASHES)

    def test_no_agent_literal_in_the_two_views(self) -> None:
        for rel in ("js/views/inbox.js", "js/views/connectors.js"):
            src = read(rel)
            for agent in AGENTS:
                self.assertNotIn(agent, src, rel)


if __name__ == "__main__":
    unittest.main()
