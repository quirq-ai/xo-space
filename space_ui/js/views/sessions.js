/* Agents tab — multi-runtime telemetry dashboard (data: GET
   /xo/sessions.json, pre-aggregated by the API). Independent of the atlas:
   own lazy fetch on
   first activation, own error handling — a graph-data failure cannot take
   this tab down, and vice versa. (The old fallbackView/window.__switchView
   dance is gone: the registry keeps tabs switchable no matter which views
   are broken.) Window filtering happens here, client-side, over per-day
   rollups.

   Every visual is a shadcn/ui component ported to Space (js/core/shadcn.js
   for markup, js/core/chart.js for the SVG charts, css/shadcn.css for the
   styles); css/sessions.css only lays the pieces out. */
import {API_BASE,apiFetch} from '../core/api.js';
import {esc,toast} from '../core/ui.js';
import {AGENT_PAGES} from '../core/navigation.js?v=20260915-agents2';
import {icons,button,badge,card,table,sortHead,checkbox,label,toggleGroup,pagination,skeleton,alert,breadcrumb,empty,item,itemGroup,itemSeparator,spinner,switchControl,input} from '../core/shadcn.js?v=20260915-agents2';
import {areaChart,barChartHorizontal,barChartStacked,donutChart,radialChart,heatmapChart} from '../core/chart.js?v=20260915-typesync1';

let _open=null;
let _toolbar=()=>null;
let _refresh=async()=>{};
let agentMount=null;
let activeToolbarRefresh=()=>{};
const agentToolbarRefreshers=new Map();

export function createAgentViews(){
  return AGENT_PAGES.map(page=>({
    ...page,
    toolbar:()=>_toolbar(),
    /* The shell's section Refresh button is the only refresh control:
       every Agents page shares the one telemetry payload. */
    refresh:()=>_refresh(),
    mount(el,ctx){
      agentToolbarRefreshers.set(page.id,ctx.refreshToolbar||(()=>{}));
      activeToolbarRefresh=agentToolbarRefreshers.get(page.id);
      if(!agentMount)agentMount=agentController.mount(el,{...ctx,refreshToolbar:()=>activeToolbarRefresh()});
      return agentMount;
    },
    show(){
      activeToolbarRefresh=agentToolbarRefreshers.get(page.id)||(()=>{});
      _open?.(page.route.split('/')[1]);
    },
  }));
}

const agentController={
  id:'agents',label:'Agents',order:4,
  toolbar(){return _toolbar();},
  async mount(el,ctx){
const wrap=document.getElementById('sesswrap');
const WINS=[['today','Today'],['7d','7 days'],['30d','30 days'],['all','All']];
const WDAYS={today:1,'7d':7,'30d':30,all:null};
const SUBS=[['overview','Overview'],['sessions','Sessions'],['trends','Trends'],['configure','Configure']];
let SD=null,loading=false,failed=null,win='7d',sub='overview',sel=null,sortK='started_at',sortD=-1,enabledAgents=null,page=0,query='';
const PAGE_SIZE=10;              /* sessions list page length */
/* Configure page: telemetry source descriptors from /api/telemetry/sources,
   merged at render time with the collection status and usage in SD. */
let SRC=null,srcLoading=false,srcFailed=null;
const srcBusy=new Set();         /* source ids with a save in flight */
const srcDraft=new Map();        /* source id -> unsaved path text */
const srcNote=new Map();         /* source id -> inline note after a save */
const srcEditing=new Set();      /* source ids whose path row is in edit mode */
let telemetryRebuild=null;       /* timer: quiet reload after a config change */
const promptsCache=new Map();    /* session key -> session_prompts payload */
/* Search belongs to the loaded table, not the charts or a selected transcript. */
_toolbar=()=>sub==='sessions'&&!sel&&SD&&!loading&&!failed?{search:{
  placeholder:'Search loaded sessions…',
  getValue:()=>query,
  setValue(value){
    value=String(value??'');
    if(value===query)return;
    query=value;page=0;render();
  }
}}:null;

const tok=n=>n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(Math.round(n));
const usd=n=>{n=Number(n)||0;return'~$'+(n>=1000?Math.round(n).toLocaleString():n>=100?n.toFixed(0):n.toFixed(2));};
const costfmt=(n,known=true)=>known===false?((Number(n)||0)>0?usd(n)+'*':'—'):usd(n);
const dur=s=>{if(!s)return'—';const m=Math.floor(s/60),h=Math.floor(m/60);return h?h+'h '+String(m%60).padStart(2,'0')+'m':m+'m';};
const dtfmt=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';
const mshort=m=>(m||'').replace(/^claude-/,'')||'unknown';
const num=v=>{const n=Number(v);return Number.isFinite(n)?n:0;};
const stok=s=>{
  const total=Number(s?.total_tokens);
  if(s?.total_tokens!==undefined&&s?.total_tokens!==null&&Number.isFinite(total))return total;
  const explicit=Number(s?.tokens);
  if(s?.tokens!==undefined&&s?.tokens!==null&&Number.isFinite(explicit))return explicit;
  return num(s?.fresh)+num(s?.output)+num(s?.cache_read)+num(s?.cache_write);
};
const cutoff=()=>{const d=WDAYS[win];return d==null?'':new Date(Date.now()-d*864e5).toISOString().slice(0,10);};
const winLabel=()=>WINS.find(([k])=>k===win)?.[1]||'';
const winText=()=>win==='all'?'all time':win==='today'?'today':'last '+winLabel().toLowerCase();
const agentOf=row=>row?.agent||row?.source||'claude_code';
const sessionKey=row=>row?.key||(agentOf(row)+':'+row?.id);
const dailySessionKey=row=>row?.session_key||(agentOf(row)+':'+row?.session_id);
const agentOn=row=>enabledAgents?enabledAgents.has(agentOf(row)):true;
const sourceDefs=()=>{
  const rows=SD?.meta?.sources;
  return Array.isArray(rows)&&rows.length?rows:[{id:'claude_code',label:'Claude Code',available:true,cost_status:'estimated'}];
};
const agentLabel=id=>sourceDefs().find(source=>source.id===id)?.label||String(id||'Unknown').replaceAll('_',' ');
const costNote='* Partial estimate includes only sources that report cost; — means cost is unavailable.';

/* ---- shadcn building blocks specific to this page ---- */
const sourceBadge=id=>badge(esc(agentLabel(id)),{variant:'outline',source:id});
const modelBadge=m=>badge(esc(mshort(m)),{variant:'secondary'});
const statCard=(label,value,foot,{tone=null,action='',attrs=''}={})=>card({stat:true,tone,description:label,title:value,action,footer:foot,attrs});
/* Overview hero: one bold primary figure, one smaller secondary figure. */
const heroCard=(label,value,secondary,secondaryLabel,{tone=null,action='',attrs=''}={})=>card({stat:'hero',tone,description:label,title:value,action,attrs,
  footer:'<span class="sess-hero-secondary"><b>'+secondary+'</b><span>'+secondaryLabel+'</span></span>'});
const durLong=s=>{if(!s)return'0m';const m=Math.floor(s/60),h=Math.floor(m/60),d=Math.floor(h/24);return d>=2?d+'d '+(h%24)+'h':h?h+'h '+String(m%60).padStart(2,'0')+'m':m+'m';};
const chartConfig=(key,label,color='var(--chart-1)')=>({[key]:{label,color}});
const CHART_COLORS=['var(--chart-1)','var(--chart-2)','var(--chart-3)','var(--chart-4)','var(--chart-5)','var(--chart-6)','var(--chart-7)','var(--chart-8)'];
const note=msg=>'<div class="sess-note">'+msg+'</div>';

async function load({quiet=false}={}){
  /* quiet: re-read the payload behind the current page (after a config
     change) without flashing the skeleton */
  if(!quiet){loading=true;render();}
  failed=null;
  /* apiFetch forwards the page's query string (e.g. Coder's
     ?coder_session_token=…) so this lazy fetch authenticates on its own, and
     it classifies the failure: offline = the request never reached the server
     (down/restarting/proxy) — a different failure than any HTTP error, and it
     must not be blamed on a native telemetry store. For HTTP errors the API's
     own explanation is surfaced
     instead of a bare status code. */
  const res=await apiFetch(API_BASE+'/xo/sessions.json');
  loading=false;
  if(res.ok){
    SD=res.data;
    if(enabledAgents===null)enabledAgents=new Set(sourceDefs().map(source=>source.id));
  }
  else failed=res.offline?'\x00offline':res.error;
  render();
}
_refresh=async()=>{SD=null;SRC=null;await load();};
async function loadSources(){
  srcLoading=true;srcFailed=null;render();
  const res=await apiFetch(API_BASE+'/api/telemetry/sources');
  srcLoading=false;
  if(res.ok)SRC=res.data.items||[];
  else srcFailed=res.offline?'\x00offline':res.status===404?'\x00missing':(res.error||'error');
  render();
}
/* A saved path or switch changes what the next telemetry build reads; the
   server rebuilds sessions.json off the request path, so re-read it quietly
   once that has had time to finish. */
function scheduleTelemetryReload(attempt=0,changedAt=Date.now()){
  clearTimeout(telemetryRebuild);
  telemetryRebuild=setTimeout(async()=>{
    if(!SD)return;
    await load({quiet:true});
    /* the rebuild may still be running: keep re-reading until the payload
       is newer than the change, a few times at most */
    const built=Date.parse(SD?.meta?.generated_at||'')||0;
    if(built<changedAt&&attempt<3)return scheduleTelemetryReload(attempt+1,changedAt);
    for(const[id,note]of srcNote)if(note.tone==='ok')srcNote.delete(id);
    render();
  },attempt?7000:9000);
}
async function saveSource(id,patch){
  if(srcBusy.has(id))return;
  srcBusy.add(id);render();
  const res=await apiFetch(API_BASE+'/api/telemetry/sources/'+encodeURIComponent(id),{method:'PUT',body:patch});
  srcBusy.delete(id);
  if(!res.ok){
    srcNote.set(id,{tone:'error',text:res.error||'Could not save this source.'});
    render();return;
  }
  SRC=(SRC||[]).map(row=>row.id===id?res.data.item:row);
  srcDraft.delete(id);srcEditing.delete(id);
  srcNote.set(id,{tone:'ok',text:'Saved. Status updates in a moment.'});
  toast(patch.enabled===false?'Collection turned off':patch.enabled===true?'Collection turned on':'Data location saved');
  scheduleTelemetryReload();
  render();
}

/* ---- CSV export: the full dataset behind a table, never just its page ---- */
const csvCell=v=>{const str=String(v??'');return/[",\r\n]/.test(str)?'"'+str.replaceAll('"','""')+'"':str;};
function downloadCsv(filename,header,rows){
  const text=[header,...rows].map(r=>r.map(csvCell).join(',')).join('\r\n');
  const url=URL.createObjectURL(new Blob(['\ufeff'+text],{type:'text/csv;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download=filename;
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast('Downloaded '+filename);
}
const csvButton=key=>button(icons.download+'Export CSV',{variant:'outline',size:'sm',attrs:'data-csv="'+key+'" title="Download every row of this table as CSV"'});
/* ---- render dispatcher ---- */
function render(){
  ctx.refreshToolbar();
  const title=SUBS.find(([key])=>key===sub)?.[1]||'Overview';
  const pageHead='<header class="sess-page-head"><h1>'+title+'</h1></header>';
  if(loading){
    wrap.innerHTML=pageHead+'<div role="status" aria-label="Loading agent activity" class="sess-skeleton">'
      +'<div class="sess-grid3">'+[0,1,2].map(()=>card({content:skeleton('height:14px;width:40%')+skeleton('height:36px;width:55%;margin-top:12px')+skeleton('height:14px;width:70%;margin-top:16px')})).join('')+'</div>'
      +card({content:skeleton('height:20px;width:30%')+skeleton('height:220px;margin-top:14px')})+'</div>';
    return;
  }
  if(sub==='configure'){rConfigure(pageHead);return;}
  if(failed){
    const off=failed==='\x00offline';
    wrap.innerHTML=pageHead+alert({variant:'destructive',icon:off?icons.wifiOff:icons.alert,
      title:off?'xo-space is unreachable':'Could not load .xo/sessions.json',
      description:(off
        ?'<span>The request never reached the server (stopped or restarting). Not a telemetry-source problem.</span>'
        :'<span>'+esc(failed)+'. The API reads local telemetry for each runtime (Claude Code: <b>ARGUS_DB</b>; Codex: <b>CODEX_HOME</b>; Cursor: <b>CURSOR_HOME</b>).</span>')
        +button(icons.refresh+'Retry',{variant:'outline',size:'sm',attrs:'id="sess-retry"'})});
    document.getElementById('sess-retry').addEventListener('click',load);return;
  }
  if(!SD){wrap.innerHTML=pageHead+empty({icon:icons.bot,title:'No telemetry loaded',description:'Open this tab to load session telemetry.'});return;}
  const showWin=(sub==='overview'||sub==='trends');
  const sources=sourceDefs();
  wrap.innerHTML=pageHead+'<div class="sess-toolbar">'
    +'<div class="sess-sources" data-slot="checkbox-group" role="group" aria-label="Sources"><span data-slot="field-legend">Sources</span>'
    +sources.map(source=>{
      const id='sess-src-'+source.id;
      return'<div'+(source.available===false?' class="is-unavailable"':'')+' title="'+esc(source.available===false?(source.message||source.label+' is unavailable'):source.label+' sessions')+'">'
        +checkbox({id,checked:enabledAgents.has(source.id),attrs:'data-agent="'+esc(source.id)+'" aria-controls="sess-body"'})
        +label(esc(source.label)+(source.available===false?badge('offline',{variant:'destructive'}):''),{htmlFor:id})+'</div>';
    }).join('')+'</div>'
    +'<div class="sess-spacer"></div>'
    +(showWin?toggleGroup({items:WINS.map(([value,text])=>({value,label:text})),value:win,ariaLabel:'Time window',attrs:'class="sess-win"'}):'')
    +'</div><div id="sess-body"></div>';
  wrap.querySelectorAll('.sess-win [data-value]').forEach(b=>b.addEventListener('click',()=>{win=b.dataset.value;render();}));
  wrap.querySelectorAll('[data-agent]').forEach(box=>box.addEventListener('click',()=>{
    const changedAgent=box.dataset.agent;
    if(box.dataset.state!=='checked')enabledAgents.add(changedAgent);else enabledAgents.delete(changedAgent);
    page=0;
    const selected=SD.sessions.find(row=>sessionKey(row)===sel);
    if(selected&&!agentOn(selected))sel=null;
    render();
    [...wrap.querySelectorAll('[data-agent]')].find(next=>next.dataset.agent===changedAgent)?.focus();
  }));
  const el=document.getElementById('sess-body');
  if(!enabledAgents.size){
    el.innerHTML=empty({icon:icons.filter,title:'No session sources selected',description:'Turn on Claude Code, Codex, Cursor, or any combination to calculate this view.'});
    return;
  }
  if(!sources.some(source=>enabledAgents.has(source.id)&&source.available!==false)){
    el.innerHTML=empty({icon:icons.wifiOff,title:'Selected sources are unavailable',description:'Refresh after the local telemetry store becomes readable, or choose another source.'});
    return;
  }
  if(sub==='overview')rOverview(el);
  else if(sub==='sessions'){if(sel)rDetail(el);else rList(el);}
  else rTrends(el);
}

/* Cost over a set of daily model rows: unavailable, partial or estimated. */
function costSummary(dm){
  const knownCostRows=dm.filter(r=>r.cost_known!==false);
  const knownCost=knownCostRows.reduce((a,r)=>a+num(r.cost),0);
  return!dm.length
    ?{value:'—',label:'no data',title:'No telemetry rows in this window'}
    :!knownCostRows.length
      ?{value:'—',label:'unavailable',title:'Selected telemetry rows do not report cost'}
      :knownCostRows.length<dm.length
        ?{value:usd(knownCost),label:'partial estimate*',title:'Includes only selected telemetry rows that report cost'}
        :{value:usd(knownCost),label:'estimated',title:'Estimated from runtime pricing'};
}

/* ---- Overview ---- */
function rOverview(el){
  const co=cutoff();
  const dm=SD.daily_models.filter(r=>agentOn(r)&&r.day>=co);
  const ds=SD.daily_sessions.filter(r=>agentOn(r)&&r.day>=co);
  const tokens=dm.reduce((a,r)=>a+num(r.tokens),0);
  const costState=costSummary(dm);
  const nsess=new Set(ds.map(dailySessionKey)).size;
  const byDay=new Map();dm.forEach(r=>byDay.set(r.day,(byDay.get(r.day)||0)+r.tokens));
  const days=[...byDay.keys()].sort().map(d=>({d,v:byDay.get(d)}));
  const byModel=new Map();dm.forEach(r=>byModel.set(r.model,(byModel.get(r.model)||0)+r.tokens));
  const models=[...byModel.entries()].sort((a,b)=>b[1]-a[1]).slice(0,8);
  const per=new Map();
  ds.forEach(r=>{const key=dailySessionKey(r),p=per.get(key)||{t:0,c:0,known:true};p.t+=r.tokens;p.c+=r.cost;p.known=p.known&&r.cost_known!==false;per.set(key,p);});
  const meta=new Map(SD.sessions.filter(agentOn).map(s=>[sessionKey(s),s]));
  const top=[...per.entries()].map(([key,p])=>({key,t:p.t,c:p.c,known:p.known,m:meta.get(key)}))
    .filter(r=>r.m).sort((a,b)=>b.t-a.t).slice(0,10);
  /* harnesses with activity in the window, and total session time (from
     the loaded session list, which is windowed by start time) */
  const harnesses=[...new Set(ds.map(agentOf))].map(agentLabel).sort();
  const duration=SD.sessions.filter(s=>agentOn(s)&&(s.started_at||'').slice(0,10)>=co).reduce((a,s)=>a+num(s.duration_sec),0);
  el.innerHTML='<div class="sess-grid3">'
    +heroCard('Total tokens',tok(tokens),nsess?tok(tokens/nsess):'0','per session',{tone:'primary',action:badge(esc(winLabel()),{variant:'outline'})})
    +heroCard('Sessions',nsess.toLocaleString(),String(harnesses.length),(harnesses.length===1?'harness':'harnesses')+(harnesses.length?' · '+esc(harnesses.join(', ')):''))
    +heroCard('Time in sessions',durLong(duration),costState.value,esc(costState.label)+' cost',{attrs:'title="'+esc(costState.title)+'"'})
    +'</div>'
    +card({title:'Tokens over time',description:'Daily token volume, '+winText(),content:'<div id="ch-area"></div>'})
    +'<div class="sess-grid2">'
    +card({title:'Daily activity',description:'Last 16 weeks, all time',content:'<div id="ch-heat"></div>'})
    +card({title:'Top models',description:'By tokens, '+winText(),content:'<div id="ch-models"></div>'})
    +'</div>'
    +card({title:'Top sessions',description:'Heaviest sessions '+winText(),content:topTable(top)});
  areaChart(document.getElementById('ch-area'),{data:days,x:'d',series:[{key:'v'}],config:chartConfig('v','Tokens'),format:tok,xFormat:d=>d.slice(5)});
  const allByDay=new Map();SD.daily_models.filter(agentOn).forEach(r=>allByDay.set(r.day,(allByDay.get(r.day)||0)+r.tokens));
  heatmapChart(document.getElementById('ch-heat'),{byDay:allByDay,config:chartConfig('value','Tokens'),format:v=>tok(v)+' tokens'});
  barChartHorizontal(document.getElementById('ch-models'),{data:models.map(([m,v])=>({label:mshort(m),value:v})),config:chartConfig('value','Tokens'),format:tok});
  el.querySelectorAll('[data-sid]').forEach(tr=>tr.addEventListener('click',()=>{sel=tr.dataset.sid;sub='sessions';render();}));
}
function topTable(rows){
  if(!rows.length)return empty({size:'sm',icon:icons.inbox,title:'No sessions in this window',description:'Widen the time window or enable more sources.'});
  return table({
    head:[{html:'Started'},{html:'Project'},{html:'Source'},{html:'Model'},{html:'Tokens (win)',cls:'num'},{html:'Cost (win)',cls:'num'}],
    rows:rows.map(r=>({cls:'rowlink',attrs:'data-sid="'+esc(r.key)+'"',cells:[
      {html:dtfmt(r.m.started_at)},{html:'<b>'+esc(r.m.project)+'</b>'},{html:sourceBadge(agentOf(r.m))},{html:modelBadge(r.m.model)},
      {html:tok(r.t),cls:'num acc'},{html:costfmt(r.c,r.known),cls:'num'}]})),
    caption:rows.some(r=>!r.known)?costNote:'',
  });
}

/* ---- Sessions list + detail ---- */
const COLS=[['started_at','Started'],['project','Project'],['agent','Source'],['model','Model'],['tokens','Tokens'],['cost','Cost'],['turns','Turns'],['duration_sec','Duration']];
const NUM_COLS=new Set(['tokens','cost','turns','duration_sec']);
function rList(el){
  const loaded=SD.sessions.filter(agentOn);
  const terms=query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rows=terms.length?loaded.filter(s=>{
    const text=[s.project,s.project_path,agentOf(s),agentLabel(agentOf(s)),s.model,s.id,sessionKey(s)]
      .map(value=>String(value??'')).join(' ').toLowerCase();
    return terms.every(term=>text.includes(term));
  }):loaded;
  const kf=s=>sortK==='tokens'?stok(s):sortK==='project'?s.project:sortK==='agent'?agentLabel(agentOf(s)):(s[sortK]??0);
  rows.sort((a,b)=>{const x=kf(a),y=kf(b);return(x<y?-1:x>y?1:0)*sortD;});
  const byAgent=SD.totals.sessions_by_agent;
  const selectedTotal=byAgent&&typeof byAgent==='object'
    ?Object.entries(byAgent).filter(([agent])=>enabledAgents.has(agent)).reduce((sum,[,count])=>sum+(Number(count)||0),0)
    :(enabledAgents.has('claude_code')?SD.totals.sessions:0);
  /* client-side pagination: sort spans the full loaded list, the table shows
     one page. Page index is clamped so a filter change can never strand the
     view past the last page. */
  const pages=Math.max(1,Math.ceil(rows.length/PAGE_SIZE));
  page=Math.max(0,Math.min(page,pages-1));
  const start=page*PAGE_SIZE;
  const pageRows=rows.slice(start,start+PAGE_SIZE);
  const loadedCap=selectedTotal>loaded.length?'newest '+loaded.length+' loaded of '+selectedTotal+' selected sessions':loaded.length+' sessions (all time)';
  const cap=(terms.length?rows.length+' matching of '+loaded.length+' loaded sessions'
      +(selectedTotal>loaded.length?' · '+loadedCap:' (all time)'):loadedCap)
    +(rows.length>PAGE_SIZE?' · showing '+(start+1)+'–'+(start+pageRows.length):'');
  el.innerHTML=card({
    title:'Sessions',description:'<span role="status">'+esc(cap)+'</span>',
    action:terms.length?badge(icons.search+esc(query),{variant:'secondary'}):'',
    content:table({
      head:COLS.map(([k,l])=>sortHead(l,{key:k,active:k===sortK,dir:sortD,cls:NUM_COLS.has(k)?'num':''})).concat([{html:'Sub-agents',cls:'num'}]),
      rows:pageRows.map(s=>({cls:'rowlink',attrs:'data-sid="'+esc(sessionKey(s))+'"',cells:[
        {html:dtfmt(s.started_at)},{html:'<b>'+esc(s.project)+'</b>'},{html:sourceBadge(agentOf(s))},{html:modelBadge(s.model)},
        {html:tok(stok(s)),cls:'num acc'},{html:costfmt(s.cost,s.cost_known),cls:'num'},
        {html:String(s.turns),cls:'num'},{html:dur(s.duration_sec),cls:'num'},{html:s.subagents.length?String(s.subagents.length):'',cls:'num'}]})),
      empty:terms.length?empty({size:'sm',icon:icons.search,title:'No matching sessions',description:'No loaded sessions match this search for the selected sources.'}):'',
    }),
    footer:pages>1?'<div class="sess-pager">'+pagination({page,pages})+'<span>Page '+(page+1)+' of '+pages+'</span></div>':'',
  });
  el.querySelectorAll('[data-k]').forEach(th=>th.addEventListener('click',()=>{
    const k=th.dataset.k;if(sortK===k)sortD=-sortD;else{sortK=k;sortD=-1;}page=0;render();}));
  el.querySelectorAll('[data-sid]').forEach(tr=>tr.addEventListener('click',()=>{sel=tr.dataset.sid;render();}));
  el.querySelectorAll('[data-page]').forEach(a=>a.addEventListener('click',e=>{
    e.preventDefault();
    const next=Number(a.dataset.page);
    if(a.getAttribute('aria-disabled')==='true'||next===page||next<0||next>=pages)return;
    page=next;render();
  }));
}
const kvRows=pairs=>table({rows:pairs.map(([k,v])=>({cells:[{html:k,cls:'kv-key'},{html:esc(v??'—'),cls:'wrap'}]}))});
function rDetail(el){
  const s=SD.sessions.find(x=>sessionKey(x)===sel&&agentOn(x));
  if(!s){sel=null;ctx.refreshToolbar();rList(el);return;}
  const t=stok(s);
  const explicitOwn=Number(s.own_tokens);
  const childTokens=s.subagents.reduce((sum,row)=>sum+num(row.total_tokens??row.tokens),0);
  const own=s.own_tokens!==undefined&&s.own_tokens!==null&&Number.isFinite(explicitOwn)
    ?Math.max(0,explicitOwn):Math.max(0,t-childTokens);
  const fresh=num(s.fresh),output=num(s.output),cacheWrite=num(s.cache_write),cacheRead=num(s.cache_read);
  const classified=fresh+output+cacheWrite+cacheRead;
  const explicitUnclassified=Number(s.unclassified);
  const unclassified=s.unclassified!==undefined&&s.unclassified!==null&&Number.isFinite(explicitUnclassified)
    ?Math.max(0,explicitUnclassified):Math.max(0,t-classified);
  const breakdownKnown=s.breakdown_known!==false;
  const tokenSummary=t.toLocaleString()+' total · '+own.toLocaleString()+' own · '
    +s.subagents.length+' sub-agents listed separately'
    +(breakdownKnown?'':' · '+unclassified.toLocaleString()+' unclassified');
  const breakdownStatus=classified>0
    ?'partial; session total is authoritative'
    :'unavailable; session total is entirely unclassified';
  const breakdownPairs=[['Fresh input',fresh.toLocaleString()+' tokens'],['Output',output.toLocaleString()+' tokens'],
    ['Cache writes',cacheWrite.toLocaleString()+' tokens'],['Cache reads',cacheRead.toLocaleString()+' tokens']]
    .concat(!breakdownKnown||unclassified>0?[['Unclassified',unclassified.toLocaleString()+' tokens']]:[])
    .concat(!breakdownKnown?[['Breakdown status',breakdownStatus]]:[]);
  const costKnown=s.cost_known!==false;
  el.innerHTML=breadcrumb([
      {text:'Agents',href:'#/agents/overview'},
      {text:'Sessions',href:'#/agents/sessions',attrs:'id="sess-back"'},
      {text:esc(s.project)+' '+badge(esc(s.id.slice(0,8)),{variant:'secondary',mono:true})},
    ],{attrs:'class="sess-crumbs"'})
    +'<div class="sess-grid4">'
    +statCard('Session tokens',tok(t),esc(tokenSummary),{tone:'primary'})
    +statCard('Cost'+(costKnown?' (est.)':''),costfmt(s.cost,costKnown),costKnown?'pricing '+esc(SD.meta.pricing_version||'—'):'not reported by '+esc(agentLabel(agentOf(s))))
    +statCard('Turns','<span id="sess-turn-v">'+s.turns+'</span>','<span id="sess-turn-s">model iterations</span>')
    +statCard('Duration',dur(s.duration_sec),'started '+dtfmt(s.started_at))
    +'</div>'
    +'<div class="sess-grid2 sess-grid2-wide">'
    +card({title:'Session',description:'Runtime, project and token accounting',content:kvRows([
      ['Source',agentLabel(agentOf(s))],['Runtime version',s.agent_version],['Project',s.project_path],
      ['Primary model',s.model],['Started',dtfmt(s.started_at)],['Ended',dtfmt(s.ended_at)],
      ['Breakdown scope',s.subagents.length?'session tree, including listed sub-agents':'session only'],
      ...breakdownPairs,['Session id',s.id]])})
    +card({title:'Token breakdown',description:breakdownKnown?'Share of classified tokens':'Breakdown '+breakdownStatus,content:'<div id="ch-breakdown"></div>'})
    +'</div>'
    +'<div class="sess-grid2">'
    +card({title:'Tools',description:s.tools.reduce((a,x)=>a+x.calls,0)+' calls, top '+s.tools.length,content:toolTable(s.tools)})
    +card({title:'Sub-agents',description:s.subagents.length+' spawned',content:subTable(s.subagents)})
    +'</div>'
    +card({title:'Prompts by turn',description:'What you typed, one exchange per turn',content:'<div id="sess-prompts"><div class="sess-note">'+spinner()+' loading prompts…</div></div>'});
  document.getElementById('sess-back').addEventListener('click',e=>{e.preventDefault();sel=null;render();});
  const parts=[['fresh','Fresh input',fresh],['output','Output',output],['cache_write','Cache writes',cacheWrite],['cache_read','Cache reads',cacheRead]]
    .concat(unclassified>0?[['unclassified','Unclassified',unclassified]]:[]);
  const config={};parts.forEach(([key,label],i)=>{config[key]={label,color:CHART_COLORS[i%CHART_COLORS.length]};});
  donutChart(document.getElementById('ch-breakdown'),{data:parts.map(([key,,value])=>({key,value})),config,format:tok,center:{value:tok(t),label:'tokens'},height:230});
  renderPrompts(s);
}
/* Prompt text is deliberately absent from the aggregate sessions.json payload;
   each detail view lazily pulls its own session's typed prompts and caches the
   result for the tab's lifetime (transcripts of finished sessions are stable). */
function renderPrompts(s){
  const host=document.getElementById('sess-prompts');
  if(!host)return;
  const key=sessionKey(s);
  const cached=promptsCache.get(key);
  if(cached){host.innerHTML=promptsHtml(cached,s);return;}
  apiFetch('data/session_prompts.json?agent='+encodeURIComponent(agentOf(s))
    +'&sid='+encodeURIComponent(s.id)).then(res=>{
    /* the user may have navigated away (or into another session) meanwhile */
    if(document.getElementById('sess-prompts')!==host)return;
    if(!res.ok){
      /* a 404 is only "transcript gone" when the API itself said so; a bare
         "Not Found" means the route is missing (server predates it) */
      host.innerHTML=note(res.status===404&&res.error!=='Not Found'
        ?'No transcript found for this session (its log may have been cleaned up).'
        :res.status===404
          ?'This server does not have the prompts endpoint yet — restart xo-space to pick it up.'
          :'Could not load prompts ('+esc(res.error||'error')+').');
      return;
    }
    promptsCache.set(key,res.data);
    host.innerHTML=promptsHtml(res.data,s);
    syncTurnCard(res.data,s);
  });
}
/* Once the transcript is read, "Turns" means what a person expects: one
   exchange per typed prompt. The runtime's own iteration counter (every
   tool-use round trip) moves to the sub-line. */
function syncTurnCard(d,s){
  const v=document.getElementById('sess-turn-v'),sub=document.getElementById('sess-turn-s');
  if(!v||!sub||d.supported===false||!(d.prompts||[]).length)return;
  v.textContent=d.total_prompts;
  sub.textContent='exchanges · '+s.turns+' model iterations';
}
function promptsHtml(d,s){
  if(d.supported===false)
    return note('Prompt capture is not available for '+esc(agentLabel(agentOf(s)))+' sessions yet.');
  const ps=d.prompts||[];
  if(!ps.length)return note('no typed prompts recorded in this session');
  return'<div class="sess-prompts-sub">'+d.total_prompts+' turn'+(d.total_prompts===1?'':'s')
    +' (one exchange per turn: your prompt plus every reply and tool call before the next prompt)'
    +(d.capped?' · newest '+ps.length+' shown':'')+'</div>'
    +itemGroup(ps.map((p,i)=>(i?itemSeparator():'')+item({cls:'sess-prompt',size:'sm',
      header:'<span class="sess-prompt-meta">'+badge('Turn '+p.turn,{variant:'secondary'})+'<span>'+dtfmt(p.timestamp)+'</span>'
        +(p.responses!==undefined?'<span>'+p.responses+' repl'+(p.responses===1?'y':'ies')+' · '+p.tool_uses+' tool call'+(p.tool_uses===1?'':'s')+'</span>':'')+'</span>',
      content:'<div class="sess-prompt-text">'+esc(p.text)+(p.truncated?'<span class="sess-prompt-trunc"> [… truncated]</span>':'')+'</div>',
    })).join(''));
}
function toolTable(ts){
  if(!ts.length)return empty({size:'sm',icon:icons.inbox,title:'No tool calls recorded'});
  return table({head:[{html:'Tool'},{html:'Calls',cls:'num'},{html:'Errors',cls:'num'}],
    rows:ts.map(x=>({cells:[{html:'<b>'+esc(x.name)+'</b>'},{html:String(x.calls),cls:'num'},{html:x.errors?String(x.errors):'',cls:'num'+(x.errors?' err':'')}]}))});
}
function subTable(ss){
  if(!ss.length)return empty({size:'sm',icon:icons.bot,title:'No sub-agents spawned'});
  return table({head:[{html:'Id'},{html:'Tokens',cls:'num'},{html:'Cost',cls:'num'},{html:'Turns',cls:'num'}],
    rows:ss.map(x=>({cells:[{html:esc(x.id.split('/').pop())},{html:tok(x.tokens),cls:'num acc'},{html:costfmt(x.cost,x.cost_known),cls:'num'},{html:String(x.turns),cls:'num'}]}))});
}

/* ---- Trends: charts only. Weekly volume by model and by project, share
   donuts, tool usage. Nothing the Overview already shows repeats here, and
   the raw rows stay out of the page: every card exports its full dataset
   as CSV for anyone who wants the detail. ---- */
function isoWeek(day){
  const d=new Date(day+'T00:00:00Z');
  const th=new Date(d);th.setUTCDate(d.getUTCDate()+3-((d.getUTCDay()+6)%7));
  const y=th.getUTCFullYear();
  const w=Math.ceil(((th-new Date(Date.UTC(y,0,1)))/864e5+1)/7);
  return y+'-W'+String(w).padStart(2,'0');
}
/* Weekly rollup of rows grouped by `dim` (model or project): the five
   heaviest groups become stacked series, the rest fold into Other. */
function weeklyStack(rows,dim,short){
  const wk=new Map(),totals=new Map();
  rows.forEach(r=>{
    const k=isoWeek(r.day),g=r[dim];
    const week=wk.get(k)||new Map();
    week.set(g,(week.get(g)||0)+num(r.tokens));wk.set(k,week);
    totals.set(g,(totals.get(g)||0)+num(r.tokens));
  });
  const weeks=[...wk.keys()].sort();
  const top=[...totals.entries()].sort((a,b)=>b[1]-a[1]).slice(0,5).map(([g])=>g);
  const config={};top.forEach((g,i)=>{config['s'+i]={label:short(g),color:CHART_COLORS[i]};});
  const hasOther=totals.size>top.length;
  if(hasOther)config.other={label:'Other',color:'var(--chart-7)'};
  const data=weeks.map(k=>{
    const row={x:k};let rest=0;
    wk.get(k).forEach((t,g)=>{const i=top.indexOf(g);if(i>=0)row['s'+i]=t;else rest+=t;});
    if(hasOther)row.other=rest;
    return row;
  });
  return{data,config,weeks};
}
/* Share of tokens by group, top seven plus Other, for a donut. */
function shareDonut(host,groups,short){
  const rows=[...groups.entries()].sort((a,b)=>b[1].t-a[1].t);
  const total=rows.reduce((a,[,v])=>a+v.t,0);
  if(!total){host.innerHTML='';host.insertAdjacentHTML('beforeend',empty({size:'sm',icon:icons.inbox,title:'No usage in this window'}));return;}
  const top=rows.slice(0,7),rest=rows.slice(7).reduce((a,[,v])=>a+v.t,0);
  const config={};
  top.forEach(([g],i)=>{config['s'+i]={label:short(g),color:CHART_COLORS[i%CHART_COLORS.length]};});
  if(rest>0)config.other={label:'Other',color:'var(--chart-7)'};
  donutChart(host,{data:top.map(([,v],i)=>({key:'s'+i,value:v.t})).concat(rest>0?[{key:'other',value:rest}]:[]),config,format:tok,center:{value:tok(total),label:'tokens'},height:240});
}
function rTrends(el){
  const co=cutoff();
  const dm=SD.daily_models.filter(r=>agentOn(r)&&r.day>=co);
  const dt=SD.daily_tools.filter(r=>agentOn(r)&&r.day>=co);
  /* per-session daily rows carry no project; join them to the loaded
     session list. Rows whose session is not loaded group as Other sessions. */
  const meta=new Map(SD.sessions.filter(agentOn).map(s=>[sessionKey(s),s]));
  const dp=SD.daily_sessions.filter(r=>agentOn(r)&&r.day>=co)
    .map(r=>({...r,project:meta.get(dailySessionKey(r))?.project||'Other sessions'}));

  const byModel=new Map();
  dm.forEach(r=>{const p=byModel.get(r.model)||{t:0,c:0,known:true};p.t+=num(r.tokens);p.c+=num(r.cost);p.known=p.known&&r.cost_known!==false;byModel.set(r.model,p);});
  const byProject=new Map();
  dp.forEach(r=>{const p=byProject.get(r.project)||{t:0,c:0,known:true,s:new Set()};p.t+=num(r.tokens);p.c+=num(r.cost);p.known=p.known&&r.cost_known!==false;p.s.add(dailySessionKey(r));byProject.set(r.project,p);});
  const modelTotal=[...byModel.values()].reduce((a,v)=>a+v.t,0)||1;
  const projectTotal=[...byProject.values()].reduce((a,v)=>a+v.t,0)||1;
  const weeklyModels=weeklyStack(dm,'model',mshort);
  const weeklyProjects=weeklyStack(dp,'project',g=>g);

  const byTool=new Map();
  dt.forEach(r=>{const p=byTool.get(r.name)||{c:0,e:0};p.c+=r.calls;p.e+=r.errors;byTool.set(r.name,p);});
  const calls=dt.reduce((a,r)=>a+r.calls,0),errs=dt.reduce((a,r)=>a+r.errors,0);
  const tools=[...byTool.entries()].sort((a,b)=>b[1].c-a[1].c);
  const mcp=new Map();
  byTool.forEach((v,name)=>{
    if(!name.startsWith('mcp__'))return;
    const rest=name.slice(5),i=rest.indexOf('__');
    if(i<0)return;
    const srv=rest.slice(0,i),p=mcp.get(srv)||{c:0,e:0,t:new Set()};
    p.c+=v.c;p.e+=v.e;p.t.add(name);mcp.set(srv,p);});
  const servers=[...mcp.entries()].sort((a,b)=>b[1].c-a[1].c);
  const rate=calls?errs/calls:0;

  el.innerHTML=card({title:'Tokens per week by model',description:'Stacked by the five heaviest models, '+winText(),action:csvButton('weekly-models'),content:'<div id="ch-wk"></div>'})
    +card({title:'Tokens per week by project',description:'Stacked by the five heaviest projects, '+winText(),action:csvButton('weekly-projects'),content:'<div id="ch-wk-projects"></div>'})
    +'<div class="sess-grid2">'
    +card({title:'Models',description:byModel.size+' models, share of tokens '+winText(),action:csvButton('models'),content:'<div id="ch-share"></div>'})
    +card({title:'Projects',description:byProject.size+' projects, share of tokens '+winText(),action:csvButton('projects'),content:'<div id="ch-projects"></div>'})
    +'</div>'
    +card({title:'Tools',description:calls.toLocaleString()+' calls, '+errs.toLocaleString()+' errors, '+byTool.size+' distinct tools, '+winText(),action:csvButton('tools'),
      content:tools.length?'<div class="sess-grid3 sess-tools-charts"><div id="ch-tools"></div><div id="ch-rate"></div></div>':empty({size:'sm',icon:icons.wrench,title:'No tool calls in this window'})})
    +card({title:'MCP servers',description:servers.length+' servers, calls routed through MCP tools, '+winText(),action:csvButton('mcp'),
      content:servers.length?'<div id="ch-mcp"></div>':empty({size:'sm',icon:icons.inbox,title:'No MCP tool calls in this window'})});

  const stacked=(host,{data,config})=>data.length
    ?barChartStacked(host,{data,x:'x',series:Object.keys(config).map(key=>({key})),config,format:tok,xFormat:k=>k.slice(5),aspect:3.4})
    :host.insertAdjacentHTML('beforeend',empty({size:'sm',icon:icons.inbox,title:'No weekly data in this window'}));
  stacked(document.getElementById('ch-wk'),weeklyModels);
  stacked(document.getElementById('ch-wk-projects'),weeklyProjects);
  shareDonut(document.getElementById('ch-share'),byModel,mshort);
  shareDonut(document.getElementById('ch-projects'),byProject,g=>g);
  if(tools.length){
    barChartHorizontal(document.getElementById('ch-tools'),{data:tools.slice(0,10).map(([n,v])=>({label:n,value:v.c})),config:chartConfig('value','Calls'),format:v=>v.toLocaleString()});
    radialChart(document.getElementById('ch-rate'),{value:rate,config:chartConfig('value','Error rate',rate>0.1?'var(--chart-4)':'var(--chart-1)'),text:(100*rate).toFixed(1)+'%',label:errs.toLocaleString()+' errors',height:170});
  }
  if(servers.length)barChartHorizontal(document.getElementById('ch-mcp'),{data:servers.slice(0,12).map(([n,v])=>({label:n,value:v.c})),config:chartConfig('value','Calls','var(--chart-2)'),format:v=>v.toLocaleString()});

  const now=new Date();
  const stamp=win+'-'+now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');
  const byWeek=(rows,dim)=>{
    const out=new Map();
    rows.forEach(r=>{const k=isoWeek(r.day)+'\x00'+r[dim],p=out.get(k)||{week:isoWeek(r.day),group:r[dim],t:0,c:0,known:true};
      p.t+=num(r.tokens);p.c+=num(r.cost);p.known=p.known&&r.cost_known!==false;out.set(k,p);});
    return[...out.values()].sort((a,b)=>b.week.localeCompare(a.week)||b.t-a.t).map(p=>[p.week,p.group,p.t,p.known?p.c.toFixed(4):'',p.known]);
  };
  const exports={
    'weekly-models':()=>downloadCsv('agents-weekly-models-'+stamp+'.csv',['week','model','tokens','cost_usd','cost_known'],byWeek(dm,'model')),
    'weekly-projects':()=>downloadCsv('agents-weekly-projects-'+stamp+'.csv',['week','project','tokens','cost_usd','cost_known'],byWeek(dp,'project')),
    models:()=>downloadCsv('agents-models-'+stamp+'.csv',['model','tokens','share_pct','cost_usd','cost_known'],
      [...byModel.entries()].sort((a,b)=>b[1].t-a[1].t).map(([m,v])=>[m,v.t,(100*v.t/modelTotal).toFixed(2),v.known?v.c.toFixed(4):'',v.known])),
    projects:()=>downloadCsv('agents-projects-'+stamp+'.csv',['project','tokens','share_pct','sessions','cost_usd','cost_known'],
      [...byProject.entries()].sort((a,b)=>b[1].t-a[1].t).map(([g,v])=>[g,v.t,(100*v.t/projectTotal).toFixed(2),v.s.size,v.known?v.c.toFixed(4):'',v.known])),
    tools:()=>downloadCsv('agents-tools-'+stamp+'.csv',['tool','calls','errors','error_rate_pct'],
      tools.map(([n,v])=>[n,v.c,v.e,v.c?(100*v.e/v.c).toFixed(2):''])),
    mcp:()=>downloadCsv('agents-mcp-servers-'+stamp+'.csv',['server','calls','errors','tools_used'],
      servers.map(([n,v])=>[n,v.c,v.e,v.t.size])),
  };
  el.querySelectorAll('[data-csv]').forEach(b=>b.addEventListener('click',()=>exports[b.dataset.csv]?.()));
}

/* ---- Configure: data collection per telemetry source ----
   One card per source, laid out like xo-swarm's agent cards
   (app/projects/create): a hero with the vendor mark and a status pill,
   title + vendor badge, "Powered by", an Overview | Details tab pair, and
   a footer control. Chat agents and the activity watcher stay in Setup's
   Intelligence layer; this page is only about what Space reads. */
const VENDORS={
  anthropic:{label:'Anthropic',color:'#fb923c'},
  openai:{label:'OpenAI',color:'#60a5fa'},
  cursor:{label:'Cursor',color:'#a78bfa'},
  google:{label:'Google',color:'#34d399'},
  nous:{label:'Nous',color:'#818cf8'},
  openclaw:{label:'OpenClaw',color:'#a8d94f'},
  unknown:{label:'Runtime',color:'#8f8a7e'},
};
const VENDOR_MARKS={
  anthropic:'<svg viewBox="0 0 24 24" fill="currentColor" fill-rule="evenodd" aria-hidden="true"><path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z"/></svg>',
  openai:'<svg viewBox="0 0 24 24" fill="currentColor" fill-rule="evenodd" aria-hidden="true"><path d="M21.55 10.004a5.416 5.416 0 00-.478-4.501c-1.217-2.09-3.662-3.166-6.05-2.66A5.59 5.59 0 0010.831 1C8.39.995 6.224 2.546 5.473 4.838A5.553 5.553 0 001.76 7.496a5.487 5.487 0 00.691 6.5 5.416 5.416 0 00.477 4.502c1.217 2.09 3.662 3.165 6.05 2.66A5.586 5.586 0 0013.168 23c2.443.006 4.61-1.546 5.361-3.84a5.553 5.553 0 003.715-2.66 5.488 5.488 0 00-.693-6.497v.001zm-8.381 11.558a4.199 4.199 0 01-2.675-.954c.034-.018.093-.05.132-.074l4.44-2.53a.71.71 0 00.364-.623v-6.176l1.877 1.069c.02.01.033.029.036.05v5.115c-.003 2.274-1.87 4.118-4.174 4.123zM4.192 17.78a4.059 4.059 0 01-.498-2.763c.032.02.09.055.131.078l4.44 2.53c.225.13.504.13.73 0l5.42-3.088v2.138a.068.068 0 01-.027.057L9.9 19.288c-1.999 1.136-4.552.46-5.707-1.51h-.001zM3.023 8.216A4.15 4.15 0 015.198 6.41l-.002.151v5.06a.711.711 0 00.364.624l5.42 3.087-1.876 1.07a.067.067 0 01-.063.005l-4.489-2.559c-1.995-1.14-2.679-3.658-1.53-5.63h.001zm15.417 3.54l-5.42-3.088L14.896 7.6a.067.067 0 01.063-.006l4.489 2.557c1.998 1.14 2.683 3.662 1.529 5.633a4.163 4.163 0 01-2.174 1.807V12.38a.71.71 0 00-.363-.623zm1.867-2.773a6.04 6.04 0 00-.132-.078l-4.44-2.53a.731.731 0 00-.729 0l-5.42 3.088V7.325a.068.068 0 01.027-.057L14.1 4.713c2-1.137 4.555-.46 5.707 1.513.487.833.664 1.809.499 2.757h.001zm-11.741 3.81l-1.877-1.068a.065.065 0 01-.036-.051V6.559c.001-2.277 1.873-4.122 4.181-4.12.976 0 1.92.338 2.671.954-.034.018-.092.05-.131.073l-4.44 2.53a.71.71 0 00-.365.623l-.003 6.173v.002zm1.02-2.168L12 9.25l2.414 1.375v2.75L12 14.75l-2.415-1.375v-2.75z"/></svg>',
  cursor:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/></svg>',
};
const vendorOf=row=>VENDORS[row.vendor]||VENDORS.unknown;
const vendorMark=row=>VENDOR_MARKS[row.vendor]||icons.bot;
const relTime=iso=>{
  if(!iso)return'';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'';
  return s<3600?Math.max(1,Math.floor(s/60))+'m ago':s<86400?Math.floor(s/3600)+'h ago':Math.floor(s/86400)+'d ago';
};
/* Collection state of one source: what the last telemetry build said,
   overridden by the switch the person just flipped. */
function collectionState(row){
  const live=SD?.meta?.sources?.find(source=>source.id===row.id);
  if(!row.enabled)return{key:'off',label:'Paused',detail:''};
  if(!live)return{key:'pending',label:'Pending',detail:''};
  if(live.status==='disabled')return{key:'pending',label:'Starting',detail:''};
  if(live.available)return{key:'on',label:'Collecting',detail:''};
  return{key:'down',label:'Not found',detail:live.reason||live.message||'Telemetry source unavailable.'};
}
function agentUsage(id){
  const totals=SD?.totals||{};
  const pick=map=>map&&typeof map==='object'?num(map[id]):0;
  const sessions=SD?.sessions?.filter(s=>agentOf(s)===id)||[];
  const last=sessions.reduce((best,s)=>s.started_at>best?s.started_at:best,'');
  const days=[];
  const byDay=new Map();
  (SD?.daily_models||[]).forEach(r=>{if(agentOf(r)===id)byDay.set(r.day,(byDay.get(r.day)||0)+num(r.tokens));});
  for(let i=29;i>=0;i--){const d=new Date(Date.now()-i*864e5).toISOString().slice(0,10);days.push({x:d,v:byDay.get(d)||0});}
  return{sessions:pick(totals.sessions_by_agent),tokens:pick(totals.tokens_by_agent),cost:pick(totals.cost_by_agent),last,days};
}
/* One card per source, three full-width bands sharing one left edge:
     header   mark · name + vendor tag · status line        switch
     content  Sessions / Tokens / Cost
     footer   ENV_KEY  path                                  Edit
   The vendor color appears only on the tag; status only on its dot. */
const stat=(labelText,value)=>'<div class="sess-stat"><span>'+esc(labelText)+'</span><b>'+value+'</b></div>';
function agentCard(row){
  const vendor=vendorOf(row),state=collectionState(row),usage=agentUsage(row.id);
  const busy=srcBusy.has(row.id),editing=srcEditing.has(row.id);
  const note=srcNote.get(row.id);
  const costKnown=row.cost_status!=='unavailable';
  const inputId='sess-src-path-'+row.id;
  const cost=!costKnown?'—':usage.cost>=1000?'~$'+(usage.cost/1000).toFixed(1)+'K':usd(usage.cost);
  const problem=note?.tone==='error'?note.text:state.key==='down'?state.detail:'';
  const footer=!row.path.editable
    ?'<div class="sess-path"><span class="sess-path-key">auto</span><span class="sess-path-value">Discovered automatically</span></div>'
    :editing
      ?'<div class="sess-path is-editing">'
        +'<label class="sess-path-key" for="'+inputId+'">'+esc(row.path.env)+'</label>'
        +input({id:inputId,value:srcDraft.has(row.id)?srcDraft.get(row.id):row.path.configured,placeholder:row.path.default,disabled:busy,attrs:'data-path-input="'+esc(row.id)+'"'})
        +'</div>'
        +'<div class="sess-path-actions">'
        +(row.path.configured?button('Use default',{variant:'ghost',size:'sm',disabled:busy,attrs:'data-path-reset="'+esc(row.id)+'" title="'+esc(row.path.default)+'"'}):'')
        +button('Cancel',{variant:'outline',size:'sm',disabled:busy,attrs:'data-path-cancel="'+esc(row.id)+'"'})
        +button(busy?spinner('Saving'):'Save',{size:'sm',disabled:busy,attrs:'data-path-save="'+esc(row.id)+'"'})
        +'</div>'
      :'<div class="sess-path">'
        +'<span class="sess-path-key">'+esc(row.path.env)+'</span>'
        +'<span class="sess-path-value" title="'+esc(row.path.resolved)+'">'+esc(row.path.effective)+'</span>'
        +button(icons.pencil,{variant:'outline',size:'icon-sm',disabled:busy,attrs:'data-path-edit="'+esc(row.id)+'" aria-label="Edit '+esc(row.path.label)+'" title="Edit"'})
        +'</div>';
  return'<div data-slot="card" class="sess-agent-card" data-source="'+esc(row.id)+'" data-collection="'+state.key+'" style="--vendor:'+vendor.color+'">'
    +'<div data-slot="card-header" class="sess-agent-head">'
      +'<span class="sess-agent-mark">'+vendorMark(row)+'</span>'
      +'<div class="sess-agent-title">'
        +'<div data-slot="card-title">'+esc(row.label)+badge(esc(vendor.label),{variant:'outline',attrs:'data-vendor-tag'})+'</div>'
        +'<div data-slot="card-description" class="sess-agent-status" data-state="'+state.key+'"><i></i>'+esc(state.label)+'</div>'
      +'</div>'
      +switchControl({checked:row.enabled,disabled:busy,attrs:'data-collect="'+esc(row.id)+'" aria-label="Collect from '+esc(row.label)+'" aria-busy="'+busy+'"'})
    +'</div>'
    +'<div data-slot="card-content" class="sess-agent-stats">'
      +stat('Sessions',usage.sessions.toLocaleString())+stat('Tokens',tok(usage.tokens))+stat('Cost',cost)
    +'</div>'
    +'<div data-slot="card-footer" class="sess-agent-foot">'+footer
      +(problem?'<div class="sess-path-error">'+esc(problem)+'</div>':'')
      +(note&&note.tone==='ok'?'<div class="sess-path-ok">'+esc(note.text)+'</div>':'')
    +'</div>'
  +'</div>';
}
function rConfigure(pageHead){
  const intro='<p class="sess-configure-intro">Telemetry sources Space reads on this machine. Chat agents live in <a href="#/setup/intelligence">Setup</a>.</p>';
  if(SRC===null&&!srcLoading&&!srcFailed)loadSources();
  let body='';
  if(srcLoading&&SRC===null)body='<div class="sess-agent-grid" role="status" aria-label="Loading sources">'+[0,1,2].map(()=>card({content:skeleton('height:18px;width:45%')+skeleton('height:12px;width:70%;margin-top:10px')+skeleton('height:28px;margin-top:14px')})).join('')+'</div>';
  else if(srcFailed)body=alert({variant:'destructive',icon:srcFailed==='\x00offline'?icons.wifiOff:icons.alert,
    title:srcFailed==='\x00offline'?'xo-space is unreachable':srcFailed==='\x00missing'?'This server does not have the telemetry sources endpoint yet':'Could not load telemetry sources',
    description:'<span>'+(srcFailed==='\x00missing'?'Restart xo-space to pick it up.':srcFailed==='\x00offline'?'The request never reached the server.':esc(srcFailed))+'</span>'
      +button(icons.refresh+'Retry',{variant:'outline',size:'sm',attrs:'id="sess-src-retry"'})});
  else if(!SRC.length)body=empty({icon:icons.bot,title:'No telemetry sources',description:'No installed runtime ships session telemetry.'});
  else body='<div class="sess-agent-grid">'+SRC.map(agentCard).join('')+'</div>';
  wrap.innerHTML=pageHead+intro
    +(failed&&failed!=='\x00offline'&&SRC?.length?alert({variant:'default',icon:icons.alert,title:'No telemetry could be read',description:'<span>'+esc(failed)+'. Fix a source below, then Refresh.</span>'}):'')
    +body;
  document.getElementById('sess-src-retry')?.addEventListener('click',()=>{srcFailed=null;SRC=null;render();});
  if(!SRC)return;
  wrap.querySelectorAll('[data-path-edit]').forEach(b=>b.addEventListener('click',()=>{
    srcEditing.add(b.dataset.pathEdit);srcNote.delete(b.dataset.pathEdit);render();
    wrap.querySelector('[data-path-input="'+CSS.escape(b.dataset.pathEdit)+'"]')?.focus();
  }));
  wrap.querySelectorAll('[data-path-cancel]').forEach(b=>b.addEventListener('click',()=>{srcEditing.delete(b.dataset.pathCancel);srcDraft.delete(b.dataset.pathCancel);srcNote.delete(b.dataset.pathCancel);render();}));
  wrap.querySelectorAll('[data-path-input]').forEach(box=>{
    box.addEventListener('input',()=>srcDraft.set(box.dataset.pathInput,box.value));
    box.addEventListener('keydown',e=>{
      if(e.key==='Enter'){e.preventDefault();wrap.querySelector('[data-path-save="'+CSS.escape(box.dataset.pathInput)+'"]')?.click();}
      if(e.key==='Escape'){e.preventDefault();wrap.querySelector('[data-path-cancel="'+CSS.escape(box.dataset.pathInput)+'"]')?.click();}
    });
  });
  wrap.querySelectorAll('[data-path-save]').forEach(b=>b.addEventListener('click',()=>{
    const id=b.dataset.pathSave;
    saveSource(id,{path:wrap.querySelector('[data-path-input="'+CSS.escape(id)+'"]')?.value??''});
  }));
  wrap.querySelectorAll('[data-path-reset]').forEach(b=>b.addEventListener('click',()=>{srcDraft.delete(b.dataset.pathReset);saveSource(b.dataset.pathReset,{path:''});}));
  wrap.querySelectorAll('[data-collect]').forEach(sw=>sw.addEventListener('click',()=>{
    const id=sw.dataset.collect;
    saveSource(id,{enabled:sw.dataset.state!=='checked'});
  }));
}

/* redraw chart views when the panel width actually changes */
const hostSection=wrap.closest('.view');
let lastW=0;
new ResizeObserver(es=>{
  const w=es[0].contentRect.width;
  if(Math.abs(w-lastW)>4){lastW=w;
    if(SD&&hostSection.classList.contains('is-active'))render();}
}).observe(hostSection);

    _open=requested=>{
      if(SUBS.some(([key])=>key===requested))sub=requested;
      if(!SD&&!loading)load();
      render();
    };
  },
  show(){if(_open)_open();}
};

export default agentController;
