"""Projects chat view: the real ES module under Node with a stubbed DOM.

The pure helpers (markdown, card grouping, composer state, checklist) are
checked directly; the lifecycle runs with a recorded request function, a fake
EventSource and recorded timers, so no server or browser is involved.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';

const base=pathToFileURL(process.cwd()+'/space_ui/');
globalThis.location={pathname:'/space/',search:'',hash:'#/projects/chat'};
const timers=[];
globalThis.setTimeout=(fn,ms)=>{const t={fn,ms,cleared:false};timers.push(t);return t;};
globalThis.clearTimeout=t=>{if(t)t.cleared=true;};
class Element{
  constructor(){this.nodes=new Map();this.listeners=new Map();this.dataset={};this.value='';this.textContent='';
    this.hidden=false;this.disabled=false;this.html='';this.placeholder='';this.classes=new Set();
    this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  set innerHTML(value){this.html=value;}
  get innerHTML(){return this.html;}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  addEventListener(type,handler){this.listeners.set(type,handler);}
  scrollIntoView(){}
}
const sources=[];
class FakeEventSource{
  constructor(url){this.url=url;this.closed=false;this.listeners={};sources.push(this);}
  addEventListener(type,fn){this.listeners[type]=fn;}
  emit(type,data){this.listeners[type]?.({data:JSON.stringify(data)});}
  close(){this.closed=true;}
}
const ok=data=>({ok:true,status:200,data,offline:false,notImplemented:false,error:null});
const calls=[];
let statusPayload={enabled:true,supported:true,agent:'claude_code'};
let postResult=null;
const msg=(id,text,extra={})=>({id,author_kind:'agent',author_id:'coordinator',text,written_at:'2026-10-13T12:00:00Z',where:'timeline',thread_ts:null,...extra});
const request=async(path,options={})=>{
  calls.push({path,options});
  if(options.method==='POST')return postResult||ok({message:msg('cmsg_9posted',options.body.text,{author_kind:'owner',author_id:'owner'}),delivered_to:{}});
  if(path.endsWith('/status'))return ok(statusPayload);
  if(path.endsWith('/channels'))return ok({channels:[
    {channel:'site',id:'chan_site',kind:'project',open_threads:0},
    {channel:'workspace',id:'chan_workspace',kind:'workspace'},
    {channel:'calc',id:'chan_calc',kind:'project',open_threads:2}]});
  if(/\/threads\/[^/]+$/.test(path))return ok({thread:{ts:'cmsg_1a',title:'Power op',state:'working',branch:'xo/thread-1'},
    card:{state:'working'},status:'✓ Parse ^\n✱ Write tests\n○ Commit',messages:[],session:{status:'idle',turns:1},busy:false});
  if(path.includes('/threads'))return ok({threads:[{ts:'cmsg_1a',title:'Power op',state:'working',card:{state:'needs_reply',headline:'Power op'}}]});
  if(path.includes('/messages'))return ok({messages:[msg('cmsg_1a','first',{author_kind:'owner',author_id:'owner'}),msg('cmsg_2b','second')]});
  return{ok:false,status:404,data:null,error:'http 404'};
};
const settle=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
const mod=await import(new URL('js/views/projects-chat.js',base));
const memory=new Map();
const storage={getItem:k=>memory.get(k)??null,setItem:(k,v)=>memory.set(k,v)};
const mount=async(options={})=>{
  const view=mod.createProjectsChatView({request,EventSourceImpl:FakeEventSource,storage,...options});
  const root=new Element();view.mount(root,{switchTo:async()=>true,refreshToolbar(){}});
  await view.show();await settle();
  return{view,root,$:sel=>root.querySelector(sel)};
};
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ProjectsChatViewTests(unittest.TestCase):
    def probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PRELUDE + "\n" + source],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_is_registered_at_projects_chat(self):
        self.probe(r"""
const {PROJECT_PAGES,PROJECT_SECTIONS,isProjectRoute}=await import(new URL('js/core/navigation.js',base));
const page=PROJECT_PAGES.find(p=>p.id==='projects-chat');
assert.deepEqual([page.route,page.label,page.parent,page.nav],['projects/chat','Chat','projects',false]);
assert.ok(PROJECT_SECTIONS.some(p=>p.id==='projects-chat'),'Chat is a link in the Projects section bar');
assert.ok(isProjectRoute('projects/chat'));
const view=mod.default;
assert.deepEqual([view.id,view.route,view.label,view.parent],['projects-chat','projects/chat','Chat','projects']);
for(const hook of ['mount','show','hide','refresh'])assert.equal(typeof view[hook],'function',hook);
const app=await readFile(new URL('js/app.js',base),'utf8');
assert.match(app,/import projectsChatView from '\.\/views\/projects-chat\.js\?v=[^']+';/);
assert.match(app,/registerView\(projectsChatView\);/);
const index=await readFile(new URL('index.html',base),'utf8');
assert.match(index,/<link rel="stylesheet" href="css\/projects-chat\.css\?v=[^"]+">/);
""")

    def test_markdown_escapes_html_and_links_threads(self):
        self.probe(r"""
const html=mod.chatMarkdown('<script>alert(1)</script> see [Power op](#cmsg_0mv6m0vxl000834eada7)\n'
  +'[bad](javascript:alert(1)) `<b>x</b>` **done** [docs](https://example.com/?a=1&b="2")\n\nnext \x00 0 \x00');
assert.ok(!html.includes('<script'),'raw tags never survive');
assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
assert.ok(html.includes('<a href="#" class="pc-thread-link" data-pc-thread="cmsg_0mv6m0vxl000834eada7">Power op</a>'));
assert.ok(!/href="javascript/.test(html),'only #cmsg_ and http(s) targets become links');
assert.ok(html.includes('<code>&lt;b&gt;x&lt;/b&gt;</code>'));
assert.ok(html.includes('<b>done</b>'));
assert.ok(html.includes('<a href="https://example.com/?a=1&amp;b=&quot;2&quot;" target="_blank" rel="noopener noreferrer">docs</a>'));
assert.equal((html.match(/<p>/g)||[]).length,2,'blank lines split paragraphs');
assert.ok(html.includes('<br>'),'single newlines break lines');
assert.ok(!html.includes('undefined'),'NUL placeholders in the source cannot reach the code-span table');
assert.equal(mod.chatMarkdown('`[a](#cmsg_x1)`'),'<p><code>[a](#cmsg_x1)</code></p>','code spans stay literal');
const row=mod.messageHtml({id:'cmsg_3"x',author_kind:'owner',author_id:'owner',text:'<img src=x onerror=alert(1)>',
  forwarded_from:{channel_id:'chan_workspace',message_id:'cmsg_1'}});
assert.ok(!row.includes('<img'));assert.ok(row.includes('data-id="cmsg_3&quot;x"'));
assert.ok(row.includes('forwarded from workspace'));assert.ok(row.includes('is-owner'));
const sys=mod.messageHtml({id:'cmsg_4',author_kind:'system',text:'Thread **failed**'});
assert.ok(sys.includes('is-system')&&sys.includes('<b>failed</b>'));
const list=mod.checklistHtml('✓ Parsed <x>\n✱ Writing tests\n- ○ Commit\nnote');
assert.ok(list.includes('pc-check is-done')&&list.includes('pc-check is-active')&&list.includes('pc-check is-todo')&&list.includes('pc-check is-note'));
assert.ok(list.includes('Parsed &lt;x&gt;'));assert.equal(mod.checklistHtml(''),'');
""")

    def test_cards_group_by_state_and_hide_empty_groups(self):
        self.probe(r"""
const t=(ts,card,state='working',at='2026-10-13T10:00:00Z')=>({ts,title:ts,state,last_activity_at:at,card:card&&{state:card,headline:ts}});
const threads=[t('cmsg_a','needs_reply'),t('cmsg_b','needs_approval','working','2026-10-13T11:00:00Z'),
  t('cmsg_c','working'),t('cmsg_d','done','resolved'),t('cmsg_e','done'),t('cmsg_f',null,'working')];
const groups=mod.groupThreads(threads);
assert.deepEqual(groups.map(g=>[g.key,g.label,g.threads.map(x=>x.ts)]),[
  ['waiting','Waiting on you',['cmsg_b','cmsg_a']],['working','Working',['cmsg_c','cmsg_f']],
  ['ready','Ready',['cmsg_e']],['resolved','Resolved',['cmsg_d']]]);
assert.equal(mod.threadGroup(t('cmsg_g','failed')),'failed');
assert.equal(mod.threadGroup(t('cmsg_h','needs_reply','resolved')),'resolved','a resolved thread is Resolved whatever its card says');
const html=mod.cardsHtml(threads,'cmsg_c');
assert.ok(!html.includes('data-group="failed"'),'empty groups are hidden');
assert.ok(html.indexOf('data-group="waiting"')<html.indexOf('data-group="working"'));
assert.ok(html.includes('class="pc-card is-open" data-pc-thread="cmsg_c"'));
assert.match(mod.cardsHtml([]),/No threads yet/);
assert.ok(!mod.cardsHtml([t('<x>','done')]).includes('<x>'),'thread fields are escaped');
assert.deepEqual(mod.sortChannels([{channel:'b'},{channel:'workspace'},{channel:'a'}]).map(c=>c.channel),['workspace','a','b']);
""")

    def test_composer_disabled_with_reason_when_chat_cannot_send(self):
        self.probe(r"""
const off=mod.composerState(ok({enabled:false,supported:true}));
assert.equal(off.disabled,true);assert.match(off.reason,/router is off/i);
const unsupported=mod.composerState(ok({enabled:true,supported:false,agent:'hermes'}));
assert.equal(unsupported.disabled,true);assert.match(unsupported.reason,/not available for the active agent \(hermes\)/);
assert.match(mod.composerState({ok:false,status:501,notImplemented:true}).reason,/not available for the active agent/);
assert.equal(mod.composerState(ok({enabled:true,supported:true})).disabled,false);
assert.match(mod.postFailText({ok:false,status:409,error:'x'}),/router is off/);
assert.match(mod.postFailText({ok:false,status:501,notImplemented:true}),/not available for the active agent/);
assert.match(mod.postFailText({ok:false,status:0,offline:true}),/unreachable/);

statusPayload={enabled:false,supported:true};
const {view,$}=await mount();
assert.equal($('[data-pc-input]').disabled,true);assert.equal($('[data-pc-send]').disabled,true);
assert.match($('[data-pc-hint]').textContent,/router is off/i);
assert.ok($('[data-pc-hint]').classes.has('is-blocked'));
$('[data-pc-input]').value='hello';
await view._send();
assert.equal(calls.filter(c=>c.options.method==='POST').length,0,'a disabled composer never posts');
view.hide();
""")

    def test_stream_per_channel_send_threads_and_polling_fallback(self):
        self.probe(r"""
const {view,$}=await mount();
assert.equal(view._state().channel,'workspace','Workspace is selected first');
assert.ok($('[data-pc-channels]').innerHTML.indexOf('workspace')<$('[data-pc-channels]').innerHTML.indexOf('calc'));
assert.match($('[data-pc-channels]').innerHTML,/<span class="pc-count" title="Open threads">2<\/span>/);
assert.equal(sources.length,1,'one stream for the open channel');
assert.equal(sources[0].url,'/api/projects-chat/channels/workspace/stream?after=cmsg_2b');
assert.match($('[data-pc-timeline]').innerHTML,/Thread: Power op/,'a message that started a thread links to it');
sources[0].emit('message',msg('cmsg_3c','live [Power op](#cmsg_1a)'));
sources[0].emit('message',msg('cmsg_3c','duplicate'));
assert.deepEqual(view._state().messages.map(m=>m.id),['cmsg_1a','cmsg_2b','cmsg_3c']);
assert.match($('[data-pc-timeline]').innerHTML,/data-pc-thread="cmsg_1a">Power op<\/a>/);
sources[0].emit('heartbeat',{coordinator_busy:true});
assert.equal($('[data-pc-busy]').hidden,false);
sources[0].emit('heartbeat',{coordinator_busy:false});
assert.equal($('[data-pc-busy]').hidden,true);

$('[data-pc-input]').value='  do it  ';
await view._send();
const post=calls.filter(c=>c.options.method==='POST').at(-1);
assert.equal(post.path,'/api/projects-chat/channels/workspace/messages');assert.deepEqual(post.options.body,{text:'do it'});
assert.equal($('[data-pc-input]').value,'');
assert.ok(view._state().messages.some(m=>m.id==='cmsg_9posted'));

postResult={ok:false,status:409,data:null,notImplemented:false,error:'The router is off'};
$('[data-pc-input]').value='again';await view._send();
assert.equal($('[data-pc-error]').hidden,false);assert.match($('[data-pc-error]').textContent,/router is off/);
assert.equal($('[data-pc-input]').value,'again','a refused message keeps its draft');
postResult=null;

view._openThread('cmsg_1a');await settle();
assert.equal($('[data-pc-thread-pane]').hidden,false);assert.equal($('[data-pc-cards-pane]').hidden,true);
const body=$('[data-pc-thread-body]').innerHTML;
assert.ok(body.includes('Power op')&&body.includes('<code>xo/thread-1</code>'));
assert.ok(body.includes('pc-check is-done')&&body.includes('pc-check is-active')&&body.includes('pc-check is-todo'));
$('[data-pc-thread-input]').value='straight to the thread';
await view._send({thread:true});
const threadPost=calls.filter(c=>c.options.method==='POST').at(-1);
assert.deepEqual(threadPost.options.body,{text:'straight to the thread',thread_ts:'cmsg_1a'});

await view._selectChannel('calc');await settle();
assert.equal(sources[0].closed,true,'switching channels closes the old stream');
assert.equal(sources.length,2);assert.match(sources[1].url,/\/channels\/calc\/stream\?after=/);
assert.equal(view._state().openTs,'','a channel switch closes the thread pane');
assert.equal(memory.get('space.projectsChat.channel'),'calc');

sources[1].onerror();
assert.equal(sources[1].closed,true);
const poll=timers.filter(t=>t.ms===5000&&!t.cleared);
assert.equal(poll.length,1,'a broken stream falls back to one 5 s poll');
const before=calls.length;await poll[0].fn();await settle();
assert.ok(calls.slice(before).some(c=>c.path.startsWith('/api/projects-chat/channels/calc/messages?after=')));
view.hide();
const next=timers.filter(t=>t.ms===5000);
assert.equal(next.length,2,'each poll schedules the next');
assert.equal(next.at(-1).cleared,true,'hide stops polling');
assert.equal(sources.length,2,'no stream opens while hidden');
""")


if __name__ == "__main__":
    unittest.main()
