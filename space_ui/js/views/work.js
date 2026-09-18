/* Inbox: everything that needs a person, in one list (docs/work-and-workitems.md,
   sections 6.1 and 16). Four groups: decisions (the attention items), the
   calendar (meetings coming up), completed (jobs, closed work and done todos
   that want a look), work (the open work items). A group card narrows the
   list to that group; every row has one primary action and a way to put it
   away (Dismiss or Acknowledge). One read paints the page: GET
   /api/work/inbox answers the four groups, the counts and the badge; every
   action is one call to /api/work/* or the work item routes, then a reread.
   Every visual element is a shadcn/ui component from js/core/shadcn.js;
   css/work.css only lays the pieces out. Every string reaching innerHTML is
   escaped: rows are agent, provider and person content. The view names no
   agent: labels come from /api/telemetry/sources and colors from the kit. */
import {esc,rel,toast} from '../core/ui.js';
import {API_BASE,apiFetch,failText} from '../core/api.js';
import {setSlottedInterval,clearSlottedInterval} from '../core/store.js';
import {WORK_PAGES} from '../core/navigation.js?v=20260919-work4';
import {setSectionActions} from '../core/section-nav.js?v=20260919-work4';
import {icons,button,badge,card,nativeSelect,empty,item,input,field,label,alert,textarea} from '../core/shadcn.js?v=20260919-work4';

/* the four groups: what each holds, in one line, on its card */
const GROUPS=[
  {value:'decisions',label:'Decisions',icon:'bell',hint:'assigned, blocked, unanswered, failing'},
  {value:'calendar',label:'Calendar',icon:'calendar',hint:'meetings today and tomorrow'},
  {value:'completed',label:'Completed',icon:'checkCircle',hint:'jobs, closed work and done todos'},
  {value:'work',label:'Work',icon:'briefcase',hint:'open work items and who owns them'},
];
const TITLES={decisions:'Needs a decision',calendar:'Coming up',completed:'Completed, for a look',work:'Open work'};
/* reason -> [label, primary action] (design sections 9 and 16.2) */
const REASON={assigned_to_me:['assigned to you','Claim'],unassigned:['unassigned','Assign to me'],todo_blocked:['blocked','Open project'],
  issue_mine:['issue for you','Track'],connection:['needs a reply','Track'],agent_question:['asks you','Open project'],
  share_pending:['shared with you','Clone'],commits_behind:['behind','Apply'],source_error:['source error','Reconnect'],
  item_new:['arrived','Start session'],item_question:['asks you','Open session'],item_draft:['draft ready','Open workbench'],
  item_task:['task proposed','Track'],item_failed:['session failed','Retry']};
const REFRESH_MS=30000,BADGE_MS=60000;

let root=null,go=()=>{},actions=null;
let data=null,agents=null,loading=false,failed='',shown=false;
let group='all',project='',query='',composer=false,focusTitle=false,pendingItem=null,busy=false;
const expanded=new Set(),hidden=new Set();   /* rows put away this paint, before the reread lands */
/* the item threads: which are open, what each holds, and the refs the rows were painted with */
const openThreads=new Set(),threads=new Map(),itemRefs=new Map();
const THREAD_POLL_MS=2000;

/* ── names ────────────────────────────────────────────────────────────── */
const ME=()=>data?.me||['me'];
const isMe=v=>!!v&&ME().some(m=>String(m).toLowerCase()===String(v).toLowerCase().replace(/^@/,''));
const myRuntime=()=>ME().find(v=>v!=='me')||'local';
const agentLabel=id=>agents?.find(a=>a.id===id)?.label||String(id||'').replaceAll('_',' ');
const isAgent=id=>!!agents?.some(a=>a.id===id);
const who=id=>!id?'':isMe(id)?'<span>you</span>':isAgent(id)?badge(esc(agentLabel(id)),{variant:'outline',source:id}):'<span>@'+esc(String(id).replace(/^@/,''))+'</span>';
const dtfmt=iso=>{const t=Date.parse(iso);return Number.isFinite(t)?new Date(t).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';};
const tfmt=iso=>new Date(iso).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
function until(iso){
  const s=(Date.parse(iso)-Date.now())/1000;
  if(!Number.isFinite(s))return'';
  if(s<0)return'now';
  if(s<3600)return'in '+Math.max(1,Math.floor(s/60))+' min';
  if(s<86400)return'in '+Math.floor(s/3600)+' h';
  return'tomorrow';
}
const dot=tone=>'<i class="work-dot" data-tone="'+esc(tone)+'" aria-hidden="true"></i>';
const hit=(terms,values)=>!terms.length||terms.every(t=>values.map(v=>String(v??'')).join(' ').toLowerCase().includes(t));
const terms=()=>query.trim().toLowerCase().split(/\s+/).filter(Boolean);
const projects=()=>data?.projects||[];
function assigneeOptions(current){
  const out=[{value:'',label:'Unassigned'},{value:'me',label:'You'}];
  for(const a of agents||[])out.push({value:a.id,label:a.label||a.id});
  const seen=new Set(out.map(o=>o.value));
  for(const w of data?.work||[]){
    const v=w.assignee;
    if(v&&!seen.has(v)&&!isMe(v)){seen.add(v);out.push({value:v,label:'@'+v});}
  }
  if(current&&!seen.has(current)&&!isMe(current))out.push({value:current,label:'@'+current});
  return out;
}

/* ── the four groups, narrowed by the page's own state ────────────────── */
const alive=list=>list.filter(x=>!hidden.has(x.key));
const decisions=()=>alive(data?.decisions||[]).filter(a=>(!project||a.project_id===project)&&hit(terms(),[a.title,a.detail,a.project_id,a.source,REASON[a.reason]?.[0]]));
const meetings=()=>alive(data?.calendar||[]).filter(e=>hit(terms(),[e.title,e.detail,e.toolkit]));
const completed=()=>alive(data?.completed||[]).filter(c=>(!project||c.project_id===project)&&hit(terms(),[c.title,c.detail,c.project_id,c.actor?.runtime]));
const work=()=>(data?.work||[]).filter(w=>(!project||w.project_id===project)
  &&hit(terms(),[w.title,w.project_id,isMe(w.assignee)?'you mine':w.assignee,w.assignee?'':'unassigned',w.body,w.issue?'#'+w.issue.number:'',...(w.labels||[])]));

/* ── painting ─────────────────────────────────────────────────────────── */
function render(){
  if(!root)return;
  if(!data){
    root.innerHTML='<div class="workwrap"><header class="work-page-head"><h1>Inbox</h1><p role="status">'+(loading?'loading…':esc(failed||''))+'</p></header>'
      +(failed&&!loading?alert({variant:'destructive',title:'The Inbox could not be read',description:esc(failed)}):'')+'</div>';
    return;
  }
  const drafts=new Map();   /* what is typed in an open reply box survives the repaint */
  for(const box of root.querySelectorAll('[data-ikey] [data-field="reply"]'))if(box.value)drafts.set(box.closest('[data-ikey]').dataset.ikey,box.value);
  const lists={decisions:decisions(),calendar:meetings(),completed:completed(),work:work()};
  const keys=group==='all'?GROUPS.map(g=>g.value):[group];
  const total=keys.reduce((n,k)=>n+lists[k].length,0);
  const waiting=lists.decisions.length+lists.completed.length;
  root.innerHTML='<div class="workwrap">'
    +'<header class="work-page-head"><h1>Inbox</h1><p role="status">'+(waiting?waiting+' waiting for you':'nothing waiting')+' · '+lists.work.length+' open work items'
      +(data.counts?.running?' · '+data.counts.running+(data.counts.running===1?' session running':' sessions running'):'')
      +(failed?' · <span class="work-error">'+esc(failed)+'</span>':'')+'</p></header>'
    +'<div class="work-groups" role="group" aria-label="Groups">'+GROUPS.map(g=>groupCard(g,lists[g.value].length)).join('')+'</div>'
    +(composer?composerHTML():'')
    +'<div class="work-toolbar">'
      +(group==='all'?'<span class="work-count">Everything, newest first in each group</span>'
        :badge(esc(GROUPS.find(g=>g.value===group).label)+' <span aria-hidden="true">×</span>',{variant:'secondary',cls:'work-chip',attrs:'role="button" tabindex="0" data-chip="group" aria-label="Show every group"'}))
      +'<span class="work-spacer"></span>'
      +nativeSelect({options:[{value:'',label:'All projects'},...projects().map(p=>({value:p.id,label:p.id}))],value:project,size:'sm',ariaLabel:'Project',attrs:'data-act="project-filter"'})
    +'</div>'
    +(total?card({cls:'work-list',content:keys.filter(k=>lists[k].length).map(k=>
        '<div class="work-day">'+icons[GROUPS.find(g=>g.value===k).icon]+esc(TITLES[k])+' · '+lists[k].length+'</div>'+lists[k].map(ROW[k]).join('')).join('')})
      :empty({size:'sm',icon:icons.inbox,title:group==='all'?'Nothing needs you':'Nothing here',description:query||project?'Nothing in this selection.':'Decisions, meetings, finished work and open work items land here.'}))
  +'</div>';
  for(const [key,text] of drafts){const box=root.querySelector('[data-ikey="'+CSS.escape(key)+'"] [data-field="reply"]');if(box&&!box.disabled)box.value=text;}
  if(focusTitle){focusTitle=false;root.querySelector('#wi-title')?.focus();}
  paintBadge(alive(data.decisions).length+alive(data.completed).length);
}
/* one card per group: the count, what the group holds, and a press state
   that narrows the list to it (press again for everything) */
function groupCard(g,n){
  const on=group===g.value;
  return card({stat:true,tone:g.value==='decisions'&&n?'primary':null,cls:'work-group'+(on?' is-on':''),
    attrs:'role="button" tabindex="0" aria-pressed="'+(on?'true':'false')+'" data-group-card="'+esc(g.value)+'"',
    description:icons[g.icon]+esc(g.label),title:String(n),footer:esc(n?g.hint:'nothing here')});
}
function decisionRow(a){
  const [text,primary]=REASON[a.reason]||[a.reason,'Open'];
  const isItem=!!a.ref?.item_id,open=isItem&&openThreads.has(a.key);
  if(isItem)itemRefs.set(a.key,a.ref);
  return item({size:'sm',cls:'work-row'+(open?' is-open':''),attrs:'data-akey="'+esc(a.key)+'"'+(isItem?' data-ikey="'+esc(a.key)+'"':''),
    title:dot(a.tone==='error'?'error':'attention')
      +(isItem?'<button type="button" class="work-toggle" data-act="toggle-item" aria-expanded="'+(open?'true':'false')+'"><span class="work-title">'+esc(a.title)+'</span></button>'
        :'<span class="work-title">'+esc(a.title)+'</span>')+badge(esc(text),{variant:'outline'}),
    description:open?'':esc(a.detail||''),
    footer:open?threadHTML(a.key):'',
    actions:(a.project_id?badge(esc(a.project_id),{variant:'secondary'}):a.source?'<span>'+esc(a.source)+'</span>':'')
      +(a.since?'<time class="work-time" datetime="'+esc(a.since)+'" title="'+esc(dtfmt(a.since))+'">'+esc(rel(a.since))+'</time>':'')
      +button(primary,{variant:'outline',size:'sm',attrs:'data-act="primary"'})
      +(a.ref?.can_send?button('Send',{variant:'outline',size:'sm',attrs:'data-act="send"'}):'')
      +button('Dismiss',{variant:'ghost',size:'sm',attrs:'data-act="dismiss"'})});
}
function meetingRow(e){
  const live=Date.parse(e.starts)<=Date.now();
  return item({size:'sm',cls:'work-row',attrs:'data-mkey="'+esc(e.key)+'" data-since="'+esc(e.starts)+'"',
    title:'<span class="work-cal-time">'+esc(tfmt(e.starts))+'</span>'+dot(live?'pending':'info')+'<span class="work-title">'+esc(e.title)+'</span>'
      +badge(live?'now':esc(until(e.starts)),{variant:live?'default':'outline'}),
    description:esc(e.detail||''),
    actions:'<span>'+esc(e.toolkit||'')+'</span>'
      +(e.ref?.url?button(live?'Join':'Open',{tag:'a',variant:'outline',size:'sm',attrs:'href="'+esc(e.ref.url)+'" target="_blank" rel="noopener noreferrer"'}):'')
      +button('Dismiss',{variant:'ghost',size:'sm',attrs:'data-act="dismiss-meeting"'})});
}
function completedRow(c){
  const runtime=c.actor?.runtime;
  const isItem=!!c.ref?.item_id,open=isItem&&openThreads.has(c.key);
  if(isItem)itemRefs.set(c.key,c.ref);
  return item({size:'sm',cls:'work-row'+(open?' is-open':''),attrs:'data-ckey="'+esc(c.key)+'"'+(isItem?' data-ikey="'+esc(c.key)+'"':'')+(c.ref?.workitem_id?' data-wid="'+esc(c.ref.workitem_id)+'" data-wproject="'+esc(c.ref.project_id||c.project_id||'')+'"':''),
    title:dot(c.tone)+badge(c.kind==='job'?'Job':c.kind==='workitem'?'Work item':c.kind==='item'?'Item':'Todo',{variant:'outline'})
      +(isItem?'<button type="button" class="work-toggle" data-act="toggle-item" aria-expanded="'+(open?'true':'false')+'"><span class="work-title">'+esc(c.title)+'</span></button>'
        :'<span class="work-title">'+esc(c.title)+'</span>'),
    description:open?'':esc(c.detail||''),
    footer:open?threadHTML(c.key):'',
    actions:(c.project_id?badge(esc(c.project_id),{variant:'secondary'}):'')+who(runtime)
      +'<time class="work-time" datetime="'+esc(c.ts)+'" title="'+esc(dtfmt(c.ts))+'">'+esc(rel(c.ts))+'</time>'
      +(c.primary?button(c.primary==='output'?'Output':'Accept',{variant:'outline',size:'sm',attrs:'data-act="'+esc(c.primary)+'"'}):'')
      +(c.kind==='workitem'?button('Reopen',{variant:'ghost',size:'sm',attrs:'data-act="reopen"'}):'')
      +button(c.kind==='workitem'?'Dismiss':'Acknowledge',{variant:'ghost',size:'sm',attrs:'data-act="ack"'})});
}
function workRow(w){
  const open=expanded.has(w.id);
  return item({size:'sm',cls:'work-row'+(open?' is-open':''),attrs:'data-id="'+esc(w.id)+'" data-project="'+esc(w.project_id)+'"',
    title:dot(w.in_progress?'progress':'info')
      +'<button type="button" class="work-toggle" data-act="toggle" aria-expanded="'+(open?'true':'false')+'">'
        +'<span class="work-title">'+esc(w.title)+'</span>'
        +badge(esc(w.project_id),{variant:'secondary'})
        +(w.issue?badge('#'+esc(String(w.issue.number)),{variant:'outline',mono:true}):'')
        +(w.in_progress?badge('in progress',{variant:'outline'}):'')
        +(w.stale?badge('issue gone',{variant:'outline'}):'')
      +'</button>',
    actions:(w.assignee?who(w.assignee):'<span class="work-muted-i">unassigned</span>')
      +'<time class="work-time" datetime="'+esc(w.updated_at||'')+'" title="'+esc(dtfmt(w.updated_at))+'">'+esc(rel(w.updated_at))+'</time>',
    footer:open?recordHTML(w):''});
}
const ROW={decisions:decisionRow,calendar:meetingRow,completed:completedRow,work:workRow};
/* The record: body or issue, who is on it, then the two things a person
   changes here. GitHub owns the status of an adopted item, so that button
   is a link to the issue. */
function recordHTML(w){
  const issueLink=w.issue?.url?'<a href="'+esc(w.issue.url)+'" target="_blank" rel="noopener noreferrer">'+esc((w.issue.repo||'')+' #'+w.issue.number)+'</a>':esc((w.issue?.repo||'')+' #'+(w.issue?.number||''));
  const body=w.origin==='github'
    ?'<div class="work-detail">Body and status live on the issue. '+issueLink+(w.github_assignees?.length?' · on GitHub: '+esc(w.github_assignees.join(', ')):'')+'</div>'
    :'<div class="work-detail">'+esc(w.body||'No description.')+'</div>';
  const claim=w.in_progress&&w.claim?'<div class="work-detail">'+esc(agentLabel(w.claim.runtime))+' is on it, session '+esc(String(w.claim.session_id||'').slice(0,8))+(w.claim.started_at?', since '+esc(rel(w.claim.started_at)):'')+'.</div>':'';
  const labels=w.labels?.length?'<div class="work-detail">'+w.labels.map(l=>badge(esc(l),{variant:'outline'})).join(' ')+'</div>':'';
  return body+claim+labels
    +'<div class="work-assign">'+label('Assignee',{htmlFor:'wi-assignee-'+esc(w.id)})
      +nativeSelect({id:'wi-assignee-'+w.id,options:assigneeOptions(w.assignee),value:isMe(w.assignee)?'me':(w.assignee||''),size:'sm',attrs:'data-act="assignee"'})+'</div>'
    +'<div class="work-acts">'
      +(w.origin==='github'&&w.issue?.url?button('Change on GitHub',{tag:'a',variant:'ghost',size:'sm',attrs:'href="'+esc(w.issue.url)+'" target="_blank" rel="noopener noreferrer"'})
        :button('Close',{variant:'outline',size:'sm',attrs:'data-act="status"'}))
      +button('Open project',{variant:'ghost',size:'sm',attrs:'data-act="project"'})
      +button('Delete',{variant:'ghost',size:'sm',attrs:'data-act="delete"'})
    +'</div>';
}
function composerHTML(){
  const list=projects();
  return card({title:'New work item',
    content:'<div class="work-fields">'
      +field({label:'Title',htmlFor:'wi-title',control:input({id:'wi-title',placeholder:'What needs doing',attrs:'data-field="title"'})})
      +field({label:'Project',htmlFor:'wi-project',control:nativeSelect({id:'wi-project',full:true,options:list.map(p=>({value:p.id,label:p.id})),value:project||list[0]?.id||'',attrs:'data-field="project"'})})
      +field({label:'Assignee',htmlFor:'wi-assignee',control:nativeSelect({id:'wi-assignee',full:true,options:assigneeOptions(),value:'',attrs:'data-field="assignee"'})})
    +'</div>',
    footer:button('Create',{size:'sm',attrs:'data-act="create"'})+button('Cancel',{variant:'ghost',size:'sm',attrs:'data-act="cancel"'})});
}

/* ── the item's thread: the mail, the turns, the reply box ─────────────── */
const OUTCOME_LABEL={reply_drafted:'reply drafted',task_proposed:'task proposed',needs_you:'asks you',fyi:'for your information',handled:'handled'};
function msgHTML(role,who,when,body,{outcome='',pending=false,url=''}={}){
  return'<div class="work-msg" data-role="'+esc(role)+'"'+(pending?' data-pending':'')+'>'
    +'<div class="work-msg-head"><b>'+who+'</b>'+(when?'<time datetime="'+esc(when)+'" title="'+esc(dtfmt(when))+'">'+esc(rel(when))+'</time>':'')
      +(outcome?badge(esc(OUTCOME_LABEL[outcome]||outcome),{variant:'secondary'}):'')+'</div>'
    +'<div class="work-msg-body">'+body+(url?' <a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">Open</a>':'')+'</div></div>';
}
function threadHTML(key){
  const t=threads.get(key),ref=itemRefs.get(key);
  if(!t)return'<div class="work-thread"><div class="work-thread-note">'+(threads.has(key)?'':'Loading the thread…')+'</div></div>';
  if(t.error)return'<div class="work-thread"><div class="work-thread-note work-error">'+esc(t.error)+'</div></div>';
  const it=t.item||{};
  let html=msgHTML('item',esc(it.title||'')+' <span>· '+esc((it.connection||ref?.toolkit||'')+' · '+(it.collector||''))+'</span>',it.ts,esc(it.body||'(no body)'),{url:it.url||''});
  for(const m of t.thread||[]){
    const who=m.type==='person'?'You':m.type==='agent'?esc(agentLabel(t.session?.runtime||''))||'Agent':'Note';
    html+=msgHTML(m.type,who,m.ts,esc(m.text||''),{outcome:m.outcome||''});
  }
  if(t.running)html+=msgHTML('agent',esc(agentLabel(t.session?.runtime||''))||'Agent','','<i class="work-dot" data-tone="progress" aria-hidden="true"></i> is answering…',{pending:true});
  const o=t.outcome;
  if(o&&!t.running)html+='<div class="work-thread-note">'+badge(esc(OUTCOME_LABEL[o.kind]||o.kind),{variant:'outline'})+' '+esc(o.summary||'')
    +(o.draft?' · draft in <code>'+esc(o.draft)+'</code>':'')+(o.task?.title?' · task: '+esc(o.task.title):'')+(o.question?' · '+esc(o.question):'')+'</div>';
  html+='<div class="work-reply">'
    +textarea({placeholder:t.running?'The agent is answering; your next message can wait a moment.':(t.thread||[]).length?'Reply to the agent…':'Tell the agent what to do with this item…',rows:3,disabled:!t.can_reply||t.running,attrs:'data-field="reply" aria-label="Reply"'})
    +'<div class="work-reply-foot"><span>'+(t.can_reply?'⌘↩ sends. The answer lands here.':'Sessions are off for this connection.')+'</span>'
      +'<span>'+(t.can_send?button('Send the draft',{variant:'outline',size:'sm',attrs:'data-act="send"'}):'')
      +button('Reply',{size:'sm',attrs:'data-act="reply"'+(!t.can_reply||t.running?' disabled':'')})+'</span></div>'
  +'</div>';
  return'<div class="work-thread">'+html+'</div>';
}
async function loadThread(key,{quiet=false}={}){
  const ref=itemRefs.get(key);
  if(!ref)return;
  if(!quiet&&!threads.has(key)){threads.set(key,null);render();}
  const res=await apiFetch(API_BASE+itemPath(ref,'/thread'));
  const was=threads.get(key);
  threads.set(key,res.ok?res.data:{error:failText(res)});
  if(!openThreads.has(key))return;
  const now=threads.get(key);
  if(!was||was.error||!now||now.error||JSON.stringify(was.thread)!==JSON.stringify(now.thread)||was.running!==now.running){
    render();
  }
  if(was&&was.running&&now&&!now.running)await load({quiet:true});   /* the row's reason changed with the answer */
}
function pollThreads(){
  setSlottedInterval('work-thread',()=>{
    for(const key of openThreads){const t=threads.get(key);if(t&&t.running)loadThread(key,{quiet:true});}
  },THREAD_POLL_MS);
}
async function toggleThread(key){
  if(openThreads.has(key))openThreads.delete(key);else openThreads.add(key);
  render();
  if(openThreads.has(key)){await loadThread(key);root.querySelector('[data-ikey="'+CSS.escape(key)+'"] [data-field="reply"]')?.focus({preventScroll:true});}
}
async function reply(key){
  const ref=itemRefs.get(key),box=root.querySelector('[data-ikey="'+CSS.escape(key)+'"] [data-field="reply"]');
  const text=box?.value.trim();
  if(!ref)return;
  if(!text){box?.focus();toast('Write something first');return;}
  const res=await call(itemPath(ref,'/reply'),{method:'POST',body:{text}});
  if(!res)return;
  if(box)box.value='';
  const t=threads.get(key);
  if(t&&!t.error){t.thread=[...(t.thread||[]),{ts:new Date().toISOString(),type:'person',text}];t.running=true;}
  render();
}

/* ── tab badge: what awaits a person (decisions and completed), nothing else */
let lastBadge=-1;
function paintBadge(n){
  const tab=document.getElementById('tab-work');
  if(!tab)return;
  n=Math.max(0,Math.floor(Number(n)||0));
  if(n===lastBadge)return;
  lastBadge=n;
  const b=tab.querySelector('.work-badge');
  if(n>0){if(b)b.textContent=String(n);else tab.insertAdjacentHTML('beforeend',badge(String(n),{variant:'default',cls:'work-badge'}));}
  else if(b)b.remove();
}
export async function refreshWorkBadge(){
  const res=await apiFetch(API_BASE+'/api/work/summary');
  if(res.ok&&res.data)paintBadge(res.data.badge);
}
export function initWorkBadge(){
  refreshWorkBadge();
  setSlottedInterval('work-badge',()=>{if(!shown)refreshWorkBadge();},BADGE_MS);   /* the page's own read feeds it while shown */
}

/* ── loading ──────────────────────────────────────────────────────────── */
async function load({quiet=false}={}){
  if(!quiet){loading=true;render();}
  const [res,src]=await Promise.all([apiFetch(API_BASE+'/api/work/inbox'),agents?null:apiFetch(API_BASE+'/api/telemetry/sources')]);
  loading=false;
  if(src?.ok)agents=src.data.items||[];else if(src&&!agents)agents=[];
  if(res.ok&&res.data){data=res.data;failed='';hidden.clear();for(const key of openThreads)loadThread(key,{quiet:true});}
  else failed=failText(res);   /* a failed reread keeps the previous page visible with the reason */
  render();
}
function startPoll(){setSlottedInterval('work-inbox',()=>{if(shown&&!busy)load({quiet:true});},REFRESH_MS);pollThreads();}

/* ── actions: one call, then a reread ─────────────────────────────────── */
async function call(path,opts,okText){
  busy=true;
  const res=await apiFetch(API_BASE+path,opts);
  busy=false;
  if(!res.ok){toast(failText(res));await load({quiet:true});return null;}
  if(okText)toast(okText);
  return res;
}
const wiPath=(p,id)=>'/api/xo-projects/'+encodeURIComponent(p)+'/workitems'+(id?'/'+encodeURIComponent(id):'');
async function openProject(id){
  if(!id)return;
  if(await go('projects/data/list')===true&&location.hash==='#/projects/data/list')dispatchEvent(new CustomEvent('space:open-project',{detail:id}));
}
function toggle(id){
  if(!id)return;
  if(expanded.has(id))expanded.delete(id);else expanded.add(id);
  render();
  root.querySelector('[data-id="'+CSS.escape(id)+'"] .work-toggle')?.focus({preventScroll:true});
}
async function putAway(key,since,kind){
  hidden.add(key);render();   /* leaves at once; the reread confirms */
  const res=await call(kind==='ack'?'/api/work/ack':'/api/work/dismiss',{method:'POST',body:kind==='ack'?{key}:{key,since:since||null}});
  if(res)await load({quiet:true});
}
async function create(){
  const title=root.querySelector('[data-field="title"]')?.value.trim();
  const pid=root.querySelector('[data-field="project"]')?.value;
  if(!title){root.querySelector('#wi-title')?.focus();toast('A work item needs a title');return;}
  if(!pid){toast('Create a project first');return;}
  const assignee=root.querySelector('[data-field="assignee"]')?.value||null;
  const res=await call(wiPath(pid),{method:'POST',body:{runtime:myRuntime(),title,assignee:assignee==='me'?myRuntime():assignee}},'Work item created');
  if(!res)return;
  composer=false;if(group!=='all')group='work';expanded.add(res.data.id);
  await load({quiet:true});
}
/* the connection folders' items (design section 17): one call per action, then a reread */
const itemPath=(ref,tail='')=>'/api/work/inbox/items/'+encodeURIComponent(ref.toolkit)+'/'+encodeURIComponent(ref.item_id)+tail;
async function startItem(a,retry){
  hidden.add(a.key);render();
  const res=await call(itemPath(a.ref,'/start'+(retry?'?retry=true':'')),{method:'POST'},retry?'Session started again':'Session started');
  if(res)await load({quiet:true});
}
async function decideItem(ref,action,extra,okText){
  const res=await call(itemPath(ref,'/decide'),{method:'POST',body:{action,...extra}},okText);
  if(res)await load({quiet:true});
}
async function openSession(ref){
  const id=ref?.native_session_id||ref?.session_id;
  if(!id){toast('No session yet');return;}
  if(await go('agents/sessions')===true)dispatchEvent(new CustomEvent('space:open-session',{detail:{id}}));
}
function openWorkbench(ref){
  toast('Draft in '+ref.project_id+'/'+ref.workbench+(ref.draft?'/'+ref.draft:''));
  openProject(ref.project_id);
}
async function track(a){
  const pid=a.project_id||project||projects()[0]?.id;
  if(!pid){toast('Create a project first: a work item lives in one');return;}
  const res=await call('/api/work/promote',{method:'POST',body:{key:a.key,project_id:pid}});
  if(!res)return;
  toast(res.status===201?'Tracked as a work item in '+pid:'Already tracked in '+res.data.project_id);
  hidden.add(a.key);expanded.add(res.data.workitem_id);
  await load({quiet:true});
}
async function primary(a){
  if(!a)return;
  const wid=a.ref?.workitem_id,pid=a.ref?.project_id||a.project_id;
  switch(a.reason){
    case'assigned_to_me':toast('Claim opens the project so a session can start on it');openProject(pid);break;
    case'unassigned':if(wid&&pid){if(await call(wiPath(pid,wid)+'/assignee',{method:'PUT',body:{assignee:'me'}},'Assigned to you'))await load({quiet:true});}break;
    case'todo_blocked':case'agent_question':openProject(pid);break;
    case'issue_mine':case'connection':await track(a);break;
    case'share_pending':go('work/history');break;
    case'commits_behind':if(pid&&await call('/api/xo-projects/'+encodeURIComponent(pid)+'/apply',{method:'POST'},'Applied'))await load({quiet:true});break;
    case'source_error':go(a.source==='scheduler'?'work/live':'setup/connectors');break;
    case'item_new':case'item_failed':await startItem(a,a.reason==='item_failed');break;
    case'item_question':if(!openThreads.has(a.key))await toggleThread(a.key);break;
    case'item_draft':openWorkbench(a.ref);break;
    case'item_task':await decideItem(a.ref,'track',{project_id:project||a.ref.project_id},'Tracked as a work item');break;
    default:if(a.ref?.url)open(a.ref.url,'_blank','noopener');
  }
}
async function onClick(ev){
  const t=ev.target;
  const gc=t.closest('[data-group-card]');
  if(gc){group=group===gc.dataset.groupCard?'all':gc.dataset.groupCard;render();root.querySelector('[data-group-card="'+CSS.escape(gc.dataset.groupCard)+'"]')?.focus({preventScroll:true});return;}
  if(t.closest('[data-chip="group"]')){group='all';render();return;}
  const row=t.closest('[data-id]'),arow=t.closest('[data-akey]'),mrow=t.closest('[data-mkey]'),crow=t.closest('[data-ckey]');
  const b=t.closest('button[data-act],a[data-act]');
  const w=row?data.work.find(x=>x.id===row.dataset.id):null;
  const a=arow?data.decisions.find(x=>x.key===arow.dataset.akey):null;
  const c=crow?data.completed.find(x=>x.key===crow.dataset.ckey):null;
  if(b){
    switch(b.dataset.act){
      case'toggle':toggle(row?.dataset.id);return;
      case'toggle-item':{const key=t.closest('[data-ikey]')?.dataset.ikey;if(key)await toggleThread(key);return;}
      case'reply':{const key=t.closest('[data-ikey]')?.dataset.ikey;if(key)await reply(key);return;}
      case'primary':await primary(a);return;
      case'dismiss':if(a?.ref?.item_id)await decideItem(a.ref,'dismiss',{},'Dismissed');else if(a)await putAway(a.key,a.since,'dismiss');return;
      case'send':{const key=t.closest('[data-ikey]')?.dataset.ikey,ref=a?.ref?.item_id?a.ref:itemRefs.get(key);
        if(ref){const res=await call(itemPath(ref,'/send'),{method:'POST'},'Sending through '+ref.toolkit);if(res){const th=threads.get(key);if(th&&!th.error)th.running=true;await load({quiet:true});}}return;}
      case'dismiss-meeting':if(mrow)await putAway(mrow.dataset.mkey,mrow.dataset.since,'dismiss');return;
      case'ack':case'accept':if(c?.ref?.item_id)await decideItem(c.ref,'accept',{},'Acknowledged');else if(c){await putAway(c.key,'','ack');if(b.dataset.act==='accept')toast('Accepted');}return;
      case'output':go('work/live');return;
      case'reopen':if(c?.ref?.workitem_id){
          const pid=c.ref.project_id||c.project_id;
          if(await call(wiPath(pid,c.ref.workitem_id),{method:'PATCH',body:{status:'open',state_reason:'reopened'}},'Reopened')){await call('/api/work/ack',{method:'POST',body:{key:c.key}});group=group==='all'?'all':'work';await load({quiet:true});}
        }return;
      case'create':await create();return;
      case'cancel':composer=false;render();return;
      case'status':if(w&&await call(wiPath(w.project_id,w.id),{method:'PATCH',body:{status:'closed',state_reason:'completed'}},'Closed'))await load({quiet:true});return;
      case'project':openProject(w?.project_id);return;
      case'delete':if(w&&await call(wiPath(w.project_id,w.id)+'?runtime='+encodeURIComponent(myRuntime()),{method:'DELETE'},'Deleted')){expanded.delete(w.id);await load({quiet:true});}return;
    }
    return;
  }
  if(row&&!t.closest('a,button,select,label,[data-slot="item-footer"]'))toggle(row.dataset.id);
  const irow=t.closest('[data-ikey]');
  if(irow&&!row&&!t.closest('a,button,select,label,textarea,[data-slot="item-footer"]'))await toggleThread(irow.dataset.ikey);
}
async function onChange(ev){
  const s=ev.target.closest('select');
  if(!s)return;
  if(s.dataset.act==='project-filter'){project=s.value;render();return;}
  if(s.dataset.act!=='assignee')return;
  const w=data?.work.find(x=>x.id===s.closest('[data-id]')?.dataset.id);
  if(!w)return;
  const value=s.value||null;
  if(await call(wiPath(w.project_id,w.id)+'/assignee',{method:'PUT',body:{assignee:value}},value?'Assigned to '+(value==='me'?'you':agentLabel(value)):'Unassigned'))await load({quiet:true});
}
/* History hands a work item over after switching here: show it open. */
function showItem(id){
  const w=data?.work.find(x=>x.id===id);
  const c=data?.completed.find(x=>x.ref?.workitem_id===id);
  if(!w&&!c)return;
  group=w?'work':'completed';project='';expanded.add(id);render();
  root.querySelector('[data-id="'+CSS.escape(id)+'"],[data-wid="'+CSS.escape(id)+'"]')?.scrollIntoView({block:'center'});
}

export default {
  ...WORK_PAGES.find(page=>page.id==='work'),
  toolbar:()=>({search:{placeholder:'Search the inbox…',getValue:()=>query,
    setValue(value){value=String(value??'');if(value===query)return;query=value;render();}}}),
  async mount(el,ctx){
    root=el;go=ctx.switchTo;
    if(!actions){
      actions=document.createElement('div');
      actions.innerHTML=button('+ Work item',{size:'sm',attrs:'data-act="compose"'});
      actions.addEventListener('click',e=>{if(e.target.closest('[data-act="compose"]')){composer=true;focusTitle=true;render();}});
    }
    setSectionActions('work',actions);
    el.addEventListener('click',onClick);
    el.addEventListener('keydown',ev=>{
      if((ev.key==='Enter'||ev.key==='Return')&&(ev.metaKey||ev.ctrlKey)&&ev.target.matches('[data-field="reply"]')){
        ev.preventDefault();const key=ev.target.closest('[data-ikey]')?.dataset.ikey;if(key)reply(key);return;}
      if(ev.key!=='Enter'&&ev.key!==' ')return;
      const target=ev.target.closest('[data-group-card],[data-chip]');
      if(target){ev.preventDefault();target.click();}
    });
    el.addEventListener('change',onChange);
    addEventListener('space:work-open-workitem',ev=>{
      const id=String(ev.detail?.id||'');
      if(!id)return;
      if(location.hash==='#/work'&&data)showItem(id);else pendingItem=id;
    });
    render();
  },
  async show(){
    shown=true;
    await load({quiet:!!data});
    if(pendingItem){const id=pendingItem;pendingItem=null;showItem(id);}
    startPoll();
  },
  hide(){shown=false;clearSlottedInterval('work-inbox');clearSlottedInterval('work-thread');},
  refresh(){load({quiet:!!data});},
};
