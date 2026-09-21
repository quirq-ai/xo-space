"""The item page (space_ui/js/views/work-item.js): one Inbox work item, the
fact, the transcript of its latest session and the outcome on the left, the
chat and the actions on the right, over the Inbox detail route and the
session transcript route. Text pins hold the seams (registration, the two
route families, escaping, the hash selection, the outcome strip); a Node
probe drives the lifecycle over a DOM stub."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"
DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")  # en dash, em dash: banned; built from code points


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class CompositionTests(unittest.TestCase):
    def test_registered_with_a_stamp_beside_the_inbox_pages(self) -> None:
        app = read("js/app.js")
        self.assertRegex(app, r"import workItemView from './views/work-item\.js\?v=\d{8}-[a-z0-9]+';")
        self.assertIn("registerView(workItemView);", app)
        self.assertLess(app.index("createInboxViews().forEach(registerView);"), app.index("registerView(workItemView);"))
        self.assertRegex(read("index.html"), r'<link rel="stylesheet" href="css/work-item\.css\?v=\d{8}-[a-z0-9]+">')
        nav = read("js/core/navigation.js")
        self.assertIn("export const INBOX_ITEM_PAGE=Object.freeze({id:'inbox-item',route:'inbox/item',label:'Item',aliases:Object.freeze([]),parent:'inbox',nav:false,section:'inbox-item'});", nav)
        self.assertNotIn("'inbox-item','item'", nav, "no link of its own in the Work section nav")
        # the section nav keeps the Work links on screen for it
        self.assertIn("INBOX_ITEM_PAGE].map(page=>[page.id,page])", read("js/core/section-nav.js"))

    def test_the_view_talks_to_the_item_and_transcript_routes_only_and_escapes_everything(self) -> None:
        src = read("js/views/work-item.js")
        self.assertIn("import {API_BASE,apiFetch,failText} from '../core/api.js';", src)
        self.assertIn("import {esc,rel,toast} from '../core/ui.js';", src)
        self.assertIn("const itemPath=(sel,suffix='')=>API_BASE+'/api/inbox/'+encodeURIComponent(sel.project)+'/'+encodeURIComponent(sel.id)+suffix;", src)
        self.assertIn("const transcriptPath=id=>API_BASE+'/api/sessions/'+encodeURIComponent(id)+'/transcript';", src)
        self.assertNotRegex(src, r"apiFetch\('/")
        self.assertNotIn("fetch(", src.replace("apiFetch(", ""))
        self.assertNotIn("/api/work", src)
        for call in ("apiFetch(itemPath(sel))", "apiFetch(transcriptPath(id))",
                     "apiFetch(itemPath(selected,'/reply'),{method:'POST',body:{text}})",
                     "apiFetch(itemPath(selected,suffix),body===undefined?{method:'POST'}:{method:'POST',body})",
                     "act('/start'+(retry?'?retry=true':''),undefined,retry?'retry':'start')",
                     "act('/send',undefined,'send the draft')", "act('/archive',{},'archive')", "act('/reopen',undefined,'reopen')"):
            self.assertIn(call, src, call)
        for field in ("f.title||it.title||selected.id", "f.body", "text", "who", "o.summary", "o.question", "o.draft", "task.title",
                      "acted.join('; ')", "failText(failed)", "failText(transcriptFailed)", "titleOf()", "bits.join(' · ')",
                      "runtime||'The agent'", "placeholder", "dtfmt(f.ts)", "rel(f.ts)", "rel(o.at)"):
            self.assertIn("esc(" + field + ")", src, field)
        # the chips escape what they are handed
        self.assertIn("""function chip(text,cls=''){return'<span class="wi-chip'+(cls?' '+cls:'')+'">'+esc(text)+'</span>';}""", src)
        for chipped in ("chip(it.section||f.section)", "chip(it.entity||f.entity)", "chip(f.kind)", "chip(it.project_id)", "chip(OUTCOME_LABEL[o.kind]||o.kind,'is-outcome')"):
            self.assertIn(chipped, src, chipped)
        self.assertIn("const safeUrl=u=>typeof u==='string'&&/^https?:\\/\\//i.test(u)?u:'';", src)
        self.assertIn('target="_blank" rel="noopener noreferrer"', src)
        # the selection is the hash query, read on every show; polling is slotted and stops on hide
        self.assertIn("export function readSelection(hash){", src)
        self.assertIn("const next=readSelection(location.hash);", src)
        self.assertNotIn("space:work-item", src)
        self.assertIn("setSlottedInterval('work-item-poll',load,data&&data.running?RUNNING_POLL_MS:IDLE_POLL_MS);", src)
        self.assertIn("hide(){shown=false;clearSlottedInterval('work-item-poll');token++;},", src)
        self.assertIn("const RUNNING_POLL_MS=2000,IDLE_POLL_MS=15000;", src)
        # the trailing outcome block leaves an assistant bubble, nothing else does
        self.assertIn("export const stripOutcome=s=>String(s??'').replace(/\\s*```json[^`]*```\\s*$/,'');", src)
        self.assertIn("const text=role==='assistant'?stripOutcome(m.content):String(m&&m.content||'');", src)
        self.assertIsNone(DASHES.search(src))
        self.assertIsNone(DASHES.search(read("css/work-item.css")))
        self.assertIsNone(DASHES.search(Path(__file__).read_text(encoding="utf-8")))

    def test_the_link_helper_is_shared_and_the_inbox_no_longer_hands_items_over(self) -> None:
        self.assertIn("import {hasLink,openItemLink} from '../core/item-links.js?v=", read("js/views/work-item.js"))
        links = read("js/core/item-links.js")
        self.assertIn(
            "switchTo('projects/data/list');\n"
            "    dispatchEvent(new CustomEvent('space:preview-file',{detail:{project,path:l.path}}));",
            links,
        )
        inbox = read("js/views/inbox.js")
        self.assertNotIn("item-links", inbox)
        self.assertNotIn("space:work-item", inbox)
        self.assertNotIn("/api/work", inbox)


PROBE = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const navigation=await import(pathToFileURL(process.cwd()+'/space_ui/js/core/navigation.js'));
const links=await import(pathToFileURL(process.cwd()+'/space_ui/js/core/item-links.js'));
class Element{
  constructor(){this.html='';this.nodes=new Map();this.listeners=new Map();this.value='';this.disabled=false;this.paints=0;}
  set innerHTML(v){this.html=v;this.paints++;}
  get innerHTML(){return this.html;}
  querySelector(sel){if(sel==='.wi-input')return this.html.includes('wi-input')?this.input():null;return null;}
  input(){if(!this.nodes.has('input')){const n=new Element();n.classList={contains:c=>c==='wi-input'};this.nodes.set('input',n);}const n=this.nodes.get('input');n.disabled=this.html.includes('class="wi-input"')&&/wi-input" rows="5"[^>]*disabled/.test(this.html);return n;}
  addEventListener(type,fn){this.listeners.set(type,fn);}
  focus(){}
}
const intervals=new Map(),calls=[],toasts=[],opened=[];
let reply={ok:true,data:{session_id:'s1'}};
const detail=(over={})=>({kind:'workitem',id:'aaaa0001',project_id:'inbox-connections',pid:'p1',title:'Invoice <b>question</b>',
  section:'connections',entity:'gmail',state:'waiting',status:'open',state_reason:null,assignee:null,
  source:{kind:'connection',key:'connection:gmail:unread:1',connection:{toolkit:'gmail',type:'unread',event:'1'}},
  fact:{ts:'2026-09-21T10:00:00Z',kind:'gmail.unread',url:'https://mail.example.invalid/1',link:{view:'projects'},toolkit:'gmail',
    title:'Invoice <b>question</b>',body:'<script>alert(1)</script> 12 seats',key:'connection:gmail:unread:1',section:'connections',entity:'gmail'},
  claim:null,session:{session_id:'s1',native_session_id:'n1',runtime:'demo',attempt:1,started_at:'2026-09-21T10:01:00Z',ended_at:'2026-09-21T10:05:00Z',exit:{status:'ok',message:null}},
  outcome:{kind:'reply_drafted',summary:'A seat-count fix',draft:'reply.md',task:null,question:null,acted:[],at:'2026-09-21T10:05:00Z'},
  sessions:[{id:'s1',native_id:'n1',runtime:'demo',title:'Invoice',updated_at:'2026-09-21T10:05:00Z',live:false}],
  transcript:{session_id:'s1',native_session_id:'n1'},policy:{sessions:{mode:'auto',act:true}},
  running:false,can_reply:true,can_send:true,created_at:'2026-09-21T10:00:00Z',updated_at:'2026-09-21T10:05:00Z',...over});
const transcript=(over={})=>({title:'Invoice question',messages:[
  {id:'m1',role:'user',content:'Handle the invoice mail'},
  {id:'m2',role:'assistant',content:'Drafted a reply in reply.md <i>x</i>\n\n```json\n{"outcome": "reply_drafted", "draft": "reply.md"}\n```'},
  {id:'m3',role:'person',content:'Shorter'},
],...over});
let answers={detail:()=>({ok:true,data:detail()}),transcript:()=>({ok:true,data:transcript()})};
const context={console,Date,Map,Set,Promise,JSON,Number,String,Array,Object,URLSearchParams,API_BASE:'',
  location:{hash:'#/inbox/item'},
  apiFetch:(path,opts)=>{calls.push({path,opts});
    if(opts&&opts.method==='POST')return Promise.resolve(reply);
    return Promise.resolve(path.includes('/transcript')?answers.transcript(path):answers.detail(path));},
  failText:res=>res.error||'failed',esc:v=>String(v??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
  rel:()=>'just now',toast:t=>toasts.push(t),
  setSlottedInterval:(name,fn,ms)=>intervals.set(name,{fn,ms}),clearSlottedInterval:name=>intervals.delete(name),
  hasLink:links.hasLink,openItemLink:(go,it)=>{opened.push(it);return true;},INBOX_ITEM_PAGE:navigation.INBOX_ITEM_PAGE,
  document:{activeElement:null},
};
vm.createContext(context);
const source=fs.readFileSync('space_ui/js/views/work-item.js','utf8').replace(/^import .*?;\n/gm,'')
  .replaceAll('export function','function').replaceAll('export const','const').replace(/export default \{/,'globalThis.view={');
vm.runInContext(source,context);
const view=context.view,root=new Element();
const settle=async()=>{for(let i=0;i<16;i++)await Promise.resolve();};
const click=act=>root.listeners.get('click')({target:{closest:()=>({disabled:false,dataset:{act}})}});
const posts=from=>calls.slice(from).filter(c=>c.opts&&c.opts.method==='POST');
const reads=from=>calls.slice(from).filter(c=>!(c.opts&&c.opts.method)).map(c=>c.path);
/* the pure helpers */
/* (JSON, not deepEqual: the vm realm's objects carry another Object prototype) */
const selection=hash=>JSON.stringify(vm.runInContext('readSelection('+JSON.stringify(hash??null)+')',context));
assert.equal(selection('#/inbox/item?p=xo-space&id=w1'),JSON.stringify({project:'xo-space',id:'w1'}));
assert.equal(selection('#/inbox/item?s=s%201'),JSON.stringify({session:'s 1'}));
assert.equal(selection('#/inbox/item?s=s9&p=x&id=y'),JSON.stringify({session:'s9'}),'a session wins over a half selection');
for(const hash of ['#/inbox/item','#/inbox/item?p=only','#/inbox/item?id=only','',undefined])assert.equal(vm.runInContext('readSelection('+JSON.stringify(hash??null)+')',context),null,String(hash));
assert.equal(vm.runInContext("stripOutcome('Done.\\n\\n```json\\n{\"outcome\": \"handled\"}\\n```\\n')",context),'Done.');
assert.equal(vm.runInContext("stripOutcome('```json\\n{\"a\": 1}\\n```\\nthen prose\\n```json\\n{\"outcome\": \"fyi\"}\\n```')",context),'```json\n{"a": 1}\n```\nthen prose','only the trailing block goes');
assert.equal(vm.runInContext("stripOutcome('no block here')",context),'no block here');
assert.equal(vm.runInContext("stripOutcome(null)",context),'');
/* mount: nothing selected until the hash names an item */
view.mount(root,{switchTo:async()=>true});
assert.match(root.html,/No item open/);
view.show();await settle();
assert.equal(calls.length,0,'no selection, no read');
assert.equal(intervals.has('work-item-poll'),false);
/* an item: the detail, then the transcript it names */
context.location.hash='#/inbox/item?p=inbox-connections&id=aaaa0001';
view.show();await settle();
assert.deepEqual(calls.map(c=>c.path),['/api/inbox/inbox-connections/aaaa0001','/api/sessions/s1/transcript']);
assert.match(root.html,/<h1>Invoice &lt;b&gt;question&lt;\/b&gt;<\/h1>/);
assert.match(root.html,/&lt;script&gt;alert\(1\)&lt;\/script&gt; 12 seats/);
assert.ok(!root.html.includes('<script>alert'),'the body is escaped');
assert.match(root.html,/wi-turn is-person"><div class="wi-turn-head"><b>You<\/b>/,'a user message is the person');
assert.match(root.html,/wi-turn is-assistant"><div class="wi-turn-head"><b>demo<\/b>/,'the assistant is named by the session runtime');
assert.match(root.html,/reply\.md &lt;i&gt;x&lt;\/i&gt;<\/pre>/,'the agent turn is escaped and its outcome block stripped');
assert.ok(!root.html.includes('```json'),'no fenced outcome on the page');
assert.match(root.html,/<b>You<\/b><\/div><pre class="wi-text">Shorter</);
assert.match(root.html,/wi-chip is-outcome">reply drafted</);
assert.match(root.html,/Draft: <code>reply\.md<\/code>/);
assert.match(root.html,/wi-chip">connections<\/span><span class="wi-chip">gmail<\/span><span class="wi-chip">gmail\.unread<\/span><span class="wi-chip">inbox-connections</);
assert.match(root.html,/Open in Space/);
assert.match(root.html,/href="https:\/\/mail\.example\.invalid\/1" target="_blank" rel="noopener noreferrer"/);
assert.match(root.html,/wi-status" role="status">waiting for you · demo · attempt 1</);
assert.match(root.html,/data-act="send-draft"/,'can_send shows Send the draft');
assert.match(root.html,/data-act="archive"/);
assert.doesNotMatch(root.html,/data-act="(start|retry|reopen)"/);
assert.equal(intervals.get('work-item-poll').ms,15000,'idle: a slow poll');
/* while a session runs: the chat waits, the poll is quick */
answers.detail=()=>({ok:true,data:detail({state:'running',running:true,claim:{session_id:'s1',runtime:'demo',live:true}})});
await view.refresh();await settle();
assert.match(root.html,/demo is working on it/);
assert.match(root.html,/class="wi-input" rows="5"[^>]*disabled/);
assert.equal(intervals.get('work-item-poll').ms,2000);
/* a message: one POST with the text, then the detail and the transcript are re-read */
answers.detail=()=>({ok:true,data:detail()});
await view.refresh();await settle();
root.listeners.get('input')({target:{classList:{contains:c=>c==='wi-input'},value:'Keep it to two lines.'}});
let from=calls.length;
click('send');await settle();
assert.equal(posts(from)[0].path,'/api/inbox/inbox-connections/aaaa0001/reply');
assert.equal(JSON.stringify(posts(from)[0].opts.body),JSON.stringify({text:'Keep it to two lines.'}),'the body is the text, nothing else');
assert.deepEqual(reads(from),['/api/inbox/inbox-connections/aaaa0001','/api/sessions/s1/transcript'],'re-read after a reply');
assert.equal(root.input().value,'','the draft is cleared once sent');
/* cmd/ctrl+enter sends too; a failed reply keeps the draft and says why */
reply={ok:false,error:'the agent is still answering'};
root.listeners.get('input')({target:{classList:{contains:c=>c==='wi-input'},value:'and sign it'}});
let prevented=false;
root.listeners.get('keydown')({target:{classList:{contains:c=>c==='wi-input'}},key:'Enter',metaKey:true,preventDefault(){prevented=true;}});
await settle();
assert.ok(prevented);
assert.deepEqual(toasts,['could not send: the agent is still answering']);
assert.equal(root.input().value,'and sign it');
/* the actions: one POST each, the archive body an empty object, then a re-read */
reply={ok:true,data:{}};
from=calls.length;click('send-draft');await settle();
assert.deepEqual(posts(from).map(c=>[c.path,c.opts.body]),[['/api/inbox/inbox-connections/aaaa0001/send',undefined]]);
from=calls.length;click('archive');await settle();
assert.deepEqual(posts(from).map(c=>[c.path,JSON.stringify(c.opts.body)]),[['/api/inbox/inbox-connections/aaaa0001/archive','{}']]);
assert.ok(reads(from).length>=1,'re-read after an action');
/* failed: Retry, and only then; closed: Reopen, and only then; new with no session: Start */
answers.detail=()=>({ok:true,data:detail({state:'failed',session:{session_id:'s1',runtime:'demo',attempt:2,exit:{status:'error',message:'timed out'}},outcome:null,can_send:false})});
await view.refresh();await settle();
assert.match(root.html,/the session failed · demo · attempt 2 · timed out/);
assert.match(root.html,/data-act="retry"/);assert.doesNotMatch(root.html,/data-act="(start|send-draft|reopen)"/);
from=calls.length;click('retry');await settle();
assert.equal(posts(from)[0].path,'/api/inbox/inbox-connections/aaaa0001/start?retry=true');
answers.detail=()=>({ok:true,data:detail({state:'closed',status:'closed',state_reason:'completed',can_send:false})});
await view.refresh();await settle();
assert.match(root.html,/data-act="reopen"/);assert.doesNotMatch(root.html,/data-act="(start|retry|archive|send-draft)"/);
from=calls.length;click('reopen');await settle();
assert.equal(posts(from)[0].path,'/api/inbox/inbox-connections/aaaa0001/reopen');
assert.equal(posts(from)[0].opts.body,undefined);
answers.detail=()=>({ok:true,data:detail({state:'new',session:null,outcome:null,transcript:null,sessions:[],can_send:false})});
from=calls.length;await view.refresh();await settle();
assert.deepEqual(reads(from),['/api/inbox/inbox-connections/aaaa0001'],'no session named, no transcript read');
assert.match(root.html,/no session yet/);
assert.match(root.html,/data-act="start"/);
from=calls.length;click('start');await settle();
assert.equal(posts(from)[0].path,'/api/inbox/inbox-connections/aaaa0001/start');
/* sessions off: the chat says so and nothing sends */
answers.detail=()=>({ok:true,data:detail({can_reply:false,can_send:false})});
await view.refresh();await settle();
assert.match(root.html,/Sessions are off for this section/);
assert.match(root.html,/class="wi-input" rows="5"[^>]*disabled/);
/* a transcript that fails to read leaves the fact and says why; a detail that fails keeps the last good read */
answers.detail=()=>({ok:true,data:detail()});
answers.transcript=()=>({ok:false,status:404,error:'Session not found'});
await view.refresh();await settle();
assert.match(root.html,/12 seats/);
assert.match(root.html,/inb-fail">Session not found · showing the last good read/,'the old transcript stays while its session is the same');
answers.detail=()=>({ok:true,data:detail({transcript:{session_id:'s2',native_session_id:'n2'}})});
await view.refresh();await settle();
assert.match(root.html,/inb-fail">Session not found<\/div>/,'another session: the old transcript is not shown as its');
assert.doesNotMatch(root.html,/Shorter/);
answers.transcript=()=>({ok:true,data:transcript()});
answers.detail=()=>({ok:false,status:0,error:'xo-space is unreachable'});
await view.refresh();await settle();
assert.match(root.html,/xo-space is unreachable · showing the last good read/);
assert.match(root.html,/12 seats/);
/* Open in Space hands the fact's link over; hide stops the poll and ignores a late read */
answers.detail=()=>({ok:true,data:detail()});
await view.refresh();await settle();
click('open-link');assert.equal(JSON.stringify(opened),JSON.stringify([{link:{view:'projects'},kind:'gmail.unread',project_id:'inbox-connections'}]));
const paints=root.paints,late=view.refresh();
view.hide();assert.equal(intervals.has('work-item-poll'),false);
await late;await settle();
assert.equal(root.paints,paints,'a read still in flight when the page hides does not paint');
/* another item in the hash replaces the first and clears the draft */
root.listeners.get('input')({target:{classList:{contains:c=>c==='wi-input'},value:'leftover'}});
context.location.hash='#/inbox/item?p=orbit-api&id=bbbb0002';
from=calls.length;view.show();await settle();
assert.equal(reads(from)[0],'/api/inbox/orbit-api/bbbb0002');
assert.equal(root.input().value,'');
/* a session alone: the transcript only, no chat, no detail read */
context.location.hash='#/inbox/item?s=s3';
answers.transcript=()=>({ok:true,data:transcript({title:'Fix the <b>flaky</b> test',messages:[{id:'m1',role:'assistant',content:'Reproduced it.'}]})});
from=calls.length;view.show();await settle();
assert.deepEqual(reads(from),['/api/sessions/s3/transcript']);
assert.match(root.html,/<h1>Fix the &lt;b&gt;flaky&lt;\/b&gt; test<\/h1>/);
assert.match(root.html,/wi-body is-session/);
assert.match(root.html,/<b>Agent<\/b>/,'no session record, the assistant is the Agent');
assert.doesNotMatch(root.html,/wi-input|wi-actions/);
assert.equal(intervals.get('work-item-poll').ms,15000);
click('send');click('archive');await settle();
assert.equal(posts(from).length,0,'a session alone has no actions');
/* the hash without a selection empties the page */
context.location.hash='#/inbox/item';
view.show();await settle();
assert.match(root.html,/No item open/);
assert.equal(intervals.has('work-item-poll'),false);
console.log('ok');
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ItemPageProbeTests(unittest.TestCase):
    def test_lifecycle_over_a_dom_stub(self) -> None:
        result = subprocess.run(["node", "--input-type=module", "-e", PROBE], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_the_link_helper_lands_where_open_in_space_says(self) -> None:
        script = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const events=[];
globalThis.dispatchEvent=e=>events.push(e);
globalThis.CustomEvent=class{constructor(type,{detail}){this.type=type;this.detail=detail;}};
const {openItemLink,hasLink,safePath}=await import(pathToFileURL(process.cwd()+'/space_ui/js/core/item-links.js'));
const opened=[],go=route=>opened.push(route);
assert.equal(openItemLink(go,{link:{view:'projects',project:'sample',path:'README.md'}}),true);
assert.deepEqual([opened,events[0].type,events[0].detail],[['projects/data/list'],'space:preview-file',{project:'sample',path:'README.md'}]);
assert.equal(openItemLink(go,{link:{view:'projects',project:'sample',path:'../etc'}}),true,'an unsafe path is not previewed');
assert.equal(events.length,1);
assert.equal(openItemLink(go,{kind:'sharing.fetched',project_id:'sample',link:{view:'projects'}}),true);
assert.deepEqual([opened.at(-1),events.at(-1).type,events.at(-1).detail],['inbox/sharing','space:sharing-focus','sample']);
assert.equal(openItemLink(go,{link:{view:'agents'}}),true);assert.equal(opened.at(-1),'agents');
assert.equal(openItemLink(go,{link:{view:'connectors'}}),true);assert.equal(opened.at(-1),'connectors');
assert.equal(openItemLink(go,{link:{project:'sample'}}),true);assert.equal(opened.at(-1),'projects/data/list');
assert.equal(openItemLink(go,{link:null}),false);assert.equal(openItemLink(go,{}),false);
assert.equal(hasLink({link:{view:'agents'}}),true);assert.equal(hasLink({link:{}}),false);assert.equal(hasLink(null),false);
assert.equal(safePath('a/b.md'),true);assert.equal(safePath('/a'),false);assert.equal(safePath('a/../b'),false);
console.log('ok');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
