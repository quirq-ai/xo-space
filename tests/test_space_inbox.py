"""The Inbox page (space_ui/js/views/inbox.js): the workspace's work items
joined with their sessions over GET /api/inbox. Text pins hold the seams
(registration, the one route, escaping, Open through the hash, no
client-side section map); a Node probe drives the page over a DOM stub
(tabs from the answer, entity groups, state pills, search, the badge)."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"
DASHES = "[" + chr(0x2013) + chr(0x2014) + "]"  # en dash, em dash: banned in this repo; built from code points so this file passes its own check


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class SpaceInboxCompositionTests(unittest.TestCase):
    """The Inbox tab is composed into the shell through explicit seams: one
    import and one registerView in app.js, one badge starter after the
    registry, one stylesheet link, and a view module that talks to exactly
    two route families (the inbox rows and scheduled jobs)."""

    def test_view_is_imported_and_registered_with_a_cache_buster(self) -> None:
        app = read("js/app.js")
        self.assertRegex(
            app,
            r"import \{createInboxViews,initInboxBadge\} from './views/inbox\.js\?v=\d{8}-[a-z0-9]+';",
        )
        self.assertIn("createInboxViews().forEach(registerView);", app)
        # Inbox sits between Agents and the Setup route family in the primary nav.
        self.assertLess(app.index("createAgentViews().forEach(registerView);"), app.index("createInboxViews().forEach(registerView);"))
        self.assertLess(app.index("createInboxViews().forEach(registerView);"), app.index("createSetupViews(connectionsView).forEach(registerView);"))

    def test_badge_starts_after_the_registry_in_its_own_bulkhead(self) -> None:
        app = read("js/app.js")
        self.assertIn("try{initInboxBadge();}catch(err)", app)
        # the badge paints onto #tab-inbox, which only exists once
        # startRegistry has built the tab buttons
        self.assertLess(app.index("startRegistry("), app.index("initInboxBadge();"))

    def test_stylesheet_is_linked_and_the_shell_stamp_moved(self) -> None:
        html = read("index.html")
        self.assertRegex(html, r'<link rel="stylesheet" href="css/inbox\.css\?v=\d{8}-[a-z0-9]+">')
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

    def test_module_uses_inbox_and_schedules_routes(self) -> None:
        src = read("js/views/inbox.js")
        paths = re.findall(r"API_BASE\+'([^']*)'", src)
        self.assertTrue(paths, "inbox.js makes no API calls")
        # every API_BASE+ is followed by a literal, and every literal is the
        # inbox or the jobs page it shows beside the rows
        self.assertEqual(len(paths), src.count("API_BASE+"))
        for path in paths:
            self.assertTrue(path.startswith(("/api/inbox", "/api/schedules")), path)
        self.assertIn("'/api/inbox?state=open&limit=1'", src)          # the badge
        self.assertIn("'/api/inbox?section='+encodeURIComponent(section)+'&state='+encodeURIComponent(state)+'&limit=200'", src)
        # the list page reads; every write is the item page's
        self.assertNotIn("method:'PATCH'", src)
        self.assertNotIn("method:'DELETE'", src)
        self.assertNotIn("/api/work", src)
        self.assertNotIn("/api/inbox/'", src)
        # never raw fetch: apiFetch forwards the page query and classifies failures
        self.assertNotIn("fetch(", src.replace("apiFetch(", ""))

    def test_badge_and_poll_use_slotted_intervals(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn("setSlottedInterval('inbox-badge'", src)
        self.assertIn("setSlottedInterval('inbox-poll'", src)
        self.assertIn("clearSlottedInterval('inbox-poll')", src)
        self.assertIn("export function initInboxBadge()", src)
        self.assertIn("export async function refreshInboxBadge(", src)
        # the badge is waiting plus new over the sections summary, coerced,
        # never interpolated from a server string, and is a node appended
        # beside the label the registry painted, so the label itself is
        # never rewritten here
        self.assertIn("export const needsYou=sections=>", src)
        self.assertIn("Math.max(0,Number(c.waiting)||0)+Math.max(0,Number(c.new)||0)", src)
        self.assertIn("paintBadge(needsYou(sections));", src)
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
        self.assertIn("Issues, polled apps, project shares and what agents post land here.", src)
        self.assertIn("showing the last good read", src)

    def test_rows_are_buttons_and_every_field_is_escaped(self) -> None:
        src = read("js/views/inbox.js")
        self.assertIn('<button class="inb-row-head" type="button" data-act="open" data-id="\'+esc(it.id)+\'"', src)
        for field in ("it.id", "st", "it.title||it.id", "it.entity", "outcome", "runtime", "ts", "s.id", "s.label||s.id", "g.label"):
            self.assertIn("esc(" + field + ")", src, field)
        # the state chip is one of the five states, never the server's word
        self.assertIn("const ROW_STATES=['new','running','waiting','failed','closed'];", src)
        self.assertIn("const st=ROW_STATES.includes(it.state)?it.state:(isSession?'closed':'new');", src)
        self.assertIn("white-space:nowrap", read("css/inbox.css"))

    def test_tabs_come_from_the_answer_and_open_goes_through_the_hash(self) -> None:
        src = read("js/views/inbox.js")
        # the tabs are the answer's sections; the page only orders them
        self.assertIn("const TAB_ORDER=['projects','agents','connections','issues'];", src)
        self.assertIn("data.sections", src)
        for gone in ("SECTION_OF_SOURCE", "sectionOf(", "work-sections", "space:work-item", "SOURCES=", "expanded"):
            self.assertNotIn(gone, src, gone)
        self.assertFalse((UI / "js/core/work-sections.js").exists(), "the client-side source map is gone")
        self.assertEqual([p for p in (UI / "js").rglob("*.js") if "work-sections" in p.read_text(encoding="utf-8")], [])
        # Open: the selection travels in the hash, a session row opens the transcript alone
        self.assertIn("if(it.kind==='session')switchTo('inbox/item?s='+encodeURIComponent(it.id));", src)
        self.assertIn("else switchTo('inbox/item?p='+encodeURIComponent(it.project_id||'')+'&id='+encodeURIComponent(it.id));", src)
        self.assertNotIn("dispatchEvent(new CustomEvent('space:work-item'", src)
        # the state pills and the tab strip fetch; the search only repaints
        self.assertIn("pills(STATES,state,'state','Filter by state','inb-filter')", src)
        self.assertIn("const STATES=[['open','Open'],['active','Active'],['waiting','Waiting'],['closed','Closed'],['all','All']];", src)
        self.assertIn("if(!b||b.disabled)return;\n  if(b.dataset.state){setState(b.dataset.state);return;}\n  if(b.dataset.section){setSection(b.dataset.section);return;}", src)
        self.assertIn("query=value;render();", src)

    def test_new_files_carry_no_dashes(self) -> None:
        for rel in ("js/views/inbox.js", "css/inbox.css"):
            self.assertNotRegex(read(rel), DASHES, rel)
        self.assertNotRegex(Path(__file__).read_text(encoding="utf-8"), DASHES)


PROBE = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const navigation=await import(pathToFileURL(process.cwd()+'/space_ui/js/core/navigation.js'));
class Element{
  constructor(){this.html='';this.hidden=false;this.paints=0;this.textContent='';this.children=[];this.disabled=false;}
  set innerHTML(v){this.html=v;this.paints++;}
  get innerHTML(){return this.html;}
  querySelector(){return null;}
  querySelectorAll(){return [];}
  contains(){return false;}
  appendChild(child){this.children.push(child);}
  addEventListener(type,fn){this.listeners=this.listeners||new Map();this.listeners.set(type,fn);}
  remove(){}
}
const nodes=new Map();
for(const sel of ['.inb-items-page','.inb-jobs-page','.inb-page-head h1','.inb-page-actions','.inb-jobs'])nodes.set(sel,new Element());
const root=new Element();root.querySelector=sel=>nodes.get(sel)||null;
const tab=new Element();tab.querySelector=sel=>sel==='.inb-badge'?tab.children[0]||null:null;
const requests=[],intervals=new Map(),opened=[];
const context={console,Date,Map,Set,Promise,JSON,Number,String,Array,Object,Math,API_BASE:'',
  document:{activeElement:null,getElementById:id=>id==='tab-inbox'?tab:null,createElement:()=>{const b=new Element();b.className='';return b;}},
  CSS:{escape:v=>v},location:{hash:'#/inbox/items'},
  CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},dispatchEvent(){},
  apiFetch:(path,opts)=>new Promise(resolve=>requests.push({path,opts,resolve})),
  clearSlottedInterval:name=>intervals.delete(name),setSlottedInterval:(name,fn,ms)=>intervals.set(name,{fn,ms}),
  toast(){},openCommandResults(){},describeSchedule:()=>'',describeOnce:()=>'',isScheduled:()=>false,statusText:s=>String(s),
  failText:res=>res.offline?'xo-space is unreachable':res.notImplemented?'not available for the active agent':res.error||'request failed',
  INBOX_PAGES:navigation.INBOX_PAGES,
};
vm.createContext(context);
/* the real escape, pill strip and relative time from core, so the probe
   checks what the browser paints */
vm.runInContext(fs.readFileSync('space_ui/js/core/ui.js','utf8').replace(/^import .*?;\n/gm,'').replace(/^export (const|function) /gm,'$1 '),context);
const source=fs.readFileSync('space_ui/js/views/inbox.js','utf8').replace(/^import .*?;\n/gm,'')
  .replaceAll('export async function','async function').replaceAll('export function','function').replaceAll('export const','const')
  .replace(/export default (\w+);/,'globalThis.controller=$1;');
vm.runInContext(source,context);
const evaluate=src=>vm.runInContext(src,context);
const settle=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
const items=nodes.get('.inb-items-page');
const html=()=>items.innerHTML;
const click=dataset=>root.listeners.get('click')({target:{closest:()=>({disabled:false,dataset})}});
const counts=(n=0,r=0,w=0,f=0,c=0)=>({new:n,running:r,waiting:w,failed:f,closed:c});
const answer=(section,rows,sections)=>({ok:true,data:{schema:1,generated_at:'2026-09-21T10:00:00Z',runner:{enabled:true},
  sections:sections||[
    {id:'connections',label:'Connections',counts:counts(1,0,1),entities:[{id:'gmail',label:'gmail',counts:counts(0,0,1)},{id:'googlecalendar',label:'googlecalendar',counts:counts(1)}]},
    {id:'issues',label:'Issues',counts:counts(0,0,0,1),entities:[{id:'o/r',label:'o/r',counts:counts(0,0,0,1)}]},
    {id:'agents',label:'Agents',counts:counts(0,0,0,0,2),entities:[{id:'demo',label:'demo',counts:counts(0,0,0,0,2)}]},
    {id:'projects',label:'Projects',counts:counts(0,2),entities:[{id:'xo-space',label:'xo-space',counts:counts(0,2)},{id:'quiet',label:'quiet',counts:counts()}]},
  ],rows,count:rows.length}});
const projectRows=[
  {kind:'workitem',id:'w1',project_id:'xo-space',title:'New commits <b>fetched</b>',section:'projects',entity:'xo-space',state:'running',status:'open',
    claim:{session_id:'s2',runtime:'demo',started_at:'2026-09-21T09:58:00Z',live:true},session:{session_id:'s2',runtime:'demo',attempt:1},outcome:null,updated_at:'2026-09-21T09:59:00Z'},
  {kind:'session',id:'s3',project_id:'xo-space',title:'Fix the flaky test',section:'projects',entity:'xo-space',state:'running',runtime:'demo',live:true,updated_at:'2026-09-21T09:59:30Z'},
  {kind:'workitem',id:'w9',project_id:'xo-space',title:'Stranger',section:'projects',entity:'nobody',state:'bogus',status:'open',updated_at:''},
];
const views=Array.from(evaluate('createInboxViews()'));
const page=name=>views.find(view=>view.route==='inbox/'+name);
await Promise.all(views.map(view=>view.mount(root,{switchTo:route=>opened.push(route)})));
assert.equal(requests.length,0,'mounting fetches nothing');
page('items').show();
assert.deepEqual(requests.map(r=>r.path),['/api/inbox?section=projects&state=open&limit=200'],'the page opens on the first tab in its order');
assert.equal(intervals.get('inbox-poll').ms,30000);
requests[0].resolve(answer('projects',projectRows));await settle();
/* tabs: the answer's sections in the page order, the waiting plus new count beside the label */
assert.match(html(),/data-section="projects"[^>]*aria-selected="true"/);
assert.deepEqual([...html().matchAll(/data-section="([a-z]+)"/g)].map(m=>m[1]),['projects','agents','connections','issues']);
assert.match(html(),/data-section="connections"[^>]*>Connections<b>2<\/b>/);
assert.equal(tab.children[0].textContent,'2','the badge is waiting plus new over every section');
/* groups: the entities in the answer's order, an empty one still a heading, the row's own entity last */
assert.deepEqual([...html().matchAll(/inb-group-name">([^<]+)</g)].map(m=>m[1]),['xo-space','quiet','nobody']);
assert.match(html(),/inb-group-counts">2 running</);
assert.match(html(),/inb-group-counts">nothing here</);
assert.match(html(),/inb-sum">2 running</);
/* rows: escaped title, state chip, the live session's runtime, a session row marked as one */
assert.match(html(),/New commits &lt;b&gt;fetched&lt;\/b&gt;/);
assert.ok(!html().includes('<b>fetched'),'the title is escaped');
assert.match(html(),/inb-row is-running"><button class="inb-row-head" type="button" data-act="open" data-id="w1" data-kind="workitem"/);
assert.match(html(),/data-id="w1"[\s\S]*?inb-chip is-runtime">demo</);
assert.match(html(),/inb-row is-running is-session"><button[^>]*data-id="s3" data-kind="session"/);
assert.match(html(),/data-id="s3"[\s\S]*?inb-chip">session</);
assert.match(html(),/inb-row is-new"><button[^>]*data-id="w9"/,'an unknown state paints as new');
/* Open: the selection travels in the hash; a session row opens the transcript alone */
click({act:'open',id:'w1',kind:'workitem'});click({act:'open',id:'s3',kind:'session'});click({act:'open',id:'nope',kind:'workitem'});
assert.deepEqual(opened,['inbox/item?p=xo-space&id=w1','inbox/item?s=s3']);
/* a tab fetches its section; a state pill fetches its state; the search only repaints */
click({section:'connections'});
assert.equal(requests.at(-1).path,'/api/inbox?section=connections&state=open&limit=200');
assert.match(html(),/inb-skel/,'skeletons until the tab lands');
requests.at(-1).resolve(answer('connections',[
  {kind:'workitem',id:'w2',project_id:'inbox-connections',title:'Invoice question',section:'connections',entity:'gmail',state:'waiting',status:'open',
    claim:null,session:{session_id:'s1',runtime:'demo',attempt:1},outcome:{kind:'reply_drafted',summary:'x'},updated_at:'2026-09-21T09:00:00Z'},
  {kind:'workitem',id:'w3',project_id:'inbox-connections',title:'Review meeting',section:'connections',entity:'googlecalendar',state:'new',status:'open',claim:null,session:null,outcome:null,updated_at:'2026-09-21T09:30:00Z'},
]));await settle();
assert.match(html(),/data-section="connections"[^>]*aria-selected="true"/);
assert.match(html(),/inb-sum">1 new · 1 waiting</);
assert.match(html(),/data-id="w2"[\s\S]*?inb-chip is-outcome">reply drafted</);
assert.doesNotMatch(html(),/data-id="w2"[\s\S]*?is-runtime/,'no live claim, no runtime');
click({state:'waiting'});
assert.equal(requests.at(-1).path,'/api/inbox?section=connections&state=waiting&limit=200');
requests.at(-1).resolve(answer('connections',[{kind:'workitem',id:'w2',project_id:'inbox-connections',title:'Invoice question',section:'connections',entity:'gmail',state:'waiting',status:'open',outcome:{kind:'reply_drafted'},updated_at:''}]));await settle();
assert.match(html(),/data-state="waiting" class="is-on"/);
const before=requests.length;
page('items').toolbar().search.setValue('meeting');
assert.equal(requests.length,before,'a search never fetches');
assert.match(html(),/No loaded inbox items match this search/);
page('items').toolbar().search.setValue('invoice drafted');
assert.match(html(),/1 matching of 1 loaded items in this tab/);
assert.match(html(),/data-id="w2"/);
page('items').toolbar().search.setValue('');
/* a failed poll keeps the last good read and says so; an unchanged one leaves the DOM alone */
const paints=items.paints;
intervals.get('inbox-poll').fn();requests.at(-1).resolve({ok:false,status:0,offline:true,error:'x'});await settle();
assert.match(html(),/xo-space is unreachable · showing the last good read/);
assert.match(html(),/data-id="w2"/);
intervals.get('inbox-poll').fn();requests.at(-1).resolve(answer('connections',[{kind:'workitem',id:'w2',project_id:'inbox-connections',title:'Invoice question',section:'connections',entity:'gmail',state:'waiting',status:'open',outcome:{kind:'reply_drafted'},updated_at:''}]));await settle();
const painted=items.paints;
intervals.get('inbox-poll').fn();requests.at(-1).resolve(answer('connections',[{kind:'workitem',id:'w2',project_id:'inbox-connections',title:'Invoice question',section:'connections',entity:'gmail',state:'waiting',status:'open',outcome:{kind:'reply_drafted'},updated_at:''}]));await settle();
assert.equal(items.paints,painted,'an unchanged read does not repaint');
/* nothing at all: the empty card names the state */
click({state:'closed'});requests.at(-1).resolve(answer('connections',[],[{id:'connections',label:'Connections',counts:counts(),entities:[]}]));await settle();
assert.match(html(),/Nothing closed yet\./);
assert.deepEqual([...html().matchAll(/data-section="([a-z]+)"/g)].map(m=>m[1]),['connections'],'only the sections the answer carries are tabs');
/* a tab the answer no longer carries: the page moves to the first one it does */
click({state:'open'});requests.at(-1).resolve(answer('connections',[],[{id:'issues',label:'Issues',counts:counts(0,0,0,1),entities:[]}]));await settle();
assert.equal(requests.at(-1).path,'/api/inbox?section=issues&state=open&limit=200');
/* hide stops the poll and hands the badge back to its own poll */
page('items').hide();
assert.equal(intervals.has('inbox-poll'),false);
assert.equal(intervals.get('inbox-badge').ms,60000);
console.log('ok');
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class InboxPageProbeTests(unittest.TestCase):
    def test_page_over_a_dom_stub(self) -> None:
        result = subprocess.run(["node", "--input-type=module", "-e", PROBE], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_the_badge_sums_waiting_and_new_over_every_section(self) -> None:
        script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const navigation=await import(pathToFileURL(process.cwd()+'/space_ui/js/core/navigation.js'));
const calls=[];let answer={ok:true,data:{sections:[{id:'a',counts:{new:2,waiting:1,running:9}},{id:'b',counts:{new:'3',waiting:null}},{counts:null},null]}};
const badge={textContent:'',className:'',removed:0,remove(){this.removed++;}};
const tab={children:[],querySelector(){return this.children[0]||null;},appendChild(b){this.children.push(b);}};
const context={console,Date,Map,Set,Promise,JSON,Number,String,Array,Object,Math,API_BASE:'',
  document:{activeElement:null,getElementById:id=>id==='tab-inbox'?tab:null,createElement:()=>badge},
  apiFetch:path=>{calls.push(path);return Promise.resolve(answer);},
  setSlottedInterval(){},clearSlottedInterval(){},esc:v=>String(v),pills:()=>'',rel:()=>'',toast(){},
  INBOX_PAGES:navigation.INBOX_PAGES,openCommandResults(){},describeSchedule(){},describeOnce(){},isScheduled(){},statusText(){}};
vm.createContext(context);
const source=fs.readFileSync('space_ui/js/views/inbox.js','utf8').replace(/^import .*?;\n/gm,'')
  .replaceAll('export async function','async function').replaceAll('export function','function').replaceAll('export const','const')
  .replace(/export default (\w+);/,'');
vm.runInContext(source,context);
assert.equal(vm.runInContext('needsYou([{counts:{new:2,waiting:1,running:9}},{counts:{new:"3",waiting:null}},{counts:null},null])',context),6);
assert.equal(vm.runInContext('needsYou(undefined)',context),0);
await vm.runInContext('refreshInboxBadge()',context);
assert.deepEqual(calls,['/api/inbox?state=open&limit=1']);
assert.equal(badge.textContent,'6');
answer={ok:true,data:{sections:[]}};
await vm.runInContext('refreshInboxBadge()',context);
assert.equal(badge.removed,1,'a zero removes the badge');
answer={ok:false,status:0,offline:true};
await vm.runInContext('refreshInboxBadge()',context);
assert.equal(badge.removed,1,'a failed read leaves the tab alone');
console.log('ok');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
