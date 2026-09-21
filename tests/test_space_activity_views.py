"""Activity transforms and lifecycle run with isolated reads and fake timers."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let sequence=0;const timeouts=new Map(),intervals=new Map(),calls=[],listeners=new Map();
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
class Element{
  constructor(){this.children=[];this.nodes=new Map();this.listeners=new Map();this.dataset={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.html='';this.parent=null;this.classList={toggle(){}};}
  set innerHTML(value){this.html=value;}
  get innerHTML(){return this.html;}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  addEventListener(type,handler){this.listeners.set(type,handler);}
  focus(){this.focused=true;}
  scrollIntoView(){}
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
 :/^\/api\/xo-projects\/[^/]+\/activity$/.test(path)?success({project_id:path.split('/')[3],open_sessions:[]})
 :/^\/api\/xo-projects\/[^/]+\/todos$/.test(path)?success({project_id:path.split('/')[3],sessions:{}})
 :success({events:[],next_cursor:null});
const request=(path,options)=>{calls.push({path,options});return handler(path,options);};
const esc=value=>String(value??'').replace(/[&<>"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
const ctx=vm.createContext({
  console,AbortController,Date,Map,Set,Promise,API_BASE:'',apiFetch:request,esc,rel:()=> 'recently',
  INBOX_PAGES:[{id:'inbox-activity',route:'inbox/activity',parent:'inbox'}],
  addEventListener:(type,handler)=>{if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(handler);},
  document:{createElement:()=>new Element()},location:{hash:'#/inbox/activity'},
  setTimeout:(fn,ms)=>{const id=++sequence;timeouts.set(id,{fn,ms});return id;},clearTimeout:id=>timeouts.delete(id),
  setInterval:(fn,ms)=>{const id=++sequence;intervals.set(id,{fn,ms});return id;},clearInterval:id=>intervals.delete(id),
});
const source=fs.readFileSync('space_ui/js/views/inbox-activity.js','utf8').replace(/^import[^\n]*\n/gm,'').replace(/export function /g,'function ');
vm.runInContext(source+'\nglobalThis.api={createActivityViews,buildWorkspaceEvents,filterActivityEvents,buildProjectTodos};',ctx);
const {api}=ctx;
const emit=(type,detail)=>{for(const fn of listeners.get(type)||[])fn({detail});};
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

    def test_auxiliary_timeouts_do_not_hide_feed_and_are_cancelled(self):
        self.probe(r"""
const never=gate();
handler=path=>path.includes('/timeline')?success({events:[event('<script>unsafe</script>')],next_cursor:'2026-09-14T09:00:00Z'}):never.promise;
const [view]=api.createActivityViews({timeoutMs:25}),root=mount(view);
view.show();const shown=view.refresh();await settle();
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
view.show();const initial=view.refresh();await settle();
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
const [view]=api.createActivityViews(),root=mount(view);view.show();await view.refresh();
await root.querySelector('[data-activity-more]').emit('click');
assert.ok(calls.some(call=>call.path.includes('before=2026-09-14T09%3A00%3A00Z')));
assert.equal(rows(root).length,2);view.toolbar.search.setValue('old');
assert.equal(rows(root).length,1);revision++;await view.refresh();
assert.equal(view.toolbar.search.getValue(),'old');assert.match(markup(root),/old\.md/);
assert.equal(root.querySelector('[data-activity-more]').hidden,true,'Loaded older history retains its final cursor');
view.hide();
""")

    def test_empty_and_error_states_are_distinct_on_the_one_activity_page(self):
        self.probe(r"""
const defaults=handler;let failed=true;
handler=path=>path.includes('/timeline')?(failed?{ok:false}:success({events:[]})):defaults(path);
const views=api.createActivityViews();
assert.equal(views.length,1,'Sharing activity is gone: Activity is the only feed');
const [view]=views,a=mount(view);
view.show();await view.refresh();assert.equal(a.querySelector('[data-activity-summary]').textContent,'Activity unavailable');
assert.ok(!a.querySelector('[data-activity-empty]').textContent.includes('No project events'));
failed=false;await view.refresh();assert.match(a.querySelector('[data-activity-empty]').textContent,/No project events/);
view.toolbar.search.setValue('workspace draft');view.hide();view.show();await view.refresh();
assert.equal(view.toolbar.search.getValue(),'workspace draft','A hidden page keeps its search');
assert.equal(view.section,'inbox-activity');assert.equal(view.route,'inbox/activity');
assert.ok(!calls.some(call=>call.path.includes('project-sharing')),'The activity page never reads the sharing relay');
view.hide();assert.equal(intervals.size,0);
""")

    def test_cold_show_does_not_block_project_handoff_on_global_reads(self):
        self.probe(r"""
const stalled=gate(),defaults=handler;
handler=path=>['/api/xo-projects/timeline?limit=200','/api/xo-projects/activity','/api/xo-projects'].includes(path)?stalled.promise:defaults(path);
const [view]=api.createActivityViews(),root=mount(view);
assert.equal(view.show(),undefined,'Activation must finish before optional workspace reads');await settle();
const globalReads=calls.slice();
emit('space:activity-project',{project_id:'alpha'});await settle();
assert.ok(globalReads.every(call=>call.options.signal.aborted),'The project handoff cancels irrelevant global reads');
for(const suffix of ['timeline?limit=200','activity','todos'])assert.ok(calls.some(call=>call.path==='/api/xo-projects/alpha/'+suffix));
assert.equal(root.querySelector('[data-activity-project-filter]').value,'alpha');
assert.equal(root.querySelector('[data-activity-todos-summary]').textContent,'0 todos');
view.hide();stalled.resolve(success({items:[],events:[],open_sessions:[]}));await settle();
""")

    def test_project_todos_order_limit_and_safe_rendering(self):
        self.probe(r"""
const list=api.buildProjectTodos({sessions:{alpha:{runtime:'fixture',todos:[
 {id:'done',status:'completed',content:'done'},null,
 {id:'active',status:'in_progress',content:'<img src=x onerror=alert(1)>'},
 {id:'cancelled',status:'cancelled',content:'cancelled'},
 {id:'blocked',status:'blocked',content:'blocked'},
 {id:'pending',status:'pending',content:'pending'},
]}}});
assert.deepEqual(Array.from(list,row=>row.status),['in_progress','pending','blocked','completed','cancelled']);
assert.equal(list[0].sessionId,'alpha');assert.equal(list[0].runtime,'fixture');
const defaults=handler;
handler=path=>path.endsWith('/todos')?success({project_id:'alpha',sessions:{a:{runtime:'fixture',todos:[
 ...list,...Array.from({length:30},(_,i)=>({id:'extra-'+i,status:'pending',content:'Task '+i})),
]}}}):defaults(path);
emit('space:activity-project',{project_id:'alpha'});
const [view]=api.createActivityViews(),root=mount(view);view.show();await view.refresh();
const html=root.querySelector('[data-activity-todo-rows]').innerHTML;
assert.equal((html.match(/class="iac-todo"/g)||[]).length,30);
assert.match(html,/Showing 30 of 35 todos · 5 more/);
assert.ok(html.includes('&lt;img src=x'));assert.ok(!html.includes('<img src=x'));
assert.equal(root.querySelector('[data-activity-project-filter]').value,'alpha');
view.hide();
""")

    def test_cold_and_active_project_handoffs_use_scoped_reads_without_n_plus_one(self):
        self.probe(r"""
const [view]=api.createActivityViews(),root=mount(view);view.show();await view.refresh();
assert.equal(calls.some(call=>call.path.endsWith('/todos')),false);
view.toolbar.search.setValue('old event query');
emit('space:activity-project',{project_id:'alpha'});await settle();
assert.equal(view.toolbar.search.getValue(),'');
assert.equal(root.querySelector('[data-activity-project-filter]').value,'alpha');
for(const suffix of ['timeline?limit=200','activity','todos'])
 assert.ok(calls.some(call=>call.path==='/api/xo-projects/alpha/'+suffix));
assert.equal(root.querySelector('[data-activity-live]').open,true);
assert.equal(root.querySelector('[data-activity-todos]').open,true);
root.querySelector('[data-activity-todos]').open=false;await view.refresh();
assert.equal(root.querySelector('[data-activity-todos]').open,false,'Refresh preserves collapsed auxiliary sections');
view.hide();emit('space:activity-project',{project_id:'beta'});
view.show();await view.refresh();
assert.equal(root.querySelector('[data-activity-project-filter]').value,'beta','Hidden-page handoff is consumed on entry');
assert.ok(calls.some(call=>call.path==='/api/xo-projects/beta/todos'));
const count=calls.filter(call=>call.path.endsWith('/todos')).length;
const select=root.querySelector('[data-activity-project-filter]');select.value='';select.emit('change');await settle();
assert.equal(calls.filter(call=>call.path.endsWith('/todos')).length,count,'All projects never fans out todo requests');
assert.equal(root.querySelector('[data-activity-todos]').hidden,true);view.hide();
""")

    def test_scoped_auxiliary_failures_and_stale_replies_do_not_cross_projects(self):
        self.probe(r"""
const oldTodo=gate(),oldLive=gate(),defaults=handler;let malformed=false;
handler=path=>path==='/api/xo-projects/alpha/todos'?oldTodo.promise
 :path==='/api/xo-projects/alpha/activity'?oldLive.promise
 :path==='/api/xo-projects/beta/todos'?success({project_id:malformed?'alpha':'beta',sessions:{b:{runtime:'local',todos:[{status:'blocked',content:'Beta task'}]}}})
 :path==='/api/xo-projects/beta/activity'?success({project_id:malformed?'alpha':'beta',open_sessions:[{session_id:'beta-session',agent:'Beta agent',runtime:'local',opened_at:'invalid',last_activity_at:'2026-09-14T10:00:00Z'}]})
 :defaults(path);
emit('space:activity-project',{project_id:'alpha'});
const [view]=api.createActivityViews(),root=mount(view);view.show();const first=view.refresh();await settle();
emit('space:activity-project',{project_id:'beta'});await settle();
oldTodo.resolve(success({project_id:'alpha',sessions:{a:{todos:[{status:'pending',content:'Old Alpha task'}]}}}));
oldLive.resolve(success({project_id:'alpha',open_sessions:[{session_id:'old-alpha'}]}));await first;await settle();
const todos=root.querySelector('[data-activity-todo-rows]'),live=root.querySelector('[data-activity-live-rows]');
assert.match(todos.innerHTML,/Beta task/);assert.ok(!todos.innerHTML.includes('Alpha'));
assert.match(live.innerHTML,/Beta agent/);assert.match(live.innerHTML,/Opened/);assert.match(live.innerHTML,/Last active/);assert.match(live.innerHTML,/Time unavailable/);
malformed=true;await view.refresh();
assert.match(root.querySelector('[data-activity-warning]').textContent,/Open sessions are unavailable/);
assert.match(root.querySelector('[data-activity-warning]').textContent,/Project todos are unavailable/);
assert.ok(!todos.innerHTML.includes('Beta task'),'Mismatched identity is unavailable, never presented as current');
malformed=false;await view.refresh();assert.match(todos.innerHTML,/Beta task/);view.hide();
""")


if __name__ == "__main__":
    unittest.main()
