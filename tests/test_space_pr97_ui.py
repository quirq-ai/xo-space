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
STAMP = "20260914-accounts1"
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
        show = slice_between(self.src, "function showInboxPage(page){", "function hideInbox(")
        self.assertIn("shown=true;", show)
        self.assertIn("clearSlottedInterval('inbox-badge');", show)
        hide = slice_between(self.src, "function hideInbox(", "const skeleton=")
        self.assertIn("shown=false;", hide)
        self.assertIn("clearSlottedInterval('inbox-poll');", hide)
        self.assertIn("startBadgePoll();", hide)
        init = slice_between(self.src, "export function initInboxBadge(){", "}")
        self.assertIn("if(!shown||inboxPage!=='items')startBadgePoll();", init)
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


# Drives views/connectors.js through its own delegated click listener over
# a DOM stub just big enough for the Polling drawer: the grid keeps the
# painted html and a model of every drawer's form parsed out of it, the
# probe edits that model the way a person edits the form. A text pin cannot
# see evaluation order; this can. The prelude (server, DOM stub, helpers)
# is shared by the two scenarios below; each prints one JSON line.
PROBE_PRELUDE = r"""
const UI=process.argv[process.argv.length-1];
globalThis.location={pathname:'/space/',search:'',origin:'http://space.test'};
globalThis.CSS={escape:s=>s};
globalThis.addEventListener=()=>{};
globalThis.document={getElementById:()=>({textContent:'',classList:{add(){},remove(){}}})};

/* canned server */
const calls=[];
const TOOLKITS=['gmail','slack'].map((id,i)=>({id,slug:id,display_name:id,status:'ACTIVE',
  workspace_enabled:true,supports_action_prefs:true,schemes:['OAUTH2'],connected_account_id:'ca'+i}));
/* account labels: what the list and the per-toolkit read report, and how
   the account route answers (default: no lookup, no label) */
let accounts={};
let accountReply=id=>({toolkit:id,account_label:null,account_checked_at:null,
  error:'no account lookup for '+id+' yet',cached:false});
let accountGate=null;      /* a promise the account route awaits before answering */
const conn=id=>({toolkit:id,configured:true,enabled:true,interval_s:900,collectors:['mail'],
  available_collectors:[{id:'mail',label:'Mail',default:true},{id:'cal',label:'Calendar'}],
  events_total:0,last_poll_at:null,last_error:null,
  account_label:accounts[id]||null,account_checked_at:null});
let putReply=null;
let putFails=false;        /* the PUT throws: apiFetch answers ok:false */
let pollGate=null;         /* a promise POST /poll awaits before answering */
let prefsGate=null,connectGate=null;
globalThis.fetch=async(url,opts={})=>{
  const method=opts.method||'GET';
  const path=url.replace(/\?.*$/,'');
  const body=opts.body?JSON.parse(opts.body):undefined;
  calls.push({method,path,body,headers:opts.headers});
  const json=data=>({ok:true,status:200,json:async()=>data});
  if(path==='/xo-auth/session/self')return json({session_id:'s1'});
  if(path==='/api/connectors/composio/toolkits')return json({toolkits:TOOLKITS});
  if(/^\/api\/connectors\/composio\/[^/]+\/tools$/.test(path))return json({tools:[{slug:'send',name:'Send',enabled:true}]});
  if(/^\/api\/connectors\/composio\/[^/]+\/prefs$/.test(path)){
    if(prefsGate)await prefsGate;return json({});
  }
  if(/^\/api\/connectors\/composio\/[^/]+\/connect$/.test(path)){
    if(connectGate)await connectGate;return json({});
  }
  if(path==='/api/connections'&&method==='GET')
    return json({signed_in:true,poller_enabled:true,connections:TOOLKITS.map(t=>conn(t.id))});
  const a=path.match(/^\/api\/connections\/([^/]+)\/account$/);
  if(a&&method==='POST'){if(accountGate)await accountGate;return json(accountReply(a[1]));}
  const m=path.match(/^\/api\/connections\/([^/]+)$/);
  if(m&&method==='GET')return json(conn(m[1]));
  if(m&&method==='PUT'){if(putFails)throw new Error('boom');return json(putReply||{...conn(m[1]),...body});}
  const poll=path.match(/^\/api\/connections\/([^/]+)\/poll$/);
  if(poll&&method==='POST'){if(pollGate)await pollGate;return json({new_events:0,error:null,skipped:null});}
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
/* a paint recreates every card: the error boxes come back hidden and the
   buttons a click handed out are detached, as in a browser */
let errs={};
let cardNodes=new Map();
const buttons=[];
const grid={
  listeners:[],drawers:{},_html:'',
  get innerHTML(){return this._html;},
  set innerHTML(html){
    this._html=html;this.drawers=parseDrawers(html);paints.push(html);
    errs={};buttons.forEach(b=>{b.detached=true;});
    cardNodes=new Map();
  },
  addEventListener(type,fn){this.listeners.push(fn);},
};
/* one card of the painted html, edited in place: the facts row takes a
   chip, the open drawer's interval row takes the note after it */
const inserts=[];
function cardEl(toolkit){
  if(cardNodes.has(toolkit))return cardNodes.get(toolkit);
  const start=()=>grid._html.indexOf('data-toolkit="'+toolkit+'"');
  if(start()<0)return null;
  const inner=()=>grid._html.slice(start(),grid._html.indexOf('</article>',start()));
  const splice=(at,html)=>{grid._html=grid._html.slice(0,at)+html+grid._html.slice(at);inserts.push(html);};
  const facts={
    querySelector:sel=>sel==='.conn-account'&&/conn-account"/.test(inner())?{}:null,
    insertAdjacentHTML(where,html){
      if(where!=='beforeend')throw new Error('unstubbed insert '+where);
      const i=grid._html.indexOf('<div class="conn-facts">',start());
      splice(grid._html.indexOf('</div>',i),html);
    },
  };
  const row={
    insertAdjacentHTML(where,html){
      if(where!=='afterend')throw new Error('unstubbed insert '+where);
      const i=grid._html.indexOf('<select data-poll="interval">',start());
      splice(grid._html.indexOf('</label>',i)+'</label>'.length,html);
    },
  };
  const drawer={
    querySelector(sel){
      if(sel==='.conn-poll-account')return /conn-poll-account"/.test(inner())?{}:null;
      if(sel==='select[data-poll="interval"]')return inner().includes('<select data-poll="interval">')?{closest:()=>row}:null;
      throw new Error('unstubbed drawer selector '+sel);
    },
  };
  const card={dataset:{toolkit},hidden:false,
    querySelector(sel){
      if(sel==='.conn-facts')return facts;
      if(sel==='.conn-poll')return inner().includes('<div class="conn-poll" id="poll-'+toolkit+'"')?drawer:null;
      throw new Error('unstubbed card selector '+sel);
    },
  };
  cardNodes.set(toolkit,card);
  return card;
}
const alertEl={hidden:true,innerHTML:'',className:''};
const noMatch={hidden:true,textContent:''};
const nativeGrid={innerHTML:''};
const workspaceSection={hidden:false},accountSection={hidden:false};
const refreshBtn={listeners:[],addEventListener(type,fn){this.listeners.push(fn);}};
const refresh=()=>refreshBtn.listeners.forEach(fn=>fn());
const root={
  innerHTML:'',
  querySelector(sel){
    if(sel==='#conn-grid')return grid;
    if(sel==='#conn-native-grid')return nativeGrid;
    if(sel==='#conn-workspace-section')return workspaceSection;
    if(sel==='#conn-account-section')return accountSection;
    if(sel==='#conn-refresh')return refreshBtn;
    if(sel==='#conn-alert')return alertEl;
    if(sel==='#conn-no-match')return noMatch;
    if(sel.startsWith('#err-')){const id=sel.slice(5);return errs[id]||(errs[id]={hidden:true,textContent:''});}
    if(sel.startsWith('#poll-')){const m=grid.drawers[sel.slice(6)];return m?drawerEl(m):null;}
    if(sel==='.conn-card[data-toolkit]')return grid._html.includes('conn-card')?{}:null;
    const card=sel.match(/^\.conn-card\[data-toolkit="([^"]+)"\]$/);
    if(card)return cardEl(card[1]);
    throw new Error('unstubbed selector '+sel);
  },
  querySelectorAll(sel){
    if(sel==='.conn-card[data-toolkit]')
      return [...grid._html.matchAll(/<article[^>]*data-toolkit="([^"]+)"/g)].map(m=>cardEl(m[1]));
    throw new Error('unstubbed all selector '+sel);
  },
};
function click(toolkit,action){
  const card={dataset:{toolkit}};
  const button={dataset:{action},disabled:false,detached:false,classList:{toggle(){}},closest:()=>card};
  buttons.push(button);
  grid.listeners.forEach(fn=>fn({target:{closest:sel=>sel.startsWith('input')?null:button}}));
  return button;
}
const settle=async()=>{for(let i=0;i<25;i++)await new Promise(r=>setTimeout(r,0));};
const gmail=()=>grid.drawers.gmail;
const snap=d=>d?{enabled:d.enabled,interval:d.interval,cal:!!d.collectors.cal,mail:!!d.collectors.mail}:null;
const out={};

/* These probes exercise the account-app controller, including its real API,
   session and polling code. Native integrations have independent browser
   coverage; a no-op child keeps this intentionally small DOM stub focused. */
const fs=await import('node:fs/promises');
const connectorURL=new URL(UI+'/js/views/connectors.js');
const connectorSource=(await fs.readFile(connectorURL,'utf8'))
  .replace(/^import \{mountNativeConnectors\} from .*?;$/m,
    'const mountNativeConnectors=()=>({refresh:async()=>{},setFilter:()=>({total:0,shown:0})});')
  .replace(/from '([^']+)'/g,(_match,specifier)=>"from '"+new URL(specifier,connectorURL).href+"'");
const view=(await import('data:text/javascript;base64,'+Buffer.from(connectorSource).toString('base64'))).default;
"""

DRAWER_PROBE = PROBE_PRELUDE + r"""
await view.mount(root);
await settle();
out.cards=(grid._html.match(/<article class="conn-card/g)||[]).length;

/* open gmail's drawer: painted from the server's copy */
click('gmail','polling');await settle();
out.opened=snap(gmail());

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
click('gmail','polling');await settle();
out.reopenedAfterHide=snap(gmail());

console.log(JSON.stringify(out));
process.exit(0);
"""

# The account label on the Connectors card: one list read per load, one
# account lookup per load for a connected toolkit turned on here that the
# read left unlabelled, a repaint (through the draft-keeping paint) when a
# label arrives, nothing at all when the lookup answers without one.
ACCOUNT_PROBE = PROBE_PRELUDE + r"""
const listReads=()=>calls.filter(c=>c.method==='GET'&&c.path==='/api/connections').length;
const asks=()=>calls.filter(c=>c.method==='POST'&&/\/account$/.test(c.path)).map(c=>c.path);
const chip=label=>new RegExp('conn-fact conn-account" title="the account this workspace uses">'+label+'<').test(grid._html);
const chips=()=>(grid._html.match(/conn-account"/g)||[]).length;

/* mount: the list labels gmail only; slack (connected, on here) is asked
   once and the answer carries a label, so the grid repaints with it */
accounts={gmail:'dev@example.com'};
accountReply=id=>({toolkit:id,account_label:id==='slack'?'ops@example.com':null,
  account_checked_at:'2026-09-14T00:00:00Z',error:null,cached:false});
await view.mount(root);
await settle();
out.listReadsAfterMount=listReads();
out.asksAfterMount=asks();
out.gmailChip=chip('dev@example.com');
out.slackChip=chip('ops@example.com');
out.headersOnAccountCalls=calls.some(c=>/\/api\/connections/.test(c.path)&&c.headers!==undefined);

/* Refresh with the list now labelling both: one more read, nobody asked */
accounts={gmail:'dev@example.com',slack:'ops@example.com'};
refresh();await settle();
out.listReadsAfterRefresh=listReads();
out.asksAfterRefresh=asks().length;
out.chipsAfterRefresh=chips();

/* the list forgets slack and the lookup answers with an error and no
   label: asked once more, and the card is left as it is (no chip, and
   no paint beyond the load's own) */
accounts={gmail:'dev@example.com'};
accountReply=id=>({toolkit:id,account_label:null,account_checked_at:null,
  error:'no account lookup for '+id+' yet',cached:true});
const before=paints.length;
refresh();await settle();
out.asksAfterFailure=asks().length;
out.paintsOnFailure=paints.length-before;
out.slackChipAfterFailure=chip('ops@example.com');
out.gmailChipAfterFailure=chip('dev@example.com');

/* an unsaved edit in gmail's drawer survives the repaint a label causes,
   and the drawer names the account it polls as */
click('gmail','polling');await settle();
out.drawerNote=/<p class="conn-poll-note conn-poll-account">Polling as dev@example.com<\/p>/.test(grid._html);
gmail().collectors.cal=true;gmail().interval='1800';
accountReply=id=>({toolkit:id,account_label:'ops@example.com',account_checked_at:'2026-09-14T00:00:00Z',error:null,cached:false});
refresh();await settle();
out.slackChipAfterLabel=chip('ops@example.com');
out.editKept=snap(gmail());

/* one request in flight per toolkit: two loads while the lookup hangs ask
   once; the chip lands when it answers */
accounts={gmail:'dev@example.com'};
let release;accountGate=new Promise(r=>{release=r;});
const asked=asks().length;
refresh();await settle();
refresh();await settle();
out.asksWhileHanging=asks().length-asked;
out.slackChipWhileHanging=chip('ops@example.com');
release();accountGate=null;await settle();
out.slackChipAfterRelease=chip('ops@example.com');

/* a label that lands while the page is mid-action: gmail's Save just
   failed (its card error is showing) and slack's Poll now is still in
   flight (its button is busy). The chip and, for slack's open drawer, the
   note land in place: no paint, the error stays, the button stays busy
   and attached for the setBusy(false) that follows. */
accountGate=new Promise(r=>{release=r;});
refresh();await settle();
click('slack','polling');await settle();           /* gmail's drawer closes, slack's opens */
click('gmail','polling');await settle();           /* and back: gmail's open for the Save */
click('slack','polling');await settle();
out.bothDrawers=!!gmail()&&!!grid.drawers.slack;   /* one at a time: false */
click('gmail','polling');await settle();
putFails=true;
click('gmail','poll-save');await settle();
putFails=false;
out.errorShown=!errs.gmail.hidden&&errs.gmail.textContent==='boom';
pollGate=new Promise(r=>{out.releasePoll=r;});
const pollBtn=click('slack','poll-now');await settle();
out.pollBusy=pollBtn.disabled;
const paintsBefore=paints.length;
release();accountGate=null;await settle();
out.paintsOnLabel=paints.length-paintsBefore;
out.chipLandedInPlace=chip('ops@example.com');
out.errorKept=!!errs.gmail&&!errs.gmail.hidden&&errs.gmail.textContent==='boom';
out.pollStillBusyAndAttached=pollBtn.disabled&&!pollBtn.detached;
out.inserts=inserts.length;
const releasePoll=out.releasePoll;delete out.releasePoll;
releasePoll();pollGate=null;await settle();
out.pollReleased=!pollBtn.disabled;
/* the same lookup, landing while that toolkit's own drawer is open, adds
   the note in place too; a second label for a card that has one adds nothing */
accountGate=new Promise(r=>{release=r;});
refresh();await settle();
click('slack','polling');await settle();
out.noteBeforeLabel=/conn-poll-account/.test(grid._html);
const paintsBeforeNote=paints.length;
release();accountGate=null;await settle();
out.paintsOnNote=paints.length-paintsBeforeNote;
out.noteLandedInPlace=/<\/label><p class="conn-poll-note conn-poll-account">Polling as ops@example.com<\/p>/.test(grid._html);
out.chipsAfterNote=chips();
click('slack','polling');await settle();           /* closed again for the scenario below */

/* a hostile label is escaped on the way into the chip (from the list and
   from the lookup) and into the drawer note (slack's own read has none,
   so the note falls back to the looked-up label) */
accounts={gmail:'<b>x</b>&y'};
accountReply=id=>({toolkit:id,account_label:'<i>z</i>',account_checked_at:null,error:null,cached:false});
refresh();await settle();
out.escapedChips=grid._html.includes('>&lt;b&gt;x&lt;/b&gt;&amp;y<')&&grid._html.includes('>&lt;i&gt;z&lt;/i&gt;<')
  &&!grid._html.includes('<b>x</b>')&&!grid._html.includes('<i>z</i>');
click('slack','polling');await settle();
out.escapedNote=/Polling as &lt;i&gt;z&lt;\/i&gt;<\/p>/.test(grid._html);

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
            "  root.querySelector('#conn-grid').innerHTML=build();\n"
            "  applyFilter();\n}",
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


class AccountLabelTests(unittest.TestCase):
    """The connected account name (spec: account_label on every entry of
    GET /api/connections, POST /api/connections/{toolkit}/account to resolve
    it): two pure helpers in core, a span in the Inbox row, a chip on the
    Connectors card, and a note in the Polling drawer."""

    def setUp(self) -> None:
        self.core = read("js/core/connections.js")
        self.inbox = read("js/views/inbox.js")
        self.view = read("js/views/connectors.js")

    def test_core_exports_the_two_pure_helpers(self) -> None:
        self.assertIn("export function accountLabel(c){", self.core)
        self.assertIn("export function accountLine(c){", self.core)
        self.assertIn("account_label", self.core[: self.core.index("import ")])  # documented in the header
        self.assertNotIn("&lt;", self.core)  # no escaping here: the views escape

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_helpers_behave_under_node(self) -> None:
        script = """
          globalThis.location={pathname:'/space/',search:''};
          const conn=await import(process.argv[1]+'/js/core/connections.js');
          console.log(JSON.stringify({
            label:conn.accountLabel({account_label:'dev@example.com'}),
            labelNull:conn.accountLabel({account_label:null}),
            labelMissing:conn.accountLabel({}),
            labelNoEntry:conn.accountLabel(undefined),
            labelNumber:conn.accountLabel({account_label:7}),
            line:conn.accountLine({account_label:'dev@example.com'}),
            lineNull:conn.accountLine({account_label:null}),
            lineNoEntry:conn.accountLine(null),
          }));
        """
        out = run_node(script)
        self.assertEqual(out["label"], "dev@example.com")
        for key in ("labelNull", "labelMissing", "labelNoEntry", "labelNumber", "lineNull", "lineNoEntry"):
            self.assertEqual(out[key], "", key)
        self.assertEqual(out["line"], "as dev@example.com")

    def test_inbox_row_names_the_account_beside_the_toolkit(self) -> None:
        self.assertIn("import {accountLabel} from '../core/connections.js';", self.inbox)
        row = slice_between(self.inbox, "function connRowHTML(c){", "/* one delegated listener")
        self.assertIn("const acct=accountLabel(c);", row)
        self.assertIn("'<b>'+esc(c.display_name||c.toolkit)\n      +(acct?'<span class=\"inb-conn-acct\">'+esc(acct)+'</span>':'')+'</b>'", row)
        css = read("css/inbox.css")
        self.assertIn(".inb-conn-acct{", css)
        rule = css[css.index(".inb-conn-acct{"):]
        rule = rule[: rule.index("}")]
        for prop in ("color:var(--ink-3)", "text-overflow:ellipsis", "max-width:"):
            self.assertIn(prop, rule)

    def test_card_chip_and_drawer_note(self) -> None:
        self.assertIn("import {accountLabel,accountLine} from '../core/connections.js';", self.view)
        self.assertIn("let accountCache={};", self.view)
        card = slice_between(self.view, "function renderCard(t){", "/* ---------- polling")
        self.assertIn("const acct=accountLabel(accountCache[t.id]);", card)
        self.assertIn(
            "+(connected&&acct\n"
            "          ?'<span class=\"conn-fact conn-account\" title=\"the account this workspace uses\">'+esc(acct)+'</span>'\n"
            "          :'')",
            card,
        )
        # the "N accounts" chip stays
        self.assertIn("+(t.account_count>1?'<span class=\"conn-fact\">'+t.account_count+' accounts</span>':'')", card)
        drawer = slice_between(self.view, "function renderPolling(t,enabled){", "function pollStatus(")
        self.assertIn("const acct=accountLine(c)||accountLine(accountCache[t.id]);", drawer)
        self.assertIn("+(acct?'<p class=\"conn-poll-note conn-poll-account\">Polling '+esc(acct)+'</p>':'')", drawer)
        # above the collectors
        self.assertLess(drawer.index("conn-poll-account"), drawer.index("+available.map(a=>"))
        css = read("css/connectors.css")
        self.assertIn(".conn-account{text-transform:none;", css)
        self.assertIn(".conn-poll-account{", css)

    def test_one_list_read_per_load_and_one_lookup_per_toolkit(self) -> None:
        load = slice_between(self.view, "async function loadAll(){", "function renderSignedOut(){")
        self.assertIn("accountAsked=new Set();\n    const accounts=loadAccounts();", load)
        self.assertIn("await accounts;\n    renderGrid();\n    askAccounts();", load)
        self.assertLess(load.index("const accounts=loadAccounts();"), load.index("apiFetch(BASE+'/toolkits'"))
        self.assertIn("const accountInFlight=new Set();", self.view)
        accounts = slice_between(self.view, "/* ---------- accounts ---------- */", "function renderActions(")
        self.assertIn("const path=API_BASE+'/api/connections';\n  const res=await apiFetch(path);", accounts)
        self.assertIn(
            "const path=API_BASE+'/api/connections/'+encodeURIComponent(toolkitId)+'/account';\n"
            "    const res=await apiFetch(path,{method:'POST'});",
            accounts,
        )
        self.assertNotIn("headers", accounts)  # workspace-local routes: no session header
        self.assertNotIn("renderCard", accounts)  # never a request per card
        ask = slice_between(accounts, "function askAccounts(){", "async function resolveAccount(")
        self.assertIn("if(!isConnected(t)||!isEnabledHere(t)||accountLabel(accountCache[t.id]))continue;", ask)
        self.assertIn("if(accountAsked.has(t.id)||accountInFlight.has(t.id))continue;", ask)
        resolve = accounts[accounts.index("async function resolveAccount("):]
        self.assertIn("if(!label)return;", resolve)  # an error with no label leaves the card as is
        self.assertNotIn("resolveAccount(", resolve[resolve.index("{"):])  # no retry
        # the label lands in place: a grid repaint at an unscheduled moment
        # would hide a card error just shown and re-enable a busy button
        self.assertIn("paintAccount(toolkitId,label);", resolve)
        self.assertNotIn("renderGrid(", resolve)
        self.assertNotIn("paintGrid(", resolve)
        self.assertIn("accountInFlight.delete(toolkitId);", resolve)
        paint = resolve[resolve.index("function paintAccount(toolkitId,label){"):]
        self.assertIn("if(!toolkit||!isConnected(toolkit))return;", paint)
        self.assertIn("root.querySelector('.conn-card[data-toolkit=\"'+CSS.escape(toolkitId)+'\"]')", paint)
        self.assertIn("if(facts&&!facts.querySelector('.conn-account'))", paint)
        self.assertIn("'<span class=\"conn-fact conn-account\" title=\"the account this workspace uses\">'+esc(label)+'</span>'", paint)
        self.assertIn("if(!drawer||drawer.querySelector('.conn-poll-account'))return;", paint)
        self.assertIn("'<p class=\"conn-poll-note conn-poll-account\">Polling '+esc(accountLine(accountCache[toolkitId]))+'</p>'", paint)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_account_behaviour_under_node(self) -> None:
        out = run_node(ACCOUNT_PROBE)
        self.assertEqual(out["listReadsAfterMount"], 1)
        self.assertEqual(out["asksAfterMount"], ["/api/connections/slack/account"])
        self.assertTrue(out["gmailChip"])
        self.assertTrue(out["slackChip"])
        self.assertFalse(out["headersOnAccountCalls"])
        self.assertEqual(out["listReadsAfterRefresh"], 2)
        self.assertEqual(out["asksAfterRefresh"], 1)
        self.assertEqual(out["chipsAfterRefresh"], 2)
        self.assertEqual(out["asksAfterFailure"], 2)
        self.assertEqual(out["paintsOnFailure"], 1)
        self.assertFalse(out["slackChipAfterFailure"])
        self.assertTrue(out["gmailChipAfterFailure"])
        self.assertTrue(out["drawerNote"])
        self.assertTrue(out["slackChipAfterLabel"])
        self.assertEqual(out["editKept"], {"enabled": True, "interval": "1800", "cal": True, "mail": True})
        self.assertEqual(out["asksWhileHanging"], 1)
        self.assertFalse(out["slackChipWhileHanging"])
        self.assertTrue(out["slackChipAfterRelease"])
        # a label landing mid-action paints nothing: the card error a failed
        # Save just showed stays, the in-flight Poll now button stays busy
        # and attached, the chip and the drawer note land in place
        self.assertFalse(out["bothDrawers"])
        self.assertTrue(out["errorShown"])
        self.assertTrue(out["pollBusy"])
        self.assertEqual(out["paintsOnLabel"], 0)
        self.assertTrue(out["chipLandedInPlace"])
        self.assertTrue(out["errorKept"])
        self.assertTrue(out["pollStillBusyAndAttached"])
        self.assertTrue(out["pollReleased"])
        self.assertFalse(out["noteBeforeLabel"])
        self.assertEqual(out["paintsOnNote"], 0)
        self.assertTrue(out["noteLandedInPlace"])
        self.assertEqual(out["chipsAfterNote"], 2)
        self.assertTrue(out["escapedChips"])
        self.assertTrue(out["escapedNote"])

    def test_touched_stylesheets_carry_no_dashes(self) -> None:
        for rel in ("css/inbox.css", "css/connectors.css"):
            self.assertIsNone(DASHES.search(read(rel)), rel)


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
        self.assertIn("from './sharing_data.js?v=20260914-projectmanage1';", pane)


class ShellTests(unittest.TestCase):
    CORE_MAPPED = ("api.js", "ui.js", "connections.js")

    def test_number_hotkeys_ignore_a_focused_select(self) -> None:
        registry = read("js/core/registry.js")
        self.assertIn("if(/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName||''))return;", registry)

    def test_stamps_moved_together(self) -> None:
        app = read("js/app.js")
        # The shared routing vocabulary, all participating views and shell
        # imports advance together; unchanged controllers retain their URLs.
        navigation_stamp = "20260914-navigation1"
        for view in ("sharing", "inbox", "wiki", "quirq", "setup", "projects", "tree", "atlas", "sessions"):
            self.assertIn("./views/" + view + ".js?v=" + navigation_stamp + "'", app)
        for module in ("registry", "navigation", "section-nav", "preview"):
            self.assertIn("./core/" + module + ".js?v=" + navigation_stamp + "'", app)
        self.assertIn("./core/toolbar.js?v=20260914-context1'", app)
        self.assertIn("./views/connectors.js?v=20260914-setupapps1'", app)
        results_stamp = "20260914-navigation1"
        html = read("index.html")
        self.assertIn('href="css/projects.css?v=20260914-navigation1"', html)
        self.assertIn('href="css/navigation.css?v=20260914-navigation1"', html)
        # Later view changes legitimately advance the shell and Wiki stamps;
        # test_space_wiki checks that the cache-bust chain stays intact.
        self.assertRegex(html, r'src="js/app\.js\?v=\d{8}-[a-z0-9]+"')
        # Inbox's Jobs/results styles advanced with its view; the connector
        # stylesheet advances for the embedded Setup section.
        for sheet, stamp in (("inbox", results_stamp), ("connectors", "20260914-setupapps1")):
            self.assertIn('<link rel="stylesheet" href="css/' + sheet + '.css?v=' + stamp + '">', html)

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

    def test_touched_files_carry_no_dashes(self) -> None:
        for rel in ("js/core/ui.js", "js/core/api.js", "js/core/connections.js", "js/core/registry.js",
                    "js/core/session.js", "js/views/inbox.js", "js/views/connectors.js",
                    "js/views/sharing.js", "js/views/sharing_data.js", "js/views/wiki.js",
                    "js/app.js", "index.html"):
            self.assertIsNone(DASHES.search(read(rel)), rel)
        self.assertIsNone(DASHES.search(Path(__file__).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
