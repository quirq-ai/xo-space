"""The Work section (docs/work-and-workitems.md): the Inbox tab renamed and
reduced to Inbox, Live and History, built from the shadcn kit. The Inbox
page reads GET /api/work/inbox (section 16.5); Live and History stay on
sample data until their routes land."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "space_ui"
STAMP = "20260919-work4"
DASHES = re.compile("[\\u2013\\u2014]")  # en dash, em dash: banned in new files
AGENTS = ("openclaw", "hermes", "claude_code", "codex", "antigravity")
VIEWS = ("js/views/work.js", "js/views/work-live.js", "js/views/work-history.js")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class WorkWiringTests(unittest.TestCase):
    def test_views_are_imported_and_registered_between_agents_and_setup(self) -> None:
        app = read("js/app.js")
        self.assertIn("import workView,{initWorkBadge} from './views/work.js?v=" + STAMP + "';", app)
        self.assertIn("import workLiveView from './views/work-live.js?v=" + STAMP + "';", app)
        self.assertIn("import workHistoryView from './views/work-history.js?v=" + STAMP + "';", app)
        for name in ("workView", "workLiveView", "workHistoryView"):
            self.assertIn("registerView(" + name + ");", app)
        self.assertLess(app.index("createAgentViews().forEach(registerView);"), app.index("registerView(workView);"))
        self.assertLess(app.index("registerView(workHistoryView);"), app.index("createSetupViews(connectorsView).forEach(registerView);"))
        self.assertNotIn("inbox", app.lower())
        # Sharing is a section of Activity; the old page is no longer registered
        self.assertNotIn("./views/sharing.js", app)
        self.assertNotIn("registerView(sharingView)", app)

    def test_badge_starts_after_the_registry_in_its_own_bulkhead(self) -> None:
        app = read("js/app.js")
        self.assertIn("try{initWorkBadge();}catch(err)", app)
        # the badge paints onto #tab-work, which only exists once startRegistry built the tabs
        self.assertLess(app.index("startRegistry("), app.index("initWorkBadge();"))
        feed = read("js/views/work.js")
        self.assertIn("document.getElementById('tab-work')", feed)

    def test_stylesheet_is_linked_and_the_shell_stamp_moved(self) -> None:
        html = read("index.html")
        self.assertIn('<link rel="stylesheet" href="css/work.css?v=' + STAMP + '">', html)
        self.assertIn('<link rel="stylesheet" href="css/shadcn.css?v=' + STAMP + '">', html)
        self.assertIn('src="js/app.js?v=' + STAMP + '"', html)
        # the registry creates the Work sections itself; no section markup needed
        for section in ("view-work", "view-work-live", "view-work-history"):
            self.assertNotIn('id="' + section + '"', html)

    def test_navigation_names_the_feed_tab_and_keeps_every_inbox_route(self) -> None:
        nav = read("js/core/navigation.js")
        self.assertIn("{id:'work',label:'Work',defaultView:'work',aliases:['inbox','feed']}", nav)
        self.assertIn("['work','','Inbox',['inbox','inbox/items','feed']]", nav)
        self.assertIn("['work-live','live','Live',['inbox/jobs','inbox/connections','feed/live']]", nav)
        self.assertIn("['work-history','history','History',['work/activity','feed/activity','feed/history','inbox/activity','inbox/sharing-activity','sharing','projects/sharing','inbox/sharing']]", nav)
        switcher = read("js/core/section-nav.js")
        self.assertIn("work:WORK_PAGES", switcher)
        self.assertNotIn("INBOX_PAGES", switcher)
        palette = read("js/core/command-palette.js")
        for route in ("work", "work/live", "work/history"):
            self.assertIn("['" + route + "',", palette)
        self.assertNotIn("'inbox/", palette)
        self.assertIn("switchTo('work/history')", read("js/core/project-actions.js"))

    def test_every_importer_of_the_changed_core_modules_moved_together(self) -> None:
        for rel in sorted((ROOT / "js").rglob("*.js")):
            src = rel.read_text(encoding="utf-8")
            if "views/inbox" in str(rel) or "views/sharing" in str(rel):
                continue  # unregistered, awaiting removal
            for module in ("navigation", "shadcn", "section-nav", "data-views", "preview"):
                for match in re.finditer(r"core/" + module + r"\.js\?v=([0-9]{8}-[a-z0-9]+)", src):
                    self.assertEqual(match.group(1), STAMP, str(rel))


class WorkInboxWiringTests(unittest.TestCase):
    """The Inbox page is on the API: one read paints it, every action is one
    call then a reread, and the sample module is no longer imported."""

    def test_the_inbox_reads_and_writes_the_work_api(self) -> None:
        src = read("js/views/work.js")
        self.assertIn("from '../core/api.js';", src)
        self.assertNotIn("work-sample", src)
        for route in ("/api/work/inbox", "/api/work/summary", "/api/work/dismiss", "/api/work/ack", "/api/work/promote",
                      "/api/telemetry/sources"):
            self.assertIn("'" + route + "'", src, route)
        # the work item routes are the existing ones, never a copy
        self.assertIn("'/api/xo-projects/'+encodeURIComponent(p)+'/workitems'", src)
        self.assertIn("/assignee',{method:'PUT'", src)
        self.assertIn("{method:'PATCH',body:{status:'closed',state_reason:'completed'}}", src)
        self.assertIn("{method:'PATCH',body:{status:'open',state_reason:'reopened'}}", src)
        # a dismissal carries the since; an acknowledgement the key alone
        self.assertIn("body:kind==='ack'?{key}:{key,since:since||null}", src)
        # the page polls while shown and the badge polls the summary while it is not
        self.assertIn("setSlottedInterval('work-inbox'", src)
        self.assertIn("setSlottedInterval('work-badge'", src)
        self.assertIn("clearSlottedInterval('work-inbox')", src)
        # a failed reread keeps the page and says why
        self.assertIn("failed=failText(res)", src)

    def test_the_inbox_drives_the_connection_items(self) -> None:
        src = read("js/views/work.js")
        self.assertIn("'/api/work/inbox/items/'+encodeURIComponent(ref.toolkit)+'/'+encodeURIComponent(ref.item_id)+tail", src)
        for reason in ("item_new", "item_question", "item_draft", "item_task", "item_failed"):
            self.assertIn(reason + ":[", src, reason)
        self.assertIn("itemPath(a.ref,'/start'+(retry?'?retry=true':''))", src)
        self.assertIn("itemPath(ref,'/decide'),{method:'POST',body:{action,...extra}}", src)
        self.assertIn("itemPath(ref,'/send')", src)
        # an item row is dismissed and acknowledged through its own decide route, never the generic marks
        self.assertIn("if(a?.ref?.item_id)await decideItem(a.ref,'dismiss'", src)
        self.assertIn("if(c?.ref?.item_id)await decideItem(c.ref,'accept'", src)
        # a question opens the session on the Agents page
        self.assertIn("dispatchEvent(new CustomEvent('space:open-session',{detail:{id}}))", src)
        sessions = read("js/views/sessions.js")
        self.assertIn("addEventListener('space:open-session'", sessions)
        self.assertIn("openPendingSession();", sessions)

    def test_an_item_opens_as_a_thread_with_a_reply_box(self) -> None:
        src = read("js/views/work.js")
        self.assertIn("itemPath(ref,'/thread')", src)
        self.assertIn("itemPath(ref,'/reply'),{method:'POST',body:{text}}", src)
        self.assertIn("function threadHTML(key)", src)
        # the mail first, then every turn, then the reply box, from the kit's textarea
        self.assertIn("msgHTML('item'", src)
        self.assertIn("textarea({placeholder:", src)
        self.assertIn("data-field=\"reply\"", src)
        self.assertIn("data-act=\"toggle-item\"", src)
        # the page polls an open thread while the agent answers, and cmd/ctrl+enter sends
        self.assertIn("setSlottedInterval('work-thread'", src)
        self.assertIn("(ev.key==='Enter'||ev.key==='Return')&&(ev.metaKey||ev.ctrlKey)&&ev.target.matches('[data-field=\"reply\"]')", src)
        # a repaint (the 30 s reread) keeps what is typed in an open reply box
        self.assertIn("const drafts=new Map();", src)
        self.assertIn("if(box&&!box.disabled)box.value=text;", src)
        self.assertIn("export function textarea(", read("js/core/shadcn.js"))
        self.assertIn('[data-slot="textarea"]', read("css/shadcn.css"))
        for cls in (".work-thread", ".work-msg", ".work-reply"):
            self.assertIn(cls + "{", read("css/work.css"), cls)

    def test_the_inbox_knows_the_api_shapes_not_the_sample_ones(self) -> None:
        src = read("js/views/work.js")
        for name in ("a.project_id", "c.project_id", "w.project_id", "w.updated_at", "a.ref?.workitem_id", "c.ref?.workitem_id"):
            self.assertIn(name, src, name)
        self.assertNotIn("w.updated)", src)
        self.assertNotIn("a.project)", src)
        self.assertIn("agent_question", src)


class WorkHygieneTests(unittest.TestCase):
    def test_pages_use_the_kit_and_no_page_local_controls(self) -> None:
        for rel in VIEWS:
            src = read(rel)
            self.assertIn("from '../core/shadcn.js?v=" + STAMP + "';", src, rel)
            self.assertNotIn("pills(", src, rel)          # the pre-kit pill strip
            self.assertNotIn("class=\"inb-", src, rel)     # no Inbox markup carried over
            self.assertNotIn("<table", src, rel)

    def test_no_dashes_and_no_agent_literal_in_the_views(self) -> None:
        for rel in VIEWS + ("css/work.css", "js/views/work-sample.js", "js/core/navigation.js"):
            self.assertNotRegex(read(rel), DASHES, rel)
        # the sample module is fixture data; the views themselves name no agent
        for rel in VIEWS:
            src = read(rel)
            for agent in AGENTS:
                self.assertNotIn(agent, src, rel)
        self.assertNotRegex(Path(__file__).read_text(encoding="utf-8"), DASHES)

    def test_kit_gained_native_select_calendar_and_timeline(self) -> None:
        kit = read("js/core/shadcn.js")
        for fn in ("nativeSelect", "calendar", "timeline", "timelineItem"):
            self.assertIn("export function " + fn + "(", kit)
        self.assertIn("export const timelineSeparator=", kit)
        css = read("css/shadcn.css")
        for slot in ("native-select-wrapper", "native-select", "native-select-icon",
                     "calendar", "calendar-caption", "calendar-weekdays", "calendar-day",
                     "timeline", "timeline-item", "timeline-dot", "timeline-separator"):
            self.assertIn('[data-slot="' + slot + '"]', css)
        # Activity is the timeline; Live is the console; the Inbox is grouped
        history = read("js/views/work-history.js")
        self.assertIn("timelineSeparator(esc(dayLabel(e.ts)))", history)
        # History analyses a window with the chart kit
        for chart in ("areaChart", "donutChart", "heatmapChart", "barChartStacked", "barChartHorizontal"):
            self.assertIn(chart + "(", history)
        self.assertIn("const WINS=", history)
        self.assertIn("class=\"work-log\"", read("js/views/work-live.js"))
        self.assertIn("agents?.find(a=>a.id===id)?.label", read("js/views/work.js"), "labels come from the API, never a literal")
        self.assertIn("TITLES[k]", read("js/views/work.js"))
        self.assertIn("data-group-card=", read("js/views/work.js"))
        # one Calendar in the kit: development's (#138, Sunday-first grid with
        # wireCalendar); the Live page speaks its API (Date month, step buttons)
        self.assertEqual(kit.count("export function calendar("), 1)
        self.assertIn("export function wireCalendar(", kit)
        live = read("js/views/work-live.js")
        self.assertIn("calendar({month:new Date(my,mm-1,1),selected,today,marks})", live)
        self.assertIn("[data-calendar-step]", live)
        self.assertNotIn("data-cal-nav", live)


if __name__ == "__main__":
    unittest.main()
