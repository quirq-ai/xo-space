/* Read-only activity streams. Workspace history and the relay's volatile
   recent events have different retention, so each owns a separate page. */
import {API_BASE,apiFetch} from '../core/api.js';
import {esc,rel} from '../core/ui.js';
import {INBOX_PAGES} from '../core/navigation.js?v=20260914-manage1';

const LIMIT=200;
const text=value=>typeof value==='string'?value.trim():'';
const human=value=>text(value).replace(/[._]/g,' ');
const stamp=value=>Number.isFinite(Date.parse(value))?Date.parse(value):null;
const dateLabel=value=>stamp(value)===null?'Time unavailable':new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});
const WORKSPACE_LABELS={
  'project.created':'Project created','session.started':'Session started','session.closed':'Session ended',
  'todo.added':'Task added','todo.completed':'Task completed','todo.status_changed':'Task status changed',
  'file.edited':'File edited','file.created':'File created','plan.written':'Plan updated','episode.written':'Session notes saved',
  'peer.sync.started':'Peer sync started','peer.sync.applied':'Peer sync applied','peer.sync.conflict':'Peer sync conflict',
  'workitem.created':'Work item created','workitem.adopted':'Issue linked','workitem.assigned':'Work item assigned',
  'workitem.claimed':'Work started','workitem.released':'Work released','workitem.closed':'Work item closed',
  'workitem.reopened':'Work item reopened','workitem.deleted':'Work item deleted',
};
const SHARING_LABELS={shared_with_you:'Shared with this Space',fetched:'Commits fetched',revoked:'Sharing access removed',
  cloned:'Project cloned',clone_failed:'Clone failed',error:'Sync failed'};
const newestFirst=rows=>rows.sort((a,b)=>(b.ms??-Infinity)-(a.ms??-Infinity)||b.order-a.order);
/* Status vocabulary lives in visualizer/todo_status.py; order is a UI choice. */
const ST_ORDER={in_progress:0,pending:1,blocked:2,completed:3,cancelled:4};
const TODO_LIMIT=30;
let pendingProject=null;
addEventListener('space:activity-project',event=>{
  const id=text(event.detail?.project_id);
  if(id)pendingProject=id;
});

export function buildProjectTodos(payload){
  const rows=[];
  for(const [sessionId,session] of Object.entries(payload?.sessions||{})){
    for(const todo of Array.isArray(session?.todos)?session.todos:[]){
      if(!todo||!text(todo.status)||typeof todo.content!=='string')continue;
      rows.push({id:text(todo.id),sessionId,runtime:text(session.runtime),status:text(todo.status),content:todo.content});
    }
  }
  return rows.sort((a,b)=>(ST_ORDER[a.status]??9)-(ST_ORDER[b.status]??9));
}

export function buildWorkspaceEvents(payload,projectId=''){
  return newestFirst((Array.isArray(payload?.events)?payload.events:[]).flatMap((event,order)=>{
    if(!event||!text(event.type))return[];
    const project=text(event.project_id)||projectId,kind=text(event.type),ts=text(event.ts);
    const detail=text(event.todo?.content)||text(event.title)||text(event.path)
      ||(text(event.todo_id)?'Task '+event.todo_id:text(event.workitem_id)?'Work item '+event.workitem_id:'');
    const extras=[text(event.status)&&'Status: '+human(event.status),text(event.outcome),text(event.state_reason),
      Object.hasOwn(event,'assignee')?(event.assignee===null?'Unassigned':text(event.assignee)&&'Assigned to '+text(event.assignee)):'',
      text(event.issue?.repo)&&Number.isInteger(event.issue?.number)?event.issue.repo+' #'+event.issue.number:''].filter(Boolean);
    const row={projectId:project,filterId:project,subject:project,kind,ts,ms:stamp(ts),order,
      label:WORKSPACE_LABELS[kind]||human(kind),detail:[detail,...extras].filter(Boolean).join(' · '),
      runtime:text(event.runtime),sessionId:text(event.session_id),tone:kind.includes('conflict')||event.status==='blocked'?'error':''};
    row.key=JSON.stringify([project,ts,kind,row.detail,row.runtime,row.sessionId,text(event.user_id),text(event.agent)]);
    return[row];
  }));
}

export function buildSharingEvents(payload){
  return newestFirst((Array.isArray(payload?.recent)?payload.recent:[]).flatMap((event,order)=>{
    if(!event||!text(event.kind)||!text(event.repo))return[];
    const repo=text(event.repo),kind=text(event.kind),ts=text(event.at);
    const row={projectId:text(payload.repos?.[repo]?.project),filterId:repo,subject:repo,kind,ts,ms:stamp(ts),order,
      label:SHARING_LABELS[kind]||human(kind),detail:text(event.detail),runtime:'',sessionId:'',
      tone:kind==='error'||kind==='clone_failed'?'error':''};
    row.key=JSON.stringify([repo,ts,kind,row.detail]);return[row];
  }));
}

export function filterActivityEvents(events,{query='',project='',names=new Map()}={}){
  const words=text(query).toLowerCase().split(/\s+/).filter(Boolean);
  return events.filter(event=>(!project||event.filterId===project)&&words.every(word=>
    [event.label,event.detail,event.subject,names.get(event.projectId)||'',event.kind,event.runtime,event.sessionId]
      .join(' ').toLowerCase().includes(word)));
}

export function createActivityViews({request=apiFetch,timeoutMs=12000,pollMs=30000}={}){
  return [activityView('inbox-activity',false),activityView('inbox-sharing-activity',true)];

  function activityView(id,sharing){
    let root=null,go=()=>{},refreshToolbar=()=>{},active=false,poll=null,generation=0,pending=null;
    let query='',project='',events=[],names=new Map(),snapshot=null,sessions=null,nextCursor=null;
    let hasSnapshot=false,loadedOlder=false,loading=false,loadingMore=false,lastLoaded=null;
    let feedError='',catalogError='',liveError='',todosError='',invalidRows=false;
    let todos=null,liveLoading=false,todosLoading=false;
    const reads=new Set(),rowNodes=new Map();
    const $=selector=>root.querySelector(selector);
    const title=sharing?'Sharing activity':'Activity';

    function read(path){
      return new Promise(resolve=>{
        const controller=new AbortController();let settled=false;
        const finish=result=>{if(settled)return;settled=true;clearTimeout(timer);reads.delete(cancel);resolve(result);};
        const cancel=()=>{finish({ok:false,cancelled:true});controller.abort();};
        const timer=setTimeout(()=>{finish({ok:false});controller.abort();},timeoutMs);
        reads.add(cancel);
        Promise.resolve().then(()=>request(API_BASE+path,{signal:controller.signal})).then(finish,()=>finish({ok:false}));
      });
    }
    function cancelReads(){for(const cancel of reads)cancel();pending=null;}
    function current(revision){return active&&revision===generation;}
    function timelinePath(before=''){
      const base=project?'/api/xo-projects/'+encodeURIComponent(project)+'/timeline':'/api/xo-projects/timeline';
      return base+'?limit='+LIMIT+(before?'&before='+encodeURIComponent(before):'');
    }

    async function refresh({older=false}={}){
      if(!root||!active)return;
      if(pending)return pending;
      if(older&&(!nextCursor||sharing))return;
      const revision=++generation,selected=project,before=older?nextCursor:'';
      if(!older&&!sharing){liveLoading=true;todosLoading=Boolean(selected);}
      loading=!older;loadingMore=older;feedError='';render();
      const work=read(sharing?'/api/project-sharing/status':timelinePath(before)).then(result=>{
        if(!current(revision))return;
        loading=false;loadingMore=false;
        const payload=result.data,list=sharing?payload?.recent:payload?.events;
        if(!result.ok||!Array.isArray(list)){
          feedError=hasSnapshot?'Could not refresh activity. Previously loaded events are shown.':'Could not load activity. Try Refresh.';
          render();return;
        }
        const rows=sharing?buildSharingEvents(payload):buildWorkspaceEvents(payload,selected);
        invalidRows=rows.length!==list.length;
        if(older||(!sharing&&loadedOlder)){
          const merged=new Map(events.map(row=>[row.key,row]));
          for(const row of rows)merged.set(row.key,row);
          events=newestFirst([...merged.values()]);
        }else events=rows;
        if(!sharing&&(older||!loadedOlder)){
          const cursor=text(payload.next_cursor);
          nextCursor=rows.length&&stamp(cursor)!==null&&cursor!==before?cursor:null;
        }
        if(older)loadedOlder=true;
        snapshot=payload;hasSnapshot=true;lastLoaded=new Date().toISOString();render();
      });
      const extra=older||sharing?[]:[
        read('/api/xo-projects').then(result=>{
          if(!current(revision))return;
          if(result.ok&&Array.isArray(result.data?.items)&&result.data.items.every(item=>text(item?.id))){
            names=new Map(result.data.items.map(item=>[item.id,text(item.display_name)||item.id]));catalogError='';
          }else catalogError='Project names could not be refreshed. Project IDs are still available.';
          render();
        }),
        read(selected?'/api/xo-projects/'+encodeURIComponent(selected)+'/activity':'/api/xo-projects/activity').then(result=>{
          if(!current(revision))return;
          liveLoading=false;
          if(result.ok&&Array.isArray(result.data?.open_sessions)&&(!selected||result.data.project_id===selected)){
            sessions=result.data.open_sessions.filter(session=>session&&text(session.session_id))
              .map(session=>selected?{...session,project_id:selected}:session);liveError='';
          }else{sessions=null;liveError='Open sessions are unavailable.';}
          render();
        }),
        ...(selected?[read('/api/xo-projects/'+encodeURIComponent(selected)+'/todos').then(result=>{
          if(!current(revision))return;
          todosLoading=false;
          const data=result.data,entries=data?.sessions;
          if(result.ok&&data.project_id===selected&&entries&&typeof entries==='object'&&!Array.isArray(entries)
            &&Object.values(entries).every(session=>session&&Array.isArray(session.todos)
              &&session.todos.every(todo=>todo&&text(todo.status)&&typeof todo.content==='string'))){
            todos=buildProjectTodos(data);todosError='';
          }else{todos=null;todosError='Project todos are unavailable. Try Refresh.';}
          render();
        })]:[]),
      ];
      pending=Promise.all([work,...extra]).finally(()=>{if(current(revision)){pending=null;render();refreshToolbar();}});
      return pending;
    }

    function renderFilter(){
      const options=new Map(sharing?[]:names);
      for(const row of events)if(row.filterId)options.set(row.filterId,sharing?row.subject:names.get(row.projectId)||row.projectId);
      if(sharing&&snapshot?.repos&&typeof snapshot.repos==='object'&&!Array.isArray(snapshot.repos)){
        for(const repo of Object.keys(snapshot.repos))options.set(repo,repo);
      }
      if(project&&!options.has(project))options.set(project,project);
      const markup='<option value="">'+(sharing?'All repositories':'All projects')+'</option>'
        +[...options].sort((a,b)=>a[1].localeCompare(b[1])).map(([value,label])=>'<option value="'+esc(value)+'">'+esc(label)+'</option>').join('');
      const select=$('[data-activity-project-filter]');
      if(select.dataset.options!==markup){select.innerHTML=markup;select.dataset.options=markup;}
      select.value=project;
    }
    function renderLive(){
      if(sharing)return;
      const details=$('[data-activity-live]'),summary=$('[data-activity-live-summary]');
      const rows=(sessions||[]).filter(session=>!project||session.project_id===project);
      summary.textContent=(liveError||sessions===null)?'Open sessions unavailable':rows.length+' open '+(rows.length===1?'session':'sessions');
      if(liveLoading)summary.textContent=sessions===null?'Checking open sessions…':'Refreshing… · '+summary.textContent;
      details.classList.toggle('is-unavailable',sessions===null);
      $('[data-activity-live-rows]').innerHTML=rows.length?rows.map(session=>{
        const pid=text(session.project_id);
        const sessionTime=(label,value)=>'<span>'+label+' <time'+(stamp(value)!==null?' datetime="'+esc(value)+'"':'')+' title="'+esc(dateLabel(value))+'">'+esc(dateLabel(value))+'</time></span>';
        return'<div class="iac-session"><div class="iac-session-heading"><b>'+esc(text(session.agent)||'Session')+'</b>'
          +(text(session.runtime)?'<span class="iac-runtime">'+esc(session.runtime)+'</span>':'')
          +(!project?'<span>'+esc(names.get(pid)||pid||'Unassigned project')+'</span>':'')+'</div>'
          +'<div class="iac-session-times">'+sessionTime('Opened',session.opened_at)+sessionTime('Last active',session.last_activity_at)+'</div>'
          +'<code class="iac-session-id">'+esc(session.session_id)+'</code></div>';
      }).join(''):'<p class="iac-note">'+(sessions===null?(liveLoading?'Checking this selection…':'Try Refresh to check again.'):'No open sessions are reported for this selection.')+'</p>';
    }
    function renderTodos(){
      if(sharing)return;
      const details=$('[data-activity-todos]'),summary=$('[data-activity-todos-summary]');
      details.hidden=!project;$('[data-activity-todo-scope]').hidden=Boolean(project);
      if(!project)return;
      summary.textContent=todos===null?(todosLoading?'Loading project todos…':'Project todos unavailable'):todos.length+' '+(todos.length===1?'todo':'todos');
      if(todosLoading&&todos!==null)summary.textContent='Refreshing… · '+summary.textContent;
      const shown=(todos||[]).slice(0,TODO_LIMIT);
      $('[data-activity-todo-rows]').innerHTML=todos===null?'<p class="iac-note">'+(todosLoading?'Loading todos for this project…':'Try Refresh to check again.')+'</p>'
        :!todos.length?'<p class="iac-note">No todos are recorded for this project.</p>'
        :shown.map(todo=>'<div class="iac-todo"><span class="iac-todo-status st-'+esc(todo.status)+'">'+esc(human(todo.status))+'</span>'
          +'<span class="iac-todo-content'+(['completed','cancelled'].includes(todo.status)?' is-done':'')+'">'+esc(todo.content)+'</span>'
          +(todo.runtime?'<span class="iac-runtime">'+esc(todo.runtime)+'</span>':'')+'</div>').join('')
          +(todos.length>shown.length?'<p class="iac-note">Showing '+shown.length+' of '+todos.length+' todos · '+(todos.length-shown.length)+' more</p>':'');
    }
    function rowHTML(row){
      const subject=sharing?row.subject:names.get(row.projectId)||row.subject||'Workspace';
      const projectLink=sharing?'<a href="#/inbox/sharing">'+esc(subject)+'</a>':row.projectId
        ?'<button type="button" data-activity-project="'+esc(row.projectId)+'">'+esc(subject)+'</button>':'<span>'+esc(subject)+'</span>';
      return'<span class="iac-dot'+(row.tone?' is-'+row.tone:'')+'" aria-hidden="true"></span><div class="iac-event-body"><div class="iac-event-title"><b>'+esc(row.label)+'</b>'+projectLink+'</div>'
        +(row.detail?'<p class="iac-event-detail">'+esc(row.detail)+'</p>':'')
        +((row.runtime||row.sessionId)?'<p class="iac-event-source">'+esc([row.runtime,row.sessionId?'Session '+row.sessionId:''].filter(Boolean).join(' · '))+'</p>':'')
        +'</div><div class="iac-event-time"><time'+(row.ms!==null?' datetime="'+esc(row.ts)+'"':'')+'>'+esc(dateLabel(row.ts))+'</time><span data-activity-relative></span></div>';
    }
    function renderRows(rows){
      const list=$('[data-activity-rows]'),wanted=new Set(rows.map(row=>row.key));
      for(const [key,node] of rowNodes)if(!wanted.has(key)){node.remove();rowNodes.delete(key);}
      let anchor=list.firstElementChild;
      for(const row of rows){
        let node=rowNodes.get(row.key);
        if(!node){node=document.createElement('li');node.className='iac-event';rowNodes.set(row.key,node);}
        const markup=rowHTML(row);
        if(node.dataset.markup!==markup){node.innerHTML=markup;node.dataset.markup=markup;}
        node.querySelector('[data-activity-relative]').textContent=rel(row.ts);
        if(node===anchor)anchor=anchor.nextElementSibling;else list.insertBefore(node,anchor);
      }
    }
    function render(){
      if(!root)return;
      renderFilter();renderLive();renderTodos();
      const rows=filterActivityEvents(events,{query,project,names});renderRows(rows);
      $('[data-activity-summary]').textContent=(loading&&hasSnapshot?'Refreshing… · ':'')+(hasSnapshot?rows.length+' of '+events.length+' loaded events':feedError?'Activity unavailable':'Loading activity…');
      const warnings=[feedError,catalogError,liveError,todosError,invalidRows?'Some activity records could not be read.':''];
      if(sharing&&snapshot?.cadence==='parked')warnings.push('Sharing is paused. Open Sharing for its connection status.');
      const warning=$('[data-activity-warning]');warning.textContent=warnings.filter(Boolean).join(' ');warning.hidden=!warning.textContent;
      const empty=$('[data-activity-empty]');empty.hidden=rows.length>0;
      empty.textContent=!hasSnapshot?(loading?'Loading activity…':'Activity is unavailable.')
        :query||project?'No loaded events match this selection.':sharing?'No sharing events have been recorded since this server started.':'No project events have been recorded yet.';
      const more=$('[data-activity-more]');more.hidden=sharing||!nextCursor;more.disabled=loading||loadingMore||!!pending;
      more.textContent=loadingMore?'Loading…':'Load older events';
      $('[data-activity-updated]').textContent=lastLoaded?'Updated '+dateLabel(lastLoaded):'';
    }
    function selectProject(value,{handoff=false}={}){
      project=value;
      if(sharing){render();return;}
      if(handoff){query='';refreshToolbar();}
      ++generation;cancelReads();events=[];hasSnapshot=false;loadedOlder=false;nextCursor=null;lastLoaded=null;
      feedError='';liveError='';todosError='';invalidRows=false;sessions=null;todos=null;
      if(project){$('[data-activity-live]').open=true;$('[data-activity-todos]').open=true;}
      return refresh();
    }
    function openPendingProject(){
      if(sharing||pendingProject===null)return false;
      const selected=pendingProject;pendingProject=null;
      selectProject(selected,{handoff:true});return true;
    }
    return{
      ...INBOX_PAGES.find(page=>page.id===id),section:id,
      toolbar:{search:{placeholder:sharing?'Search sharing activity…':'Search activity…',label:'Search loaded '+title.toLowerCase(),
        getValue:()=>query,setValue:value=>{query=String(value??'');render();}}},
      mount(el,ctx){
        root=el;go=ctx.switchTo;refreshToolbar=ctx.refreshToolbar||(()=>{});
        root.innerHTML='<div class="iac"><header class="iac-head"><h1>'+title+'</h1><p data-activity-summary role="status"></p></header>'
          +'<div class="iac-controls"><label for="'+id+'-project">'+(sharing?'Repository':'Project')+'</label><select id="'+id+'-project" data-activity-project-filter><option value="">All</option></select>'
          +'<span class="iac-retention">'+(sharing?'Latest 50 events · cleared when the server restarts':'Recent workspace events · search covers loaded events')+'</span></div>'
          +'<p class="iac-warning" data-activity-warning role="status" hidden></p>'
          +(sharing?'':'<details class="iac-live" data-activity-live><summary data-activity-live-summary>Checking open sessions…</summary><div data-activity-live-rows></div></details>'
            +'<p class="iac-todo-scope" data-activity-todo-scope>Select a project to see its todos and current sessions.</p>'
            +'<details class="iac-live iac-todos" data-activity-todos hidden><summary data-activity-todos-summary>Project todos</summary><div data-activity-todo-rows></div></details>')
          +'<p class="iac-empty" data-activity-empty>Loading activity…</p><ol class="iac-events" data-activity-rows></ol>'
          +'<footer class="iac-footer"><button type="button" class="inb-btn" data-activity-more hidden>Load older events</button><span data-activity-updated></span></footer></div>';
        $('[data-activity-project-filter]').addEventListener('change',event=>{
          selectProject(event.target.value);
        });
        if(!sharing)addEventListener('space:activity-project',()=>{
          if(active&&location.hash==='#/inbox/activity')openPendingProject();
        });
        $('[data-activity-more]').addEventListener('click',()=>refresh({older:true}));
        root.addEventListener('click',async event=>{
          const button=event.target.closest('[data-activity-project]');if(!button)return;
          const target=button.dataset.activityProject;
          if(await go('projects/files/list')===true&&location.hash==='#/projects/files/list')dispatchEvent(new CustomEvent('space:open-project',{detail:target}));
        });
      },
      show(){
        active=true;clearInterval(poll);poll=setInterval(()=>{if(!pending)refresh();},pollMs);
        openPendingProject();
        // Complete navigation before reads so a project handoff can replace
        // the initial workspace request without waiting for unrelated data.
        refresh().catch(error=>console.error('Activity refresh failed:',error));
      },
      hide(){active=false;clearInterval(poll);poll=null;++generation;cancelReads();loading=false;loadingMore=false;liveLoading=false;todosLoading=false;},
      refresh:()=>refresh(),
    };
  }
}
