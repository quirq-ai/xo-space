/* History: what happened, over a range (docs/work-and-workitems.md,
   section 6.3). A window toggle (today, 7 days, 30 days, all) scopes the
   figures, the charts and the timeline together, split Space | Projects.
   Charts: events per day, the split by source, the busiest hours, a
   16-week heatmap, and the agents' share; every one drawn by
   js/core/chart.js from the loaded events. Below them the timeline
   (js/core/shadcn.js Timeline) with sticky day headers; the kind badge, the
   project chip and the agent tag are clickable narrowings shown as chips.
   The project's sharing, which used to be its own page, is a card in
   Projects. Sample data until wired to GET /api/feed. Every string reaching
   innerHTML is escaped: entries are agent and provider content. */
import {esc,rel,toast} from '../core/ui.js';
import {WORK_PAGES} from '../core/navigation.js?v=20260919-work4';
import {setSectionActions} from '../core/section-nav.js?v=20260919-work4';
import {icons,button,badge,card,toggleGroup,nativeSelect,empty,item,input,field,timeline,timelineItem,timelineSeparator} from '../core/shadcn.js?v=20260919-work4';
import {areaChart,barChartHorizontal,barChartStacked,donutChart,heatmapChart} from '../core/chart.js?v=20260915-typesync1';
import {PROJECTS,AGENTS,workitems,entries,olderEntries,history,marks,sharing} from './work-sample.js?v=20260919-work4';

const SECTIONS=[{value:'space',label:'Space'},{value:'projects',label:'Projects'}];
const WINS=[{value:'today',label:'Today'},{value:'7d',label:'7 days'},{value:'30d',label:'30 days'},{value:'all',label:'All'}];
const WDAYS={today:1,'7d':7,'30d':30,all:null};
const KIND={
  'workitem.created':'Work item created','workitem.adopted':'Issue tracked','workitem.assigned':'Assigned','workitem.claimed':'Work started',
  'workitem.released':'Work released','workitem.closed':'Work item closed','workitem.reopened':'Reopened',
  'session.started':'Session started','session.closed':'Session ended','todo.added':'Todo added','todo.completed':'Todo done',
  'todo.status_changed':'Todo changed','file.edited':'File edited','file.created':'File created','file.burst':'Files edited',
  'project.created':'Project created','peer.sync.applied':'Peer sync','plan.written':'Plan updated','episode.written':'Notes saved',
  'issue.opened':'Issue opened','issue.updated':'Issue updated','issue.closed':'Issue closed',
  'gmail.message':'Mail','googlecalendar.event':'Calendar','slack.mention':'Slack',
  'sharing.fetched':'Commits fetched','sharing.shared_with_you':'Shared with you','sharing.error':'Sharing failed',
  'job.finished':'Job ran','job.failed':'Job failed','agent.note':'Agent note',
};
/* the split by source, in chart order and chart colors */
const GROUPS=[['work','Work','var(--chart-1)'],['agents','Agents','var(--chart-3)'],['issues','Issues','var(--chart-2)'],['connections','Connections','var(--chart-5)'],
  ['sharing','Sharing','var(--chart-6)'],['jobs','Jobs','var(--chart-8)'],['projects','Projects','var(--chart-7)']];
const STEP=/^todo\.(added|completed)$/;   /* the steps inside a session stay out of the timeline (the charts count them) */
const BURST_MS=10*60*1000;
const PAGE=60;

let root=null,go=()=>{},refreshToolbar=()=>{},actions=null;
let section='projects',win='7d',project='',agent='',query='',rows=[],composer=false,pendingProject=null,shares=null,limit=PAGE;
const kinds=new Set(),expanded=new Set();

const projectName=id=>PROJECTS.find(p=>p.id===id)?.name||id;
const agentLabel=id=>AGENTS.find(a=>a.id===id)?.label||id;
const dtfmt=iso=>{const t=Date.parse(iso);return Number.isFinite(t)?new Date(t).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';};
const tfmt=iso=>new Date(iso).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
const pad=n=>String(n).padStart(2,'0');
const dayOf=iso=>{const d=new Date(iso);return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());};
function dayLabel(iso){
  const d=new Date(iso),today=new Date(),y=new Date(today);y.setDate(today.getDate()-1);
  if(d.toDateString()===today.toDateString())return'Today';
  if(d.toDateString()===y.toDateString())return'Yesterday';
  return d.toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'});
}
const winLabel=()=>WINS.find(w=>w.value===win)?.label||'';
const winText=()=>win==='all'?'all time':win==='today'?'today':'the last '+winLabel().toLowerCase();
const cutoff=()=>{const d=WDAYS[win];if(d==null)return 0;const c=new Date();c.setHours(0,0,0,0);c.setDate(c.getDate()-(d-1));return c.getTime();};
const recordId=e=>e.ref?.workitem||marks.tracked.get(e.key)||null;
const heroCard=(text,value,secondary,secondaryLabel,{tone=null,action=''}={})=>card({stat:'hero',tone,description:text,title:esc(String(value)),action,
  footer:'<span class="work-hero-secondary"><b>'+esc(String(secondary))+'</b><span>'+esc(secondaryLabel)+'</span></span>'});
function groupOf(e){
  if(e.kind.startsWith('workitem.'))return'work';
  if(e.source==='issues')return'issues';
  if(e.source==='connections')return'connections';
  if(e.source==='sharing')return'sharing';
  if(e.source==='jobs')return'jobs';
  if(e.source==='posts'||e.actor)return'agents';
  return'projects';
}

/* Consecutive file touches from one session, each within ten minutes of the
   previous one, collapse to one row that expands to the paths. */
function collapse(list){
  const out=[];
  for(const e of list){
    const last=out[out.length-1];
    if(e.kind.startsWith('file.')&&last?.kind==='file.burst'&&last.actor?.session===e.actor?.session
      &&Date.parse(last.tail)-Date.parse(e.ts)<BURST_MS){last.paths.push(e.title);last.count++;last.tail=e.ts;continue;}
    out.push(e.kind.startsWith('file.')?{...e,kind:'file.burst',key:'burst:'+e.key,paths:[e.title],count:1,tail:e.ts}:e);
  }
  return out.map(e=>e.kind!=='file.burst'?e:e.count>1?{...e,title:'Edited '+e.count+' files in '+projectName(e.project)}:{...e,kind:'file.edited',paths:null});
}
const inSection=e=>section==='space'?!e.project:!!e.project&&(!project||e.project===project);
/* the events the window and the section keep: what the figures and the charts count */
const scoped=()=>{const from=cutoff();return rows.filter(e=>inSection(e)&&Date.parse(e.ts)>=from);};
function visible(list){
  const terms=query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return collapse(list.filter(e=>!STEP.test(e.kind)&&(!agent||e.actor?.runtime===agent)
    &&(!terms.length||terms.every(t=>[e.title,e.detail,KIND[e.kind]||e.kind,e.project,e.actor?.runtime,e.toolkit].map(v=>String(v??'')).join(' ').toLowerCase().includes(t)))))
    .filter(e=>!kinds.size||kinds.has(e.kind));
}

/* ── painting ─────────────────────────────────────────────────────────── */
function render(){
  if(!root)return;
  const scope=scoped(),list=visible(scope),shown=list.slice(0,limit);
  root.innerHTML='<div class="workwrap">'
    +'<header class="work-page-head"><h1>History</h1><p role="status">'+scope.length+' events '+esc(winText())+' · '+list.length+' in the timeline</p></header>'
    +'<div class="work-toolbar">'
      +toggleGroup({items:SECTIONS,value:section,ariaLabel:'Section',attrs:'data-group="section"'})
      +toggleGroup({items:WINS,value:win,ariaLabel:'Window',attrs:'data-group="win"'})
      +chipsHTML()
      +'<span class="work-spacer"></span>'
      +(section==='projects'?nativeSelect({options:[{value:'',label:'All projects'},...PROJECTS.map(p=>({value:p.id,label:p.name}))],value:project,size:'sm',ariaLabel:'Project',attrs:'data-act="project-filter"'}):'')
    +'</div>'
    +'<div class="work-grid3">'+(section==='space'?spaceHeroes(scope):projectHeroes(scope))+'</div>'
    +card({title:'Events over time',description:'Per day, '+esc(winText())+(section==='projects'&&project?', '+esc(projectName(project)):''),content:'<div id="fh-area"></div>'})
    +'<div class="work-grid2">'
      +card({title:'By source',description:'What the events were, '+esc(winText()),content:'<div id="fh-donut"></div>'})
      +card({title:section==='space'?'Busiest hours':'By agent',description:section==='space'?'When things arrive, '+esc(winText()):'Events per day by agent, '+esc(winText()),content:'<div id="fh-bars"></div>'})
    +'</div>'
    +card({title:'Daily activity',description:'Last 16 weeks, all time',content:'<div id="fh-heat"></div>'})
    +(section==='projects'?(composer?composerHTML():'')+sharingHTML():'')
    +streamHTML(shown)
    +'<div class="work-foot">'+(list.length>limit?button('Show more',{variant:'outline',size:'sm',attrs:'data-act="more"'}):'<span>'+(list.length?'Everything in this window is shown':'')+'</span>')
      +'<span>'+shown.length+' of '+list.length+' rows</span></div>'
  +'</div>';
  if(actions)actions.hidden=section!=='projects';
  drawCharts(scope);
}
function spaceHeroes(scope){
  const days=Math.max(1,new Set(scope.map(e=>dayOf(e.ts))).size);
  const arrived=scope.filter(e=>e.source==='connections').length,jobsRan=scope.filter(e=>e.source==='jobs').length;
  const failed=scope.filter(e=>e.kind==='job.failed').length,shared=scope.filter(e=>e.source==='sharing').length;
  return heroCard('Arrived',arrived,(arrived/days).toFixed(1),'per day',{tone:arrived?'primary':null,action:badge(esc(winLabel()),{variant:'outline'})})
    +heroCard('Jobs ran',jobsRan,failed,'failed')
    +heroCard('Sharing events',shared,scope.filter(e=>e.kind==='sharing.shared_with_you').length,'shared with you');
}
function projectHeroes(scope){
  const days=Math.max(1,new Set(scope.map(e=>dayOf(e.ts))).size);
  const files=scope.filter(e=>e.kind.startsWith('file.')).length,sessions=new Set(scope.filter(e=>e.actor).map(e=>e.actor.session)).size;
  const closed=scope.filter(e=>e.kind==='workitem.closed').length,started=scope.filter(e=>e.kind==='workitem.claimed').length;
  return heroCard('Events',scope.length,(scope.length/days).toFixed(1),'per day',{tone:scope.length?'primary':null,action:badge(esc(winLabel()),{variant:'outline'})})
    +heroCard('Files touched',files,sessions,sessions===1?'session':'sessions')
    +heroCard('Work closed',closed,started,'started');
}
/* the five charts, drawn from the scoped events after the paint */
function drawCharts(scope){
  const byDay=new Map();
  for(const e of scope)byDay.set(dayOf(e.ts),(byDay.get(dayOf(e.ts))||0)+1);
  const span=WDAYS[win]||Math.max(1,Math.ceil((Date.now()-Math.min(...rows.map(e=>Date.parse(e.ts))))/864e5)+1);
  const days=[];
  for(let i=span-1;i>=0;i--){const d=new Date();d.setHours(0,0,0,0);d.setDate(d.getDate()-i);const k=dayOf(d.toISOString());days.push({d:k,v:byDay.get(k)||0});}
  const one=v=>String(Math.round(v));
  areaChart(root.querySelector('#fh-area'),{data:days,x:'d',series:[{key:'v'}],config:{v:{label:'Events',color:'var(--chart-1)'}},format:one,xFormat:d=>d.slice(5),aspect:win==='today'?6:3.6});
  const counts=new Map();
  for(const e of scope){const g=groupOf(e);counts.set(g,(counts.get(g)||0)+1);}
  const config={};
  for(const [key,label,color] of GROUPS)config[key]={label,color};
  donutChart(root.querySelector('#fh-donut'),{data:GROUPS.map(([key])=>({key,value:counts.get(key)||0})),config,format:one,center:{value:String(scope.length),label:'events'},height:230});
  const bars=root.querySelector('#fh-bars');
  if(section==='space'){
    const hours=Array.from({length:24},(_,h)=>({label:pad(h)+':00',value:0}));
    for(const e of scope)hours[new Date(e.ts).getHours()].value++;
    const busy=hours.filter(h=>h.value).sort((a,b)=>b.value-a.value).slice(0,8).sort((a,b)=>a.label.localeCompare(b.label));
    barChartHorizontal(bars,{data:busy,config:{value:{label:'Events',color:'var(--chart-2)'}},format:one,rowHeight:26});
  }else{
    const aconf={};
    for(const a of AGENTS)aconf[a.id]={label:a.label,color:a.color||'var(--chart-1)'};
    const perDay=days.map(d=>({x:d.d,...Object.fromEntries(AGENTS.map(a=>[a.id,0]))}));
    const idx=new Map(perDay.map((d,i)=>[d.x,i]));
    for(const e of scope){if(!e.actor)continue;const i=idx.get(dayOf(e.ts));if(i!=null&&e.actor.runtime in aconf)perDay[i][e.actor.runtime]++;}
    barChartStacked(bars,{data:perDay,x:'x',series:AGENTS.map(a=>({key:a.id})),config:aconf,format:one,xFormat:k=>k.slice(5),aspect:2.6});
  }
  const all=new Map();
  for(const e of rows)if(inSection(e))all.set(dayOf(e.ts),(all.get(dayOf(e.ts))||0)+1);
  heatmapChart(root.querySelector('#fh-heat'),{byDay:all,config:{value:{label:'Events',color:'var(--chart-1)'}},format:v=>v+' events'});
}
/* the narrowing a click made, as removable chips */
function chipsHTML(){
  const chips=[...kinds].map(k=>badge(esc(KIND[k]||k)+' <span aria-hidden="true">×</span>',{variant:'secondary',cls:'work-chip',attrs:'role="button" tabindex="0" data-chip="kind" data-value="'+esc(k)+'" aria-label="Stop narrowing to '+esc(KIND[k]||k)+'"'}));
  if(agent)chips.push(badge(esc(agentLabel(agent))+' <span aria-hidden="true">×</span>',{variant:'outline',source:agent,cls:'work-chip',attrs:'role="button" tabindex="0" data-chip="agent" aria-label="Stop narrowing to '+esc(agentLabel(agent))+'"'}));
  return chips.join('');
}
/* The project's sharing, folded in from the former Sharing page: one row per
   shared project (or the selected one), its sync state, Apply when behind,
   and the members when one project is open. */
function sharingHTML(){
  const mine=shares.mine.filter(m=>!project||m.project===project);
  if(!mine.length){
    if(!project)return'';
    return card({cls:'work-list',title:'Sharing',description:'not shared',action:button('Share',{variant:'outline',size:'sm',attrs:'data-act="compose"'})});
  }
  const behind=mine.filter(m=>m.behind).length;
  return card({cls:'work-list',title:'Sharing',description:mine.length+(mine.length===1?' shared project':' shared projects')+(behind?' · '+behind+' behind':' · in sync'),
    action:project?'':button('Share a project',{variant:'outline',size:'sm',attrs:'data-act="compose"'}),
    content:mine.map(m=>item({size:'sm',cls:'work-row',attrs:'data-share="'+esc(m.project)+'"',
      title:'<i class="work-dot" data-tone="'+(m.behind?'attention':'on')+'" aria-hidden="true"></i><span class="work-title">'+esc(projectName(m.project))+'</span>'
        +badge(esc(m.repo+' · '+m.branch),{variant:'outline',mono:true})
        +(m.behind?badge(esc(m.behind+' behind'),{variant:'outline'}):badge('in sync',{variant:'secondary'})),
      description:esc(m.members.length+(m.members.length===1?' member':' members')+' · checked '+rel(m.checked)),
      actions:(m.behind?button('Apply',{variant:'outline',size:'sm',attrs:'data-act="apply"'}):'')
        +(project?'':button('Open',{variant:'ghost',size:'sm',attrs:'data-act="select" data-project="'+esc(m.project)+'"'})),
      footer:project?membersHTML(m):''})).join('')});
}
function membersHTML(m){
  return'<div class="work-acts">'+m.members.map(p=>badge(esc(p.id)+(p.owner?' · owner':''),{variant:p.owner?'secondary':'outline',mono:true})
      +(p.owner||p.id===shares.me?'':button('Revoke',{variant:'ghost',size:'sm',attrs:'data-act="revoke" data-member="'+esc(p.id)+'"'}))).join('')
    +button('Copy invite',{variant:'ghost',size:'sm',attrs:'data-act="invite"'})+'</div>';
}
function composerHTML(){
  return card({title:'Share a project',description:'the other Space clones it on its next check',
    content:'<div class="work-fields">'
      +field({label:'Project',htmlFor:'sh-project',control:nativeSelect({id:'sh-project',full:true,options:PROJECTS.map(p=>({value:p.id,label:p.name})),value:project||PROJECTS[0].id,attrs:'data-field="project"'})})
      +field({label:'Their workspace id',htmlFor:'sh-ws',control:input({id:'sh-ws',placeholder:'ws_…',mono:true,attrs:'data-field="ws"'})})
    +'</div>',
    footer:button('Share',{size:'sm',attrs:'data-act="share"'})+button('Cancel',{variant:'ghost',size:'sm',attrs:'data-act="cancel"'})});
}
function streamHTML(list){
  if(!list.length)return empty({size:'sm',icon:icons.activity,title:'Nothing in this window',
    description:query||project||kinds.size||agent?'No loaded events match this selection.':'Widen the window, or wait for agents, sources and people to act.'});
  let day='',html='';
  for(const e of list){
    const k=dayOf(e.ts);
    if(k!==day){day=k;html+=timelineSeparator(esc(dayLabel(e.ts)));}
    html+=rowHTML(e);
  }
  return card({cls:'work-list',title:'Timeline',description:'newest first; click a badge, a project or an agent to narrow',content:timeline(html)});
}
function rowHTML(e){
  const open=expanded.has(e.key),wid=recordId(e);
  return timelineItem({time:'<time datetime="'+esc(e.ts)+'" title="'+esc(dtfmt(e.ts))+'">'+esc(tfmt(e.ts))+'</time>',tone:e.tone||'info',
    cls:'work-tl'+(open?' is-open':''),attrs:'data-key="'+esc(e.key)+'"',
    title:badge(esc(KIND[e.kind]||e.kind),{variant:'outline',cls:'work-pick',attrs:'role="button" tabindex="0" data-pick="kind" data-value="'+esc(e.kind)+'" title="Only '+esc(KIND[e.kind]||e.kind)+'"'})
      +'<button type="button" class="work-toggle" data-act="toggle" aria-expanded="'+(open?'true':'false')+'"><span class="work-title">'+esc(e.title)+'</span></button>'
      +(wid&&!e.ref?.workitem?badge('tracked',{variant:'secondary'}):'')+(marks.pinned.has(e.key)?badge('pinned',{variant:'secondary'}):'')
      +'<span class="work-tl-meta">'
        +(e.project&&!project?badge(esc(projectName(e.project)),{variant:'secondary',cls:'work-pick',attrs:'role="button" tabindex="0" data-pick="project" data-value="'+esc(e.project)+'" title="Only '+esc(projectName(e.project))+'"'}):'')
        +(e.actor?badge(esc(agentLabel(e.actor.runtime)),{variant:'outline',source:e.actor.runtime,cls:'work-pick',attrs:'role="button" tabindex="0" data-pick="agent" data-value="'+esc(e.actor.runtime)+'" title="Only '+esc(agentLabel(e.actor.runtime))+'"'})
          :e.toolkit?'<span class="work-sub">'+esc(e.toolkit)+'</span>':'')
        +'<span class="work-sub work-time" title="'+esc(dtfmt(e.ts))+'">'+esc(rel(e.ts))+'</span>'
      +'</span>',
    description:open?'':esc(e.detail?e.detail.split('\n')[0].slice(0,140):''),
    content:open?footerHTML(e,wid):''});
}
function footerHTML(e,wid){
  const detail=e.paths?'<ul class="work-paths">'+e.paths.map(p=>'<li>'+esc(p)+'</li>').join('')+'</ul>'
    :'<div class="work-detail">'+esc(e.detail||(e.actor?'Session '+e.actor.session:'No details.'))+'</div>';
  const url=e.ref?.url||e.ref?.issue?.url||'';
  return'<div class="work-tl-open">'+detail+'<div class="work-acts">'
    +(url?button('Open',{tag:'a',variant:'ghost',size:'sm',attrs:'href="'+esc(url)+'" target="_blank" rel="noopener noreferrer"'}):'')
    +(e.project?button('Open project',{variant:'ghost',size:'sm',attrs:'data-act="project"'}):'')
    +(wid?button('Open work item',{variant:'outline',size:'sm',attrs:'data-act="workitem" data-wid="'+esc(wid)+'"'})
      :button('Track',{variant:'outline',size:'sm',attrs:'data-act="track"'}))
    +button(marks.pinned.has(e.key)?'Unpin':'Pin',{variant:'ghost',size:'sm',attrs:'data-act="pin"'})
  +'</div></div>';
}

/* ── actions (local until the API lands) ──────────────────────────────── */
const findRow=key=>visible(scoped()).find(e=>e.key===key)||rows.find(e=>e.key===key);
function toggle(key){
  if(!key)return;
  if(expanded.has(key))expanded.delete(key);else expanded.add(key);
  render();
  root.querySelector('[data-key="'+CSS.escape(key)+'"] .work-toggle')?.focus({preventScroll:true});
}
async function openProject(id){
  if(!id)return;
  if(await go('projects/data/list')===true&&location.hash==='#/projects/data/list')dispatchEvent(new CustomEvent('space:open-project',{detail:id}));
}
async function openWorkitem(id){
  if(await go('work')===true&&location.hash==='#/work')dispatchEvent(new CustomEvent('space:work-open-workitem',{detail:{id}}));
}
function track(key){
  const e=findRow(key);
  if(!e)return;
  const now=new Date().toISOString(),pid=e.project||PROJECTS[0].id,id='t'+Date.now().toString(36).slice(-4);
  workitems().unshift({id,title:e.title,project:pid,status:'open',origin:e.ref?.issue?'github':'space',issue:e.ref?.issue||null,
    assignee:null,in_progress:false,labels:['work'],body:e.detail||null,created:now,updated:now});
  rows.unshift({key:'timeline:workitem.created:'+id,ts:now,source:'timeline',kind:'workitem.created',title:e.title,detail:'',project:pid,actor:null,ref:{workitem:id},tone:'info'});
  marks.tracked.set(key,id);marks.dismissed.add(key);
  toast('Tracked as a work item in '+projectName(pid));
  render();
}
function share(){
  const ws=root.querySelector('[data-field="ws"]')?.value.trim();
  if(!ws){root.querySelector('#sh-ws')?.focus();toast('Paste the other workspace id');return;}
  const pid=root.querySelector('[data-field="project"]').value;
  const cur=shares.mine.find(m=>m.project===pid);
  if(cur)cur.members.push({id:ws,owner:false});
  else shares.mine.push({project:pid,repo:'sharmasuraj0123/'+pid,branch:'main',behind:0,checked:new Date().toISOString(),members:[{id:shares.me,owner:true},{id:ws,owner:false}]});
  composer=false;toast('Shared '+projectName(pid)+' with '+ws+' (sample)');render();
}
function pick(kind,value){
  if(kind==='kind'){if(kinds.has(value))kinds.delete(value);else kinds.add(value);}
  else if(kind==='project'){project=value;section='projects';refreshToolbar();}
  else if(kind==='agent')agent=agent===value?'':value;
  limit=PAGE;render();
}
function onClick(ev){
  const t=ev.target;
  const tg=t.closest('[data-slot="toggle-group-item"]');
  if(tg){
    const g=tg.closest('[data-group]')?.dataset.group;
    if(g==='section')section=tg.dataset.value;else if(g==='win')win=tg.dataset.value;
    limit=PAGE;render();return;
  }
  const chip=t.closest('[data-chip]');
  if(chip){if(chip.dataset.chip==='kind')kinds.delete(chip.dataset.value);else agent='';render();return;}
  const picked=t.closest('[data-pick]');
  if(picked){pick(picked.dataset.pick,picked.dataset.value);return;}
  const row=t.closest('[data-key]'),shareRow=t.closest('[data-share]'),b=t.closest('button[data-act],a[data-act]');
  if(b){
    const key=row?.dataset.key,m=shareRow?shares.mine.find(x=>x.project===shareRow.dataset.share):null;
    switch(b.dataset.act){
      case'toggle':toggle(key);return;
      case'more':limit+=PAGE;render();return;
      case'project':openProject(findRow(key)?.project);return;
      case'workitem':openWorkitem(b.dataset.wid);return;
      case'track':track(key);return;
      case'pin':if(marks.pinned.has(key))marks.pinned.delete(key);else marks.pinned.add(key);render();return;
      case'compose':composer=true;render();root.querySelector('#sh-ws')?.focus();return;
      case'cancel':composer=false;render();return;
      case'share':share();return;
      case'select':project=b.dataset.project;render();refreshToolbar();return;
      case'apply':if(m){m.behind=0;m.checked=new Date().toISOString();toast('Applied origin/'+m.branch+' on '+projectName(m.project)+' (sample)');render();}return;
      case'revoke':if(m){m.members=m.members.filter(p=>p.id!==b.dataset.member);toast('Access removed for '+b.dataset.member+' (sample)');render();}return;
      case'invite':toast('Invite copied: share your workspace id '+shares.me+' (sample)');return;
    }
    return;
  }
  if(row&&row.dataset.key&&!t.closest('a,button,select,.work-tl-open'))toggle(row.dataset.key);
}
function onKey(ev){
  if(ev.key!=='Enter'&&ev.key!==' ')return;
  const target=ev.target.closest('[data-pick],[data-chip]');
  if(!target)return;
  ev.preventDefault();target.click();
}
function handoff(id){
  section='projects';project=PROJECTS.some(p=>p.id===id)?id:'';
  query='';kinds.clear();agent='';limit=PAGE;render();refreshToolbar();
}

export default {
  ...WORK_PAGES.find(page=>page.id==='work-history'),
  toolbar:()=>({search:{placeholder:'Search this window…',getValue:()=>query,
    setValue(value){value=String(value??'');if(value===query)return;query=value;limit=PAGE;render();}}}),
  async mount(el,ctx){
    root=el;go=ctx.switchTo;refreshToolbar=ctx.refreshToolbar||(()=>{});
    rows=[...entries(),...olderEntries(),...history()].sort((a,b)=>Date.parse(b.ts)-Date.parse(a.ts));
    shares=sharing();
    if(!actions){
      actions=document.createElement('div');
      actions.innerHTML=button('+ Share a project',{size:'sm',attrs:'data-act="compose"'});
      actions.addEventListener('click',e=>{if(e.target.closest('[data-act="compose"]')){section='projects';composer=true;render();root.querySelector('#sh-ws')?.focus();}});
    }
    setSectionActions('work-history',actions);
    el.addEventListener('click',onClick);
    el.addEventListener('keydown',onKey);
    el.addEventListener('change',ev=>{const s=ev.target.closest('select[data-act="project-filter"]');if(s){project=s.value;limit=PAGE;render();}});
    /* Manage's "View activity" hands a project over after switching here. */
    addEventListener('space:activity-project',ev=>{
      const id=String(ev.detail?.project_id||'').trim();
      if(!id)return;
      if(location.hash==='#/work/history')handoff(id);else pendingProject=id;
    });
    /* charts redraw when the panel width actually changes */
    let lastW=0;
    if(typeof ResizeObserver==='function')new ResizeObserver(es=>{
      const w=es[0].contentRect.width;
      if(Math.abs(w-lastW)>4){lastW=w;if(el.classList.contains('is-active'))render();}
    }).observe(el);
    render();
  },
  show(){if(pendingProject){const id=pendingProject;pendingProject=null;handoff(id);}else render();},
  refresh(){shares=sharing();render();},
};
