"""Activity transforms and lifecycle run with isolated reads and fake timers."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let sequence=0;const timeouts=new Map(),intervals=new Map(),calls=[];
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
class Element{
  constructor(){this.children=[];this.nodes=new Map();this.listeners=new Map();this.dataset={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.html='';this.parent=null;this.classList={toggle(){}};}
  set innerHTML(value){this.html=value;}
  get innerHTML(){return this.html;}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  addEventListener(type,handler){this.listeners.set(type,handler);}
  emit(type){return this.listeners.get(type)?.({target:this});}
  get firstElementChild(){return this.children[0]||null;}
  get nextElementSibling(){if(!this.parent)return null;return this.parent.children[this.parent.children.indexOf(this)+1]||null;}
  remove(){if(this.parent){const rows=this.parent.children;rows.splice(rows.indexOf(this),1);this.parent=null;}}
  insertBefore(node,anchor){node.remove();const at=anchor?this.children.indexOf(anchor):this.children.length;this.children.splice(at,0,node);node.parent=this;}
}
const success=data=>({ok:true,data});
const event=(title,ts='2026-09-14T10:00:00Z',project_id='alpha')=>({type:'file.edited',path:title,ts,project_id});
let handler=path=>path==='/api/xo-projects'?success({items:[{id:'alpha',display_name:'Alpha Project'},{id:'beta',display_name:'Beta Project'}]})
 :path==='/api/xo-projects/activity'?success({open_sessions:[]})
 :path==='/api/project-sharing/status'?success({recent:[],repos:{}}):success({events:[],next_cursor:null});
const request=(path,options)=>{calls.push({path,options});return handler(path,options);};
const esc=value=>String(value??'').replace(/[&<>"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
const ctx=vm.createContext({
  console,AbortController,Date,Map,Set,Promise,API_BASE:'',apiFetch:request,esc,rel:()=> 'recently',
  INBOX_PAGES:[{id:'inbox-activity',route:'inbox/activity',parent:'inbox'},{id:'inbox-sharing-activity',route:'inbox/sharing-activity',parent:'inbox'}],
  document:{createElement:()=>new Element()},location:{hash:'#/inbox/activity'},
  setTimeout:(fn,ms)=>{const id=++sequence;timeouts.set(id,{fn,ms});return id;},clearTimeout:id=>timeouts.delete(id),
  setInterval:(fn,ms)=>{const id=++sequence;intervals.set(id,{fn,ms});return id;},clearInterval:id=>intervals.delete(id),
});
const source=fs.readFileSync('space_ui/js/views/inbox-activity.js','utf8').replace(/^import[^\n]*\n/gm,'').replace(/export function /g,'function ');
vm.runInContext(source+'\nglobalThis.api={createActivityViews,buildWorkspaceEvents,buildSharingEvents,filterActivityEvents};',ctx);
const {api}=ctx;
const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const mount=view=>{const root=new Element();view.mount(root,{switchTo:async()=>true,refreshToolbar(){}});return root;};
const rows=root=>root.querySelector('[data-activity-rows]').children;
const markup=root=>rows(root).map(row=>row.innerHTML).join('\n');
"""


@unittest.skipUnless(shutil.which("node"), "node is unavailable")
class ActivityViewTests(unittest.TestCase):
    def probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "-e", PRELUDE + "\n(async()=>{\n" + source +
             "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workspace_transform_search_and_unknown_time_are_factual(self):
        self.probe(r"""
const events=api.buildWorkspaceEvents({events:[
  {type:'todo.status_changed',ts:'2026-09-14T11:00:00Z',status:'blocked',todo_id:'task-7',runtime:'local',project_id:'alpha'},
  {type:'workitem.assigned',ts:'2026-09-14T10:00:00Z',title:'Review parser',assignee:null,workitem_id:'w7'},
  {type:'session.started',ts:'not-a-date',session_id:'sess-1'},
  {unexpected:true},
]},'beta');
assert.equal(events.length,3);assert.equal(events[0].tone,'error');
assert.match(events[0].detail,/Status: blocked/);assert.equal(events[1].projectId,'beta');
assert.match(events[1].detail,/Unassigned/);assert.equal(events[2].ms,null);
const names=new Map([['alpha','Alpha Research']]);
assert.equal(api.filterActivityEvents(events,{query:'research BLOCKED',names}).length,1,'Search requires every term across names and event metadata');
assert.equal(api.filterActivityEvents(events,{query:'research parser',names}).length,0);
assert.equal(api.filterActivityEvents(events,{project:'beta'}).length,2);
""")

    def test_sharing_transform_uses_only_recorded_transitions(self):
        self.probe(r"""
const events=api.buildSharingEvents({recent:[
 {at:'2026-09-14T10:00:00Z',repo:'github.com/team/alpha',kind:'fetched',detail:'2 commit(s)'},
 {at:'2026-09-14T11:00:00Z',repo:'github.com/team/incoming',kind:'clone_failed',detail:'no_access'},
],repos:{'github.com/team/alpha':{project:'alpha',fetched:500,last_fetch_at:'2026-09-14T12:00:00Z'}}});
assert.equal(events.length,2,'Current repo state cannot manufacture extra events');
assert.equal(events[0].label,'Clone failed');assert.equal(events[0].projectId,'');
assert.equal(events[1].projectId,'alpha');
assert.equal(api.filterActivityEvents(events,{project:'github.com/team/incoming'}).length,1);
assert.equal(api.buildSharingEvents({recent:[],repos:{}}).length,0,'A fresh process has no retained history');
""")

    def test_auxiliary_timeouts_do_not_hide_feed_and_are_cancelled(self):
        self.probe(r"""
const never=gate();
handler=path=>path.includes('/timeline')?success({events:[event('<script>unsafe</script>')],next_cursor:'2026-09-14T09:00:00Z'}):never.promise;
const [view]=api.createActivityViews({timeoutMs:25}),root=mount(view);
const shown=view.show();await settle();
assert.equal(rows(root).length,1,'Timeline renders before auxiliary reads finish');
assert.ok(markup(root).includes('&lt;script&gt;unsafe&lt;/script&gt;'));
assert.ok(!markup(root).includes('<script>unsafe'));
assert.equal(root.querySelector('[data-activity-more]').disabled,true,'Older-page action waits for the current refresh to finish');
for(const {fn} of [...timeouts.values()])fn();await shown;
assert.match(root.querySelector('[data-activity-warning]').textContent,/Project names/);
assert.match(root.querySelector('[data-activity-warning]').textContent,/Open sessions/);
assert.equal(rows(root).length,1);
assert.equal(root.querySelector('[data-activity-more]').disabled,false,'Partial failure does not leave pagination disabled');
assert.equal(calls.filter(call=>call.options.signal.aborted).length,2,'Timed-out auxiliary requests are aborted independently');
assert.equal(intervals.size,1);view.hide();assert.equal(intervals.size,0);
assert.ok(calls.every(call=>!call.options.method),'The activity page performs reads only');
""")

    def test_project_change_and_hide_reject_stale_reads(self):
        self.probe(r"""
const old=gate(),next=gate(),defaults=handler;
handler=path=>path==='/api/xo-projects/timeline?limit=200'?old.promise
 :path==='/api/xo-projects/beta/timeline?limit=200'?next.promise:defaults(path);
const [view]=api.createActivityViews(),root=mount(view);
const initial=view.show();await settle();
const original=calls.find(call=>call.path==='/api/xo-projects/timeline?limit=200');
const select=root.querySelector('[data-activity-project-filter]');select.value='beta';select.emit('change');await settle();
assert.equal(original.options.signal.aborted,true);
assert.ok(calls.some(call=>call.path==='/api/xo-projects/beta/timeline?limit=200'));
next.resolve(success({events:[{type:'file.created',path:'beta.md',ts:'2026-09-14T12:00:00Z'}]}));await settle();
old.resolve(success({events:[event('old-alpha.md')]}));await initial;await settle();
assert.match(markup(root),/beta\.md/);assert.ok(!markup(root).includes('old-alpha'));
const later=gate();handler=path=>path.includes('/timeline')?later.promise:defaults(path);
const refresh=view.refresh();await settle();view.hide();await refresh;
const before=markup(root);later.resolve(success({events:[event('late.md')]}));await settle();
assert.equal(markup(root),before);assert.equal(intervals.size,0);
""")

    def test_pagination_and_refresh_keep_loaded_history_and_query(self):
        self.probe(r"""
const defaults=handler;let revision=0;
handler=path=>path.includes('before=')?success({events:[event('old.md','2026-09-13T10:00:00Z')],next_cursor:null})
 :path.includes('/timeline')?success({events:[event(revision?'new.md':'current.md')],next_cursor:'2026-09-14T09:00:00Z'}):defaults(path);
const [view]=api.createActivityViews(),root=mount(view);await view.show();
await root.querySelector('[data-activity-more]').emit('click');
assert.ok(calls.some(call=>call.path.includes('before=2026-09-14T09%3A00%3A00Z')));
assert.equal(rows(root).length,2);view.toolbar.search.setValue('old');
assert.equal(rows(root).length,1);revision++;await view.refresh();
assert.equal(view.toolbar.search.getValue(),'old');assert.match(markup(root),/old\.md/);
assert.equal(root.querySelector('[data-activity-more]').hidden,true,'Loaded older history retains its final cursor');
view.hide();
""")

    def test_empty_error_and_independent_page_state_are_distinct(self):
        self.probe(r"""
const defaults=handler;let failed=true;
handler=path=>path.includes('/timeline')?(failed?{ok:false}:success({events:[]})):defaults(path);
const [workspace,sharing]=api.createActivityViews(),a=mount(workspace),b=mount(sharing);
await workspace.show();assert.equal(a.querySelector('[data-activity-summary]').textContent,'Activity unavailable');
assert.ok(!a.querySelector('[data-activity-empty]').textContent.includes('No project events'));
failed=false;await workspace.refresh();assert.match(a.querySelector('[data-activity-empty]').textContent,/No project events/);
workspace.toolbar.search.setValue('workspace draft');workspace.hide();await sharing.show();
assert.equal(sharing.toolbar.search.getValue(),'');sharing.toolbar.search.setValue('repo search');
assert.equal(workspace.toolbar.search.getValue(),'workspace draft');
assert.equal(sharing.section,'inbox-sharing-activity');assert.equal(workspace.section,'inbox-activity');
sharing.hide();assert.equal(intervals.size,0);
""")


if __name__ == "__main__":
    unittest.main()
