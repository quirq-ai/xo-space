"""PR #97 frontend fixes: the seams the review asked for, pinned the way the
other space_ui tests pin theirs, plus two behaviour tests that run under
node (skipped when node is not installed): the pure core helpers, and the
Connectors Polling drawer driven through its own click listener over a DOM
stub (paint order, the paint after Save, one drawer at a time).

Drop-in for tests/: ROOT resolves to the repo when this file lives in
tests/; XO_SPACE_ROOT overrides it so the file also runs from elsewhere."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("XO_SPACE_ROOT") or Path(__file__).resolve().parents[1])
UI = ROOT / "space_ui"
STAMP = "20260913-inboxfix2"
DASHES = re.compile("[\\u2013\\u2014]")


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def slice_between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", "file://" + str(UI)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class CoreHelpersTests(unittest.TestCase):
    """core/ui.js, core/connections.js and core/api.js export the helpers the
    views used to copy; their contracts are checked by running them."""

    def test_ui_exports_esc_rel_pills_and_toast(self) -> None:
        ui = read("js/core/ui.js")
        for name in ("export const esc=", "export function rel(", "export function pills(", "export function toast("):
            self.assertIn(name, ui)
        self.assertIn("aria-pressed=\"true\"", ui)
        self.assertIn("' class=\"is-on\" aria-pressed=\"true\"'", ui)
        self.assertIn("role=\"group\" aria-label=\"'+esc(ariaLabel)+'\"", ui)

    def test_api_exports_fail_text(self) -> None:
        api = read("js/core/api.js")
        self.assertIn("export function failText(res){", api)
        self.assertIn("'xo-space is unreachable'", api)
        self.assertIn("'not available for the active agent'", api)
        self.assertIn("failText(res)", api[: api.index("import ")])  # documented in the header

    def test_connections_formatters_are_pure_and_shared(self) -> None:
        conn = read("js/core/connections.js")
        for name in ("export function every(", "export function collectorLabels(", "export function pollLine("):
            self.assertIn(name, conn)
        self.assertNotIn("document.", conn)
        self.assertNotIn("apiFetch", conn)
        self.assertIn("import {rel} from './ui.js';", conn)
        # both views import from core, never from each other
        inbox = read("js/views/inbox.js")
        connectors = read("js/views/connectors.js")
        self.assertIn("from '../core/connections.js';", inbox)
        self.assertIn("from '../core/connections.js';", connectors)
        self.assertNotIn("./connectors.js", inbox)
        self.assertNotIn("./inbox.js", connectors)

    def test_sharing_data_re_exports_the_core_helpers(self) -> None:
        data = read("js/views/sharing_data.js")
        self.assertIn("export {esc,rel} from '../core/ui.js';", data)
        self.assertNotIn("export const esc=", data)
        self.assertNotIn("export function rel(", data)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_helpers_behave_under_node(self) -> None:
        script = """
          globalThis.location={pathname:'/space/',search:''};
          const ui=await import(process.argv[1]+'/js/core/ui.js');
          const conn=await import(process.argv[1]+'/js/core/connections.js');
          const api=await import(process.argv[1]+'/js/core/api.js');
          const now=Date.now();
          const iso=ms=>new Date(now-ms).toISOString();
          const out={
            esc:ui.esc('<a href="x">&</a>'),
            escNull:ui.esc(null),
            relEmpty:ui.rel(''),
            relBad:ui.rel('not a date'),
            relNow:ui.rel(iso(5000)),
            relMin:ui.rel(iso(5*60000)),
            relHour:ui.rel(iso(3*3600000)),
            relDay:ui.rel(iso(4*86400000)),
            relOldIsDate:!/ago$/.test(ui.rel(iso(45*86400000))),
            pills:ui.pills([['a','A'],['b','B<']],'b','src','Pick one','my-strip'),
            pillsDefaultClass:ui.pills([['a','A']],'a','win','W'),
            every900:conn.every(900),every7200:conn.every(7200),everyZero:conn.every(0),every90:conn.every(90),
            labels:conn.collectorLabels({collectors:['mail','x'],available_collectors:[{id:'mail',label:'Mail'}]}),
            labelsNone:conn.collectorLabels({collectors:[]}),
            lineErr:conn.pollLine({last_error:'boom',last_poll_at:iso(60000)}),
            lineNever:conn.pollLine({}),
            linePolled:conn.pollLine({last_poll_at:iso(2*60000)}),
            failOffline:api.failText({offline:true,notImplemented:false,error:'x'}),
            fail501:api.failText({offline:false,notImplemented:true,error:'x'}),
            failMsg:api.failText({offline:false,notImplemented:false,error:'nope'}),
            failBare:api.failText({offline:false,notImplemented:false,error:null}),
          };
          console.log(JSON.stringify(out));
        """
        out = run_node(script)
        self.assertEqual(out["esc"], "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;")
        self.assertEqual(out["escNull"], "")
        self.assertEqual(out["relEmpty"], "")
        self.assertEqual(out["relBad"], "")
        self.assertEqual(out["relNow"], "just now")
        self.assertEqual(out["relMin"], "5m ago")
        self.assertEqual(out["relHour"], "3h ago")
        self.assertEqual(out["relDay"], "4d ago")
        self.assertTrue(out["relOldIsDate"])
        self.assertEqual(
            out["pills"],
            '<div class="my-strip" role="group" aria-label="Pick one">'
            '<button type="button" data-src="a" aria-pressed="false">A</button>'
            '<button type="button" data-src="b" class="is-on" aria-pressed="true">B&lt;</button></div>',
        )
        self.assertTrue(out["pillsDefaultClass"].startswith('<div class="pills-win" role="group" aria-label="W">'))
        self.assertEqual(out["every900"], "every 15 min")
        self.assertEqual(out["every7200"], "every 2 h")
        self.assertEqual(out["everyZero"], "every 1 min")
        self.assertEqual(out["every90"], "every 2 min")
        self.assertEqual(out["labels"], "Mail, x")
        self.assertEqual(out["labelsNone"], "no collectors")
        self.assertEqual(out["lineErr"], {"error": "boom"})
        self.assertEqual(out["lineNever"], {"text": "never polled"})
        self.assertEqual(out["linePolled"], {"text": "last poll 2m ago"})
        self.assertEqual(out["failOffline"], "xo-space is unreachable")
        self.assertEqual(out["fail501"], "not available for the active agent")
        self.assertEqual(out["failMsg"], "nope")
        self.assertEqual(out["failBare"], "request failed")


class InboxViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = read("js/views/inbox.js")

    def test_mark_all_seen_is_one_bulk_patch(self) -> None:
        body = slice_between(self.src, "async function markAllSeen(){", "/* Open:")
        self.assertIn("it.status==='new').map(it=>it.id)", body)
        self.assertIn("apiFetch(API_BASE+'/api/inbox',{method:'PATCH',body:{ids,status:'seen'}})", body)
        self.assertNotIn("for(const id of ids)", body)
        self.assertNotIn("'/api/inbox/'+encodeURIComponent(id)", body)
        self.assertIn("toast('mark all seen failed: '+failText(res))", body)
        self.assertIn("res.data.missing", body)
        self.assertIn("await load();", body)

    def test_every_call_carries_api_base(self) -> None:
        # no bare apiFetch('/api/...') left; the connections calls join the family
        self.assertNotRegex(self.src, r"apiFetch\('/")
        self.assertIn("apiFetch(API_BASE+'/api/connections')", self.src)
        self.assertIn("apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkit)+'/poll',{method:'POST'})", self.src)
        for m in re.finditer(r"apiFetch\(API_BASE\+'/api/connections[^\n]*", self.src):
            self.assertNotIn("headers", m.group(0))

    def test_shared_helpers_replace_the_private_copies(self) -> None:
        self.assertIn("import {API_BASE,apiFetch,failText} from '../core/api.js';", self.src)
        self.assertIn("import {esc,pills,rel,toast} from '../core/ui.js';", self.src)
        self.assertIn("import {collectorLabels,every,pollLine} from '../core/connections.js';", self.src)
        for private in ("const esc=", "function rel(", "function failText(", "function every(", "function collectorLabels("):
            self.assertNotIn(private, self.src)
        self.assertIn("pills(FILTERS,filter,'filter','Filter inbox','inb-filter')", self.src)
        self.assertIn("pills(SOURCE_PILLS,srcFilter,'src','Filter by source','inb-src')", self.src)

    def test_source_table_drives_pills_and_mapping(self) -> None:
        table = slice_between(self.src, "const SOURCES=[", "];")
        for row in ("{id:'all',label:'All',sources:[]}",
                    "{id:'issues',label:'Issues',sources:['issues']}",
                    "{id:'connections',label:'Connections',sources:['connections']}",
                    "{id:'workspace',label:'Workspace',sources:['timeline','todos']}",
                    "{id:'sharing',label:'Sharing',sources:['sharing']}",
                    "{id:'agents',label:'Agents',sources:[]}"):
            self.assertIn(row, table)
        self.assertIn("const SOURCE_PILLS=SOURCES.map(s=>[s.id,s.label]);", self.src)
        body = slice_between(self.src, "function sourceOf(it){", "}")
        self.assertIn("SOURCES.find(r=>r.sources.includes(s))", body)
        self.assertIn("return row?row.id:'agents';", body)
        self.assertNotIn("s==='issues'||s==='connections'", self.src)

    def test_poll_skips_an_unchanged_repaint_and_restores_focus(self) -> None:
        load = slice_between(self.src, "async function load(){", "/* ── painting")
        self.assertIn("if(paintKey()===painted){settle();return;}", load)
        self.assertIn("const paintKey=()=>JSON.stringify([data,filter,srcFilter", self.src)
        render = slice_between(self.src, "function render(){", "function settle(){")
        self.assertIn("const sel=focusSelector();", render)
        self.assertIn("painted=paintKey();", render)
        self.assertIn("el.focus({preventScroll:true})", render)
        self.assertIn("CSS.escape(a.dataset[k])", self.src)
        # an unchanged read still ages the relative times and re-enables Refresh
        settle = slice_between(self.src, "function settle(){", "function summary(")
        self.assertIn("syncBusy();", settle)
        self.assertIn("button[data-act=\"refresh\"]", settle)
        self.assertIn("[data-ts]", settle)
        self.assertIn('data-ts="\'+esc(it.ts)+\'"', self.src)

    def test_badge_poll_rests_while_the_view_is_shown(self) -> None:
        show = slice_between(self.src, "show(){", "hide(){")
        self.assertIn("shown=true;", show)
        self.assertIn("clearSlottedInterval('inbox-badge');", show)
        hide = slice_between(self.src, "hide(){", "};")
        self.assertIn("shown=false;", hide)
        self.assertIn("clearSlottedInterval('inbox-poll');", hide)
        self.assertIn("startBadgePoll();", hide)
        init = slice_between(self.src, "export function initInboxBadge(){", "}")
        self.assertIn("if(!shown)startBadgePoll();", init)
        self.assertIn("function startBadgePoll(){setSlottedInterval('inbox-badge'", self.src)

    def test_badge_never_rewrites_the_tab_label(self) -> None:
        paint = slice_between(self.src, "function paintBadge(n){", "export async function refreshInboxBadge(")
        self.assertNotIn("'Inbox", paint)
        self.assertNotIn("innerHTML", paint)
        self.assertIn("b.querySelector('.inb-badge')", paint)
        self.assertIn("badge.textContent=String(n);", paint)
        self.assertIn("else if(badge)badge.remove();", paint)

    def test_every_untrusted_field_is_escaped_and_links_stay_safe(self) -> None:
        for field in ("it.id", "it.status", "it.kind", "it.title", "it.project_id", "it.body", "it.ts",
                      "line.error", "line.text", "c.toolkit", "c.display_name||c.toolkit",
                      "collectorLabels(c)", "every(c.interval_s)", "failText(failed)", "failText(connsFailed)"):
            self.assertIn("esc(" + field + ")", self.src, field)
        self.assertIn("const safeUrl=u=>typeof u==='string'&&/^https?:\\/\\//i.test(u)?u:'';", self.src)
        self.assertIn('target="_blank" rel="noopener noreferrer"', self.src)

    def test_tool_pills_narrow_connections_by_toolkit(self) -> None:
        self.assertIn(
            "const toolOf=it=>sourceOf(it)==='connections'&&typeof it.kind==='string'?it.kind.split('.')[0]:'';",
            self.src,
        )
        self.assertIn("toolFilter,'tool','Filter connections by tool','inb-tools')", self.src)
        self.assertIn("button[data-tool]", self.src)
        self.assertIn("const paintKey=()=>JSON.stringify([data,filter,srcFilter,toolFilter,", self.src)
        self.assertIn("'src','tool'].filter(", self.src)
        # picking a tool repaints the loaded page, like a source pill
        set_tool = slice_between(self.src, "function setTool(k){", "/* Expanding a new item")
        self.assertIn("render();", set_tool)
        self.assertNotIn("load(", set_tool)
        self.assertNotIn("apiFetch(", set_tool)
        self.assertIn("toolFilter='all';", slice_between(self.src, "function setSource(k){", "function setTool(k){"))
        self.assertIn("esc(tool.name)", self.src)
        self.assertIn(".inb-tools{", read("css/inbox.css"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_tool_pills_behaviour_under_node(self) -> None:
        out = run_node(INBOX_TOOL_PROBE)
        self.assertEqual(out["allTitles"], ["Mail one", "Slack one", "Slack two", "Old notion", "Issue one"])
        self.assertTrue(out["toolsHiddenOnAll"])
        self.assertEqual(out["connTitles"], ["Mail one", "Slack one", "Slack two", "Old notion"])
        # polled toolkits first (a polled one with no rows still gets a pill),
        # then a toolkit only a loaded row names
        self.assertEqual(out["pills"], [["all", "All tools · 4"], ["gmail", "Gmail · 1"], ["slack", "Slack · 2"],
                                        ["googlemeet", "Google Meet · 0"], ["notion", "notion · 1"]])
        self.assertEqual(out["slackTitles"], ["Slack one", "Slack two"])
        self.assertTrue(out["slackOn"])
        self.assertTrue(out["meetEmpty"])
        self.assertTrue(out["bogusIgnored"])
        self.assertEqual(out["issueTitles"], ["Issue one"])
        self.assertTrue(out["resetToAll"])
        self.assertEqual(out["fetchedOnPick"], 0)


# Drives views/inbox.js through its delegated click listener over a DOM stub:
# the rows and the pills are read back out of the painted html. Output: one
# JSON line.
INBOX_TOOL_PROBE = r"""
const UI=process.argv[process.argv.length-1];
globalThis.location={pathname:'/space/',search:'',origin:'http://space.test'};
globalThis.CSS={escape:s=>s};
globalThis.addEventListener=()=>{};
globalThis.document={getElementById:()=>null,activeElement:null};

const ts='2026-09-13T10:00:00Z';
const row=(id,source,kind,title)=>({id,ts,source,kind,title,status:'seen',project_id:null});
const ITEMS=[row('a1','connections','gmail.inbox','Mail one'),row('a2','connections','slack.recent','Slack one'),
  row('a3','connections','slack.recent','Slack two'),row('a4','connections','notion.pages','Old notion'),
  row('a5','issues','issue.open','Issue one')];
const conn=(toolkit,display_name)=>({toolkit,display_name,configured:true,enabled:true,interval_s:300,collectors:[]});
const CONNS={poller_enabled:true,signed_in:true,
  connections:[conn('gmail','Gmail'),conn('slack','Slack'),conn('googlemeet','Google Meet')]};
const fetches=[];
globalThis.fetch=async url=>{
  const path=url.replace(/\?.*$/,'');
  fetches.push(path);
  const json=data=>({ok:true,status:200,json:async()=>data});
  if(path==='/api/inbox')return json({items:ITEMS,counts:{new:0,seen:5,done:0}});
  if(path==='/api/connections')return json(CONNS);
  throw new Error('unexpected '+url);
};

const box={innerHTML:'',querySelector:()=>null};
let listener=null;
const root={
  set innerHTML(html){box.innerHTML=html;},
  get innerHTML(){return box.innerHTML;},
  addEventListener(type,fn){listener=fn;},
  querySelector(sel){return sel==='.inb'?box:null;},
  querySelectorAll(){return[];},
};
const settle=async()=>{for(let i=0;i<25;i++)await new Promise(r=>setTimeout(r,0));};
const click=dataset=>listener({target:{closest:()=>({dataset,disabled:false})}});
const html=()=>box.innerHTML;
const titles=()=>[...html().matchAll(/<span class="inb-title">([^<]*)<\/span>/g)].map(m=>m[1]);
const toolPills=()=>[...html().matchAll(/<button type="button" data-tool="([^"]+)"[^>]*>([^<]*)<\/button>/g)]
  .map(m=>[m[1],m[2]]);

const view=(await import(UI+'/js/views/inbox.js')).default;
await view.mount(root,{switchTo:()=>{}});
await settle();
const out={};
out.allTitles=titles();
out.toolsHiddenOnAll=toolPills().length===0;
const before=fetches.length;
click({src:'connections'});await settle();
out.connTitles=titles();
out.pills=toolPills();
click({tool:'slack'});await settle();
out.slackTitles=titles();
out.slackOn=/data-tool="slack" class="is-on"/.test(html());
click({tool:'googlemeet'});await settle();
out.meetEmpty=html().includes('Nothing from Google Meet on this page.');
click({tool:'bogus'});await settle();
out.bogusIgnored=/data-tool="googlemeet" class="is-on"/.test(html());
click({src:'issues'});await settle();
out.issueTitles=titles();
click({src:'connections'});await settle();
out.resetToAll=/data-tool="all" class="is-on"/.test(html());
out.fetchedOnPick=fetches.length-before;
console.log(JSON.stringify(out));
process.exit(0);
"""


# Drives views/connectors.js through its own delegated click listener over
# a DOM stub just big enough for the Polling drawer: the grid keeps the
# painted html and a model of every drawer's form parsed out of it, the
# probe edits that model the way a person edits the form. A text pin cannot
# see evaluation order; this can. Output: one JSON line.
DRAWER_PROBE = r"""
const UI=process.argv[process.argv.length-1];
globalThis.location={pathname:'/space/',search:'',origin:'http://space.test'};
globalThis.CSS={escape:s=>s};
globalThis.addEventListener=()=>{};
globalThis.document={getElementById:()=>({textContent:'',classList:{add(){},remove(){}}})};

/* canned server */
const calls=[];
const TOOLKITS=['gmail','slack'].map((id,i)=>({id,slug:id,display_name:id,status:'ACTIVE',
  workspace_enabled:true,supports_action_prefs:true,schemes:['OAUTH2'],connected_account_id:'ca'+i}));
const conn=id=>({toolkit:id,configured:true,enabled:true,interval_s:900,collectors:['mail'],
  available_collectors:[{id:'mail',label:'Mail',default:true},{id:'cal',label:'Calendar'}],
  events_total:0,last_poll_at:null,last_error:null});
let putReply=null;
globalThis.fetch=async(url,opts={})=>{
  const method=opts.method||'GET';
  const path=url.replace(/\?.*$/,'');
  const body=opts.body?JSON.parse(opts.body):undefined;
  calls.push({method,path,body});
  const json=data=>({ok:true,status:200,json:async()=>data});
  if(path==='/xo-auth/session/self')return json({session_id:'s1'});
  if(path==='/api/connectors/composio/toolkits')return json({toolkits:TOOLKITS});
  if(/^\/api\/connectors\/composio\/[^/]+\/tools$/.test(path))return json({tools:[{slug:'send',name:'Send',enabled:true}]});
  const m=path.match(/^\/api\/connections\/([^/]+)$/);
  if(m&&method==='GET')return json(conn(m[1]));
  if(m&&method==='PUT')return json(putReply||{...conn(m[1]),...body});
  throw new Error('unexpected '+method+' '+path);
};

/* DOM stub */
function parseDrawers(html){
  const out={};
  const re=/<div class="conn-poll" id="poll-([^"]+)">([\s\S]*?)<\/article>/g;
  let m;
  while((m=re.exec(html))){
    const inner=m[2];
    const en=inner.match(/<input type="checkbox" data-poll="enabled"( checked)?>/);
    if(!en)continue; /* a drawer still loading has no form */
    const sel=inner.match(/<select data-poll="interval">([\s\S]*?)<\/select>/);
    const chosen=sel&&sel[1].match(/<option value="(\d+)" selected>/);
    const collectors={};
    const cre=/<input type="checkbox" data-poll="collector" value="([^"]+)"( checked)?>/g;
    let c;
    while((c=cre.exec(inner)))collectors[c[1]]=!!c[2];
    out[m[1]]={enabled:!!en[1],interval:chosen?chosen[1]:'',collectors};
  }
  return out;
}
const drawerEl=model=>({
  querySelector(sel){
    if(sel==='input[data-poll="enabled"]')return{get checked(){return model.enabled;}};
    if(sel==='select[data-poll="interval"]')return{get value(){return model.interval;}};
    return null;
  },
  querySelectorAll(sel){
    if(sel==='input[data-poll="collector"]:checked')
      return Object.entries(model.collectors).filter(([,on])=>on).map(([id])=>({value:id}));
    return[];
  },
});
const paints=[];
const grid={
  listeners:[],drawers:{},_html:'',
  get innerHTML(){return this._html;},
  set innerHTML(html){this._html=html;this.drawers=parseDrawers(html);paints.push(html);},
  addEventListener(type,fn){this.listeners.push(fn);},
};
const alertEl={hidden:true,innerHTML:'',className:''};
const root={
  innerHTML:'',
  querySelector(sel){
    if(sel==='#conn-grid')return grid;
    if(sel==='#conn-refresh')return{addEventListener(){}};
    if(sel==='#conn-alert')return alertEl;
    if(sel.startsWith('#err-'))return{hidden:true,textContent:''};
    if(sel.startsWith('#poll-')){const m=grid.drawers[sel.slice(6)];return m?drawerEl(m):null;}
    if(sel==='.conn-card[data-toolkit]')return grid._html.includes('conn-card')?{}:null;
    if(sel.startsWith('.conn-card[data-toolkit="'))return{};
    throw new Error('unstubbed selector '+sel);
  },
};
function click(toolkit,action){
  const card={dataset:{toolkit}};
  const button={dataset:{action},disabled:false,classList:{toggle(){}},closest:()=>card};
  grid.listeners.forEach(fn=>fn({target:{closest:sel=>sel.startsWith('input')?null:button}}));
}
const settle=async()=>{for(let i=0;i<25;i++)await new Promise(r=>setTimeout(r,0));};
const gmail=()=>grid.drawers.gmail;
const snap=d=>d?{enabled:d.enabled,interval:d.interval,cal:!!d.collectors.cal,mail:!!d.collectors.mail}:null;
const cls=id=>((grid._html.match(new RegExp('<article class="([^"]*)" data-toolkit="'+id+'"'))||[])[1]||'');
const out={};

const view=(await import(UI+'/js/views/connectors.js')).default;
await view.mount(root);
await settle();
out.cards=(grid._html.match(/<article class="conn-card/g)||[]).length;

/* open gmail's drawer: painted from the server's copy */
click('gmail','polling');await settle();
out.opened=snap(gmail());
/* the open card spans the row; both connected cards carry the footer row */
out.expandedOnOpen={gmail:cls('gmail').includes('is-expanded'),slack:cls('slack').includes('is-expanded')};
out.toolsRows=(grid._html.match(/<div class="conn-card-tools">/g)||[]).length;

/* edit the form, then repaint three times through the Actions toggle of
   the other card (open: two paints, close: one): every paint after the
   edit must still carry it */
gmail().collectors.cal=true;gmail().interval='1800';
const before=paints.length;
click('slack','actions');await settle();
click('slack','actions');await settle();
const after=paints.slice(before);
out.paintsAfterEdit=after.length;
out.editKeptOnEveryPaint=after.map(h=>/data-poll="collector" value="cal" checked/.test(h)&&/<option value="1800" selected>/.test(h));
out.afterRepaints=snap(gmail());

/* Save: the server normalises the interval; the drawer must show the
   server's copy right after, and keep showing it on the next repaint */
putReply={...conn('gmail'),interval_s:3600,collectors:['mail','cal']};
click('gmail','poll-save');await settle();
const put=calls.find(c=>c.method==='PUT');
out.putBody=put&&put.body;
out.afterSave=snap(gmail());
click('slack','actions');await settle();
out.afterSaveRepaint=snap(gmail());

/* edit again and let a repaint file it as the draft, then open the other
   toolkit's drawer (closes gmail's) and reopen gmail: the close discarded
   the draft, so the server's copy is back */
gmail().interval='300';
click('slack','actions');await settle();
out.draftHeldEdit=snap(gmail());
click('slack','polling');await settle();
out.gmailClosedBySlack=!gmail()&&!!grid.drawers.slack;
click('gmail','polling');await settle();
out.reopened=snap(gmail());
out.slackClosedByGmail=!grid.drawers.slack;

/* Hide polling discards too */
gmail().interval='300';
click('slack','actions');await settle();
click('gmail','polling');await settle();
out.hidden=!gmail();
out.collapsedOnHide=!cls('gmail').includes('is-expanded');
click('gmail','polling');await settle();
out.reopenedAfterHide=snap(gmail());

console.log(JSON.stringify(out));
process.exit(0);
"""


class ConnectorsViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = read("js/views/connectors.js")

    def test_api_js_is_imported_bare_like_every_other_view(self) -> None:
        self.assertIn("import {API_BASE,apiFetch} from '../core/api.js';", self.view)
        self.assertNotIn("core/api.js?v=", self.view)
        self.assertIn("import {esc,toast} from '../core/ui.js';", self.view)
        self.assertIn("import {pollLine} from '../core/connections.js';", self.view)
        self.assertNotIn("const esc=", self.view)
        self.assertNotIn("function rel(", self.view)
        # session.js has one importer, so its stamp stays on that import
        self.assertIn("from '../core/session.js?v=" + STAMP + "';", self.view)

    def test_every_call_carries_api_base(self) -> None:
        self.assertIn("const BASE=API_BASE+'/api/connectors/composio';", self.view)
        self.assertNotRegex(self.view, r"apiFetch\('/")
        self.assertIn("apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId))", self.view)
        self.assertIn("apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId),{method:'PUT',body})", self.view)
        self.assertIn("apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId)+'/poll',{method:'POST'})", self.view)

    def test_polling_drawer_keeps_unsaved_edits_across_repaints(self) -> None:
        self.assertIn("let pollDraft={};", self.view)
        # every grid write snapshots the open drawer BEFORE the markup is
        # built: renderCard paints the drawer from the draft
        self.assertIn(
            "function paintGrid(build,{snapshot=true}={}){\n"
            "  if(snapshot)snapshotPollDraft();\n"
            "  root.querySelector('#conn-grid').innerHTML=build();\n}",
            self.view,
        )
        self.assertIn("paintGrid(()=>toolkits.map(renderCard).join(''),opts);", self.view)
        self.assertNotRegex(self.view, r"paintGrid\('")          # no pre-built markup
        self.assertNotRegex(self.view, r"paintGrid\(toolkits")   # nor the cards built first
        self.assertNotIn("grid.innerHTML=", self.view)
        self.assertEqual(self.view.count("root.querySelector('#conn-grid').innerHTML="), 1)
        snap = slice_between(self.view, "function snapshotPollDraft(){", "}")
        self.assertIn("if(openPolling===null)return;", snap)
        self.assertIn("const form=readPollForm(openPolling);", snap)
        self.assertIn("if(form)pollDraft[openPolling]=form;", snap)
        # the drawer prefers the draft
        drawer = slice_between(self.view, "function renderPolling(t,enabled){", "function pollStatus(")
        self.assertIn("const draft=pollDraft[t.id];", drawer)
        self.assertIn("new Set(draft?draft.collectors", drawer)
        self.assertIn("const interval=draft?draft.interval_s:(Number(c.interval_s)||900);", drawer)
        self.assertIn("const collect=draft?draft.enabled:!!c.enabled;", drawer)
        self.assertIn("+(collect?' checked':'')+'> Collect into Inbox</label>'", drawer)
        # Save paints from the server's copy: the pre-save form is not re-read
        save = slice_between(self.view, "async function savePolling(", "async function pollNow(")
        self.assertLess(save.index("pollCache[toolkitId]=res.data;"), save.index("delete pollDraft[toolkitId];"))
        self.assertIn("if(openPolling===toolkitId)renderGrid({snapshot:false});", save)
        # every way of closing the drawer clears it, including another drawer opening
        toggle = slice_between(self.view, "async function togglePolling(", "async function loadPolling(")
        self.assertIn("openPolling=null;\n    delete pollDraft[toolkitId];", toggle)
        self.assertIn("closeOtherPolling(toolkitId);\n  openPolling=toolkitId;", toggle)
        other = slice_between(self.view, "function closeOtherPolling(toolkitId){", "}")
        self.assertIn("if(openPolling!==null&&openPolling!==toolkitId)delete pollDraft[openPolling];", other)
        active = slice_between(self.view, "if(status==='ACTIVE'){", "if(status==='FAILED'){")
        self.assertIn("closeOtherPolling(toolkitId);\n      openPolling=toolkitId;", active)
        self.assertIn("if(!enabled)delete pollDraft[toolkitId];", self.view)
        disconnect = slice_between(self.view, "async function disconnect(", "async function toggleDrawer(")
        self.assertIn("delete pollDraft[toolkitId];", disconnect)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_polling_drawer_behaviour_under_node(self) -> None:
        out = run_node(DRAWER_PROBE)
        self.assertEqual(out["cards"], 2)
        server = {"enabled": True, "interval": "900", "cal": False, "mail": True}
        self.assertEqual(out["opened"], server)
        self.assertEqual(out["expandedOnOpen"], {"gmail": True, "slack": False})
        self.assertEqual(out["toolsRows"], 2)
        self.assertTrue(out["collapsedOnHide"])
        # the edit survives every one of the three repaints (the paint order
        # bug flipped it on the first and third)
        self.assertEqual(out["paintsAfterEdit"], 3)
        self.assertEqual(out["editKeptOnEveryPaint"], [True, True, True])
        self.assertEqual(out["afterRepaints"], {"enabled": True, "interval": "1800", "cal": True, "mail": True})
        # Save sends the form and the drawer shows the server's normalised
        # copy, right after and on the next repaint (no draft survives Save)
        self.assertEqual(out["putBody"], {"enabled": True, "interval_s": 1800, "collectors": ["mail", "cal"]})
        saved = {"enabled": True, "interval": "3600", "cal": True, "mail": True}
        self.assertEqual(out["afterSave"], saved)
        self.assertEqual(out["afterSaveRepaint"], saved)
        # opening another toolkit's drawer closes this one and discards its
        # draft, as Hide polling does
        self.assertEqual(out["draftHeldEdit"]["interval"], "300")
        self.assertTrue(out["gmailClosedBySlack"])
        self.assertEqual(out["reopened"], saved)
        self.assertTrue(out["slackClosedByGmail"])
        self.assertTrue(out["hidden"])
        self.assertEqual(out["reopenedAfterHide"], saved)

    def test_poll_status_uses_the_shared_line(self) -> None:
        status = slice_between(self.view, "function pollStatus(toolkitId,c){", "async function togglePolling(")
        self.assertIn("const line=pollLine(c);", status)
        self.assertIn("esc(line.error)", status)
        self.assertIn("esc(cap(line.text))", status)
        self.assertNotIn("'Never polled'", status)

    def test_grid_rows_stay_even_and_an_open_drawer_spans_the_row(self) -> None:
        css = read("css/connectors.css")
        self.assertIn("grid-auto-flow:row dense;gap:12px;align-items:stretch}", css)
        self.assertIn(".conn-card{display:flex;flex-direction:column;", css)
        self.assertIn(".conn-card.is-expanded{grid-column:1 / -1;", css)
        self.assertIn(".conn-card-body{padding:0 18px;flex:1 0 auto}", css)
        self.assertIn(".conn-card-tools{", css)
        self.assertIn(".conn-poll-collectors{", css)
        card = slice_between(self.view, "function renderCard(t){", "/* ---------- polling")
        self.assertIn("const expanded=open||(polling&&connected);", card)
        self.assertIn("(expanded?' is-expanded':'')", card)
        self.assertIn("+(tools?'<div class=\"conn-card-tools\">':'')", card)
        toggle = slice_between(self.view, "async function togglePolling(", "function closeOtherPolling(")
        self.assertIn("revealCard(toolkitId);", toggle)


class SessionModuleTests(unittest.TestCase):
    def test_imports_api_bare_and_mints_through_api_base(self) -> None:
        session = read("js/core/session.js")
        self.assertIn("import {API_BASE,apiFetch} from './api.js';", session)
        self.assertNotIn("api.js?v=", session)
        self.assertIn("apiFetch(API_BASE+'/xo-auth/session/self')", session)
        self.assertNotRegex(session, r"apiFetch\('/")


class SharingViewTests(unittest.TestCase):
    def test_commit_row_has_no_dangling_separator_without_a_date(self) -> None:
        pane = read("js/views/sharing.js")
        self.assertIn("[esc(k.author),rel(k.date)].filter(Boolean).join(' · ')", pane)
        self.assertNotIn("' · '+rel(k.date)", pane)
        self.assertNotIn("const dtfmt=", pane)  # was unused
        self.assertIn("from './sharing_data.js?v=" + STAMP + "';", pane)


class ShellTests(unittest.TestCase):
    CORE_MAPPED = ("api.js", "ui.js", "connections.js")

    def test_number_hotkeys_ignore_a_focused_select(self) -> None:
        registry = read("js/core/registry.js")
        self.assertIn("if(/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName||''))return;", registry)

    def test_stamps_moved_together(self) -> None:
        app = read("js/app.js")
        for view in ("inbox", "wiki", "connectors", "sharing"):
            self.assertIn("./views/" + view + ".js?v=" + STAMP + "'", app)
        self.assertIn("./core/registry.js?v=" + STAMP + "'", app)  # registry.js changed too
        self.assertIn('src="js/app.js?v=' + STAMP + '"', read("index.html"))

    def test_import_map_stamps_the_bare_core_modules(self) -> None:
        """core/api.js and core/ui.js gained exports and are imported bare
        everywhere; StaticFiles sends no Cache-Control, so the stamp that
        makes a browser fetch them fresh (and every importer share one
        instance) is the import map in index.html, ahead of app.js."""
        html = read("index.html")
        m = re.search(r'<script type="importmap">\s*(\{.*?\})\s*</script>', html, re.S)
        self.assertIsNotNone(m, "index.html carries no import map")
        self.assertLess(m.start(), html.index('<script type="module" src="js/app.js'))
        imports = json.loads(m.group(1))["imports"]
        for name in self.CORE_MAPPED:
            self.assertEqual(imports["./js/core/" + name], "./js/core/" + name + "?v=" + STAMP, name)
        # one instance means every importer uses the bare specifier
        for path in sorted((UI / "js").rglob("*.js")):
            src = path.read_text(encoding="utf-8")
            for name in self.CORE_MAPPED:
                self.assertNotRegex(src, r"core/" + re.escape(name) + r"\?v=", str(path))
                self.assertNotRegex(src, r"from '\./" + re.escape(name) + r"\?v=", str(path))

    def test_wiki_labels_inbox_json_machine_local_and_lists_five_toolkits(self) -> None:
        wiki = read("js/views/wiki.js")
        self.assertIn("['~/.quirq/inbox.json',", wiki)
        row = wiki[wiki.index("['~/.quirq/inbox.json',"):]
        row = row[: row.index("],")]
        self.assertIn("'Machine-local .quirq file'", row)
        self.assertNotIn("Workspace .xo file", row)
        self.assertIn("# gmail, googlecalendar, notion, slack, telegram", wiki)
        self.assertIn("<code>googlecalendar</code>, <code>notion</code>, <code>slack</code>,\n            <code>telegram</code>)", wiki)

    def test_touched_files_carry_no_dashes(self) -> None:
        for rel in ("js/core/ui.js", "js/core/api.js", "js/core/connections.js", "js/core/registry.js",
                    "js/core/session.js", "js/views/inbox.js", "js/views/connectors.js",
                    "js/views/sharing.js", "js/views/sharing_data.js", "js/views/wiki.js",
                    "js/app.js", "index.html"):
            self.assertIsNone(DASHES.search(read(rel)), rel)
        self.assertIsNone(DASHES.search(Path(__file__).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
