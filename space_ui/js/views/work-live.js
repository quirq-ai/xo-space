/* Live: what is happening right now (docs/work-and-workitems.md, section
   6.2). A hero row (sessions, jobs, pollers), then the calendar on the left
   (a shadcn Calendar with the selected day's agenda under it) and the
   stream on the right: one line per thing the logs report as it happens,
   with the source as a tag (the vendor color for an agent), following the
   newest line unless the person scrolls up or pauses. The badge row over
   the stream names what runs now; clicking a badge or a line's tag narrows
   the stream to that source. Every visual element is a shadcn/ui component
   from js/core/shadcn.js; css/work.css lays the pieces out. The lines are
   sampled from views/work-sample.js until the page is wired to a
   server-sent stream over the same logs. */
import {esc,rel,toast} from '../core/ui.js';
import {WORK_PAGES} from '../core/navigation.js?v=20260919-work4';
import {icons,button,badge,card,calendar,toggleGroup,item} from '../core/shadcn.js?v=20260919-work4';
import {sparkline} from '../core/chart.js?v=20260915-typesync1';
import {PROJECTS,AGENTS,sessions,watcher,connections,calendarEvents,jobs,LOG_POOL} from './work-sample.js?v=20260919-work4';

const FILTERS=[{value:'all',label:'All'},{value:'agents',label:'Agents'},{value:'jobs',label:'Jobs'},{value:'pollers',label:'Pollers'},{value:'watcher',label:'Watcher'}];
const LOG_MAX=300;
const ACTIVE_MS=5*60*1000;
let root=null,go=()=>{};
let month='',selected='',today='';
let live=null,tick=null,conns=null,events=[],sched=null;
let lines=[],filter='all',only='',paused=false,following=true,timer=null,seq=0;

const projectName=id=>PROJECTS.find(p=>p.id===id)?.name||id;
const agentLabel=id=>AGENTS.find(a=>a.id===id)?.label||id;
const isAgent=id=>AGENTS.some(a=>a.id===id);
const pad=n=>String(n).padStart(2,'0');
const key=d=>{d=new Date(d);return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());};
const tfmt=iso=>new Date(iso).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
const clock=iso=>{const d=new Date(iso);return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());};
const every=s=>s%86400===0?'every '+(s/86400)+' d':s%3600===0?'every '+(s/3600)+' h':'every '+Math.round(s/60)+' min';
function until(iso){
  const s=(Date.parse(iso)-Date.now())/1000;
  if(!Number.isFinite(s))return'';
  if(s<0)return rel(iso);
  if(s<60)return'now';
  if(s<3600)return'in '+Math.floor(s/60)+' min';
  if(s<86400)return'in '+Math.floor(s/3600)+' h';
  return'in '+Math.floor(s/86400)+' d';
}
function dayLabel(k){
  const t=new Date(),n=new Date(t);n.setDate(t.getDate()+1);
  if(k===key(t))return'Today';
  if(k===key(n))return'Tomorrow';
  n.setDate(t.getDate()-1);
  if(k===key(n))return'Yesterday';
  return new Date(k+'T12:00:00').toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'});
}
const dot=tone=>'<i class="work-dot" data-tone="'+esc(tone)+'" aria-hidden="true"></i>';
const heroCard=(text,value,secondary,secondaryLabel,{tone=null}={})=>card({stat:'hero',tone,description:text,title:esc(String(value)),
  footer:'<span class="work-hero-secondary"><b>'+esc(String(secondary))+'</b><span>'+esc(secondaryLabel)+'</span></span>'});

/* ── the stream (sampled) ─────────────────────────────────────────────── */
function pick(){
  const total=LOG_POOL.reduce((n,l)=>n+l.weight,0);
  let r=Math.random()*total;
  for(const l of LOG_POOL){r-=l.weight;if(r<=0)return l;}
  return LOG_POOL[0];
}
const line=(l,ts)=>({id:++seq,ts,source:l.source,group:l.group,text:l.text,level:l.level||'info'});
function backfill(){
  lines=[];
  let t=Date.now()-40*3000;
  for(let i=0;i<40;i++){lines.push(line(pick(),new Date(t).toISOString()));t+=2000+Math.random()*2500;}
}
function append(){
  const l=line(pick(),new Date().toISOString());
  lines.push(l);
  if(lines.length>LOG_MAX)lines.splice(0,lines.length-LOG_MAX);
  const list=root?.querySelector('.work-log');
  if(!list)return;
  if(matches(l)){list.insertAdjacentHTML('beforeend',lineHTML(l,true));while(list.children.length>LOG_MAX)list.firstElementChild.remove();}
  if(following)list.scrollTop=list.scrollHeight;
  const count=root.querySelector('[data-log-count]');
  if(count)count.textContent=shown().length+' lines';
  paintPulse();
}
function schedule(){
  clearTimeout(timer);
  if(paused||!root)return;
  timer=setTimeout(()=>{append();schedule();},1800+Math.random()*2400);
}
const matches=l=>(filter==='all'||l.group===filter)&&(!only||l.source===only);
const shown=()=>lines.filter(matches);
const tag=source=>isAgent(source)?badge(esc(agentLabel(source)),{variant:'outline',source,cls:'work-log-tag',attrs:'data-only="'+esc(source)+'" title="Only '+esc(agentLabel(source))+'"'})
  :'<b class="work-log-tag" data-only="'+esc(source)+'" title="Only '+esc(source)+'">'+esc(source)+'</b>';
const lineHTML=(l,fresh=false)=>'<li data-level="'+esc(l.level)+'"'+(fresh?' data-fresh':'')+'>'+dot(l.level==='error'?'error':'info')
  +'<time datetime="'+esc(l.ts)+'">'+esc(clock(l.ts))+'</time>'+tag(l.source)+'<span>'+esc(l.text)+'</span></li>';
/* the pulse: lines per twenty seconds over the last five minutes, all sources */
function paintPulse(){
  const host=root?.querySelector('#work-pulse');
  if(!host)return;
  const now=Date.now(),bins=Array.from({length:15},(_,i)=>({x:i,v:0}));
  for(const l of lines){const age=now-Date.parse(l.ts);if(age<0||age>=300000)continue;bins[14-Math.floor(age/20000)].v++;}
  sparkline(host,{data:bins,config:{v:{label:'Lines',color:'var(--chart-1)'}},format:v=>v+' lines',xFormat:i=>((14-i)/3).toFixed(1)+' min ago',height:36});
}
function paintLog(){
  const list=root?.querySelector('.work-log');
  if(!list)return;
  paintPulse();
  list.innerHTML=shown().map(l=>lineHTML(l)).join('');
  const jump=root.querySelector('[data-act="jump"]');
  if(jump)jump.hidden=following;
  if(following)list.scrollTop=list.scrollHeight;
}

/* ── the calendar ─────────────────────────────────────────────────────── */
function agenda(){
  const out=events.map(e=>({...e,at:e.starts,label:'meeting',tone:'info'}));
  for(const j of sched.jobs)if(j.enabled&&j.next_run)out.push({key:'job:next:'+j.id,at:j.next_run,label:'job',title:j.name,detail:every(j.every_s)+(j.running?' · running now':''),tone:j.running?'pending':'info',job:j});
  return out.sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
}
function agendaRow(x,withDay=false){
  const when=withDay?dayLabel(key(x.at))+', '+tfmt(x.at):tfmt(x.at);
  const url=x.ref?.url||'';
  const act=url?button('Open',{tag:'a',variant:'link',size:'sm',attrs:'href="'+esc(url)+'" target="_blank" rel="noopener noreferrer"'})
    :x.job&&!x.job.running?button('Run now',{variant:'link',size:'sm',attrs:'data-act="run" data-job="'+esc(x.job.id)+'"'}):'';
  return item({size:'sm',cls:'work-row work-agenda-row',attrs:'data-agenda="'+esc(x.key)+'"',
    title:'<span class="work-cal-time">'+esc(when)+'</span>'+dot(x.tone)+'<span class="work-title">'+esc(x.title)+'</span>',
    description:esc((x.detail?x.detail+' · ':'')+x.label)+(act?' '+act:'')});
}
function calendarHTML(){
  const all=agenda(),marks=new Set(all.map(x=>key(x.at)));
  const day=all.filter(x=>key(x.at)===selected);
  const upcoming=all.filter(x=>key(x.at)>selected&&Date.parse(x.at)>Date.now()).slice(0,4);
  const [my,mm]=month.split('-').map(Number);
  return card({cls:'work-list',content:calendar({month:new Date(my,mm-1,1),selected,today,marks})
    +'<div class="work-cal-agenda">'
      +'<div class="work-day work-day-row"><span>'+esc(dayLabel(selected))+' · '+day.length+(day.length===1?' entry':' entries')+'</span>'
        +(selected===today?'':button('Today',{variant:'link',size:'sm',attrs:'data-act="today"'}))+'</div>'
      +(day.length?day.map(x=>agendaRow(x)).join(''):'<div class="work-note">Nothing on this day.</div>')
      +(upcoming.length?'<div class="work-day">Upcoming</div>'+upcoming.map(x=>agendaRow(x,true)).join(''):'')
    +'</div>'});
}

/* ── what runs now: one badge each, each a narrowing of the stream ─────── */
function nowHTML(){
  const out=[];
  for(const s of live)out.push(badge(dot(Date.now()-Date.parse(s.last)<ACTIVE_MS?'on':'pending')+esc(agentLabel(s.agent)+' · '+projectName(s.project)),
    {variant:'outline',cls:'work-now-chip'+(only===s.agent?' is-on':''),attrs:'role="button" tabindex="0" data-only="'+esc(s.agent)+'" title="'+esc(s.working+' · active '+rel(s.last))+'"'}));
  for(const j of sched.jobs.filter(j=>j.running))out.push(badge(dot('pending')+esc(j.name),
    {variant:'outline',cls:'work-now-chip'+(only==='scheduler'?' is-on':''),attrs:'role="button" tabindex="0" data-only="scheduler" title="running since '+esc(rel(j.running_since))+'"'}));
  for(const c of conns.filter(c=>c.enabled))out.push(badge(dot(c.error?'error':'on')+esc(c.name),
    {variant:'outline',cls:'work-now-chip'+(only===c.toolkit?' is-on':''),attrs:'role="button" tabindex="0" data-only="'+esc(c.toolkit)+'" title="'+esc(c.error||'polled '+rel(c.last_poll)+' · next '+until(c.next_poll))+'"'}));
  return'<div class="work-now"><div class="work-now-chips">'+out.join('')+'</div>'
    +'<div class="work-pulse" title="Lines per twenty seconds, last five minutes"><span>pulse</span><div id="work-pulse"></div></div></div>';
}

/* ── painting ─────────────────────────────────────────────────────────── */
function render(){
  if(!root)return;
  const running=sched.jobs.filter(j=>j.running),failing=conns.filter(c=>c.enabled&&c.error).length,polling=conns.filter(c=>c.enabled).length;
  const next=sched.jobs.filter(j=>j.enabled&&j.next_run&&!j.running).sort((a,b)=>Date.parse(a.next_run)-Date.parse(b.next_run))[0];
  root.innerHTML='<div class="workwrap">'
    +'<header class="work-page-head"><h1>Live</h1><p role="status">watcher ticked '+esc(rel(tick.last_tick))+' · '+tick.sessions_indexed+' sessions indexed</p></header>'
    +'<div class="work-grid3">'
      +heroCard('Sessions open',live.length,new Set(live.map(s=>s.agent)).size,new Set(live.map(s=>s.agent)).size===1?'agent active':'agents active',{tone:live.length?'primary':null})
      +heroCard('Jobs running',running.length,next?until(next.next_run):'none','next: '+(next?next.name:'nothing scheduled'))
      +heroCard('Pollers',polling,failing,failing?'failing':'all healthy')
    +'</div>'
    +'<div class="work-live">'
      +calendarHTML()
      +card({cls:'work-list work-logcard',title:'Stream',description:'<span data-log-count>'+shown().length+' lines</span>'+(only?' · only '+esc(agentLabel(only)):''),
        action:toggleGroup({items:FILTERS,value:filter,ariaLabel:'Stream source',attrs:'data-group="filter"'})
          +button(paused?'Resume':'Pause',{variant:paused?'default':'outline',size:'sm',attrs:'data-act="pause"'}),
        content:nowHTML()+'<ol class="work-log" aria-live="off"></ol>'
          +'<div class="work-log-foot">'+button(icons.arrowDown+'Jump to latest',{variant:'ghost',size:'sm',attrs:'data-act="jump" hidden'})
            +(only?button('Clear filter',{variant:'ghost',size:'sm',attrs:'data-act="clear-only"'}):'')+'</div>'})
    +'</div>'
  +'</div>';
  const list=root.querySelector('.work-log');
  list.addEventListener('scroll',()=>{following=list.scrollTop+list.clientHeight>=list.scrollHeight-8;const jump=root.querySelector('[data-act="jump"]');if(jump)jump.hidden=following;});
  paintLog();
}
function onClick(ev){
  const t=ev.target;
  const day=t.closest('[data-slot="calendar-day"]');
  if(day){selected=day.dataset.day;month=selected.slice(0,7);render();
    root.querySelector('[data-slot="calendar-day-button"][data-day="'+CSS.escape(selected)+'"]')?.focus({preventScroll:true});return;}
  const nav=t.closest('[data-calendar-step]');
  if(nav){const [y,m]=month.split('-').map(Number);const d=new Date(y,m-1+Number(nav.dataset.calendarStep),1);month=d.getFullYear()+'-'+pad(d.getMonth()+1);render();return;}
  const tg=t.closest('[data-slot="toggle-group-item"]');
  if(tg){filter=tg.dataset.value;only='';following=true;render();return;}
  const src=t.closest('[data-only]');
  if(src){only=only===src.dataset.only?'':src.dataset.only;filter='all';following=true;render();return;}
  const b=t.closest('button[data-act]');
  if(!b)return;
  switch(b.dataset.act){
    case'pause':paused=!paused;render();schedule();break;
    case'jump':following=true;paintLog();break;
    case'clear-only':only='';following=true;render();break;
    case'today':selected=today;month=today.slice(0,7);render();break;
    case'run':toast('Run requested for '+b.dataset.job+' (sample)');break;
  }
}
function onKey(ev){
  if(ev.key!=='Enter'&&ev.key!==' ')return;
  const target=ev.target.closest('[data-only][role="button"]');
  if(!target)return;
  ev.preventDefault();target.click();
}
function load(){live=sessions();tick=watcher();conns=connections();events=calendarEvents();sched=jobs();}

export default {
  ...WORK_PAGES.find(page=>page.id==='work-live'),
  async mount(el,ctx){
    root=el;go=ctx.switchTo;
    today=key(new Date());selected=today;month=today.slice(0,7);
    load();backfill();
    el.addEventListener('click',onClick);
    el.addEventListener('keydown',onKey);
    render();
  },
  show(){render();schedule();},
  hide(){clearTimeout(timer);timer=null;},
  refresh(){load();render();},
};
