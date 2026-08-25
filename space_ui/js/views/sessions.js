/* Sessions tab — multi-runtime telemetry dashboard (data: GET
   /xo/sessions.json, pre-aggregated by the API). Independent of the atlas:
   own lazy fetch on
   first activation, own error handling — a graph-data failure cannot take
   this tab down, and vice versa. (The old fallbackView/window.__switchView
   dance is gone: the registry keeps tabs switchable no matter which views
   are broken.) Window filtering happens here, client-side, over per-day
   rollups. */
import {API_BASE,apiFetch} from '../core/api.js';

let _open=null;

export default {
  id:'sessions',label:'Sessions',order:4,
  async mount(){
const wrap=document.getElementById('sesswrap');
const WINS=[['today','Today'],['7d','7 days'],['30d','30 days'],['all','All']];
const WDAYS={today:1,'7d':7,'30d':30,all:null};
const SUBS=[['overview','Overview'],['sessions','Sessions'],['tools','Tools'],['models','Models'],['trends','Trends']];
let SD=null,loading=false,failed=null,win='7d',sub='overview',sel=null,sortK='started_at',sortD=-1,enabledAgents=null,page=0;
const PAGE_SIZE=10;              /* sessions list page length */
const promptsCache=new Map();    /* session key -> session_prompts payload */

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
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
const agentOf=row=>row?.agent||row?.source||'claude_code';
const sessionKey=row=>row?.key||(agentOf(row)+':'+row?.id);
const dailySessionKey=row=>row?.session_key||(agentOf(row)+':'+row?.session_id);
const agentOn=row=>enabledAgents?enabledAgents.has(agentOf(row)):true;
const sourceDefs=()=>{
  const rows=SD?.meta?.sources;
  return Array.isArray(rows)&&rows.length?rows:[{id:'claude_code',label:'Claude Code',available:true,cost_status:'estimated'}];
};
const agentLabel=id=>sourceDefs().find(source=>source.id===id)?.label||String(id||'Unknown').replaceAll('_',' ');
const costNote='<div class="sess-cost-note">* Partial estimate includes only sources that report cost; — means cost is unavailable.</div>';


async function load(){
  loading=true;failed=null;render();
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

/* ---- tiny chart kit: canvas, Space theme, zero deps. Mouse math is
   canvas-local (getBoundingClientRect) — the evXY lesson. ---- */
function mkcv(host,h,wFix){
  const w=wFix||Math.max(220,host.clientWidth-2);
  const c=document.createElement('canvas');
  const d=Math.min(2,devicePixelRatio||1);
  c.width=w*d;c.height=h*d;c.style.width=w+'px';c.style.height=h+'px';
  host.appendChild(c);
  const x=c.getContext('2d');x.scale(d,d);
  return[c,x,w,h];
}
function tipEl(host){
  host.style.position='relative';
  const t=document.createElement('div');
  t.style.cssText='position:absolute;display:none;pointer-events:none;background:var(--bg-3);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:11px;color:var(--ink);z-index:5;white-space:nowrap';
  host.appendChild(t);return t;
}
function drawArea(host,pts,unit){
  if(!pts.length){host.innerHTML='<div class="sess-note">no data in this window</div>';return;}
  const h=210,[c,x,w]=mkcv(host,h),P={l:46,r:12,t:12,b:22};
  const vmax=Math.max(...pts.map(p=>p.v))||1;
  const X=i=>P.l+(w-P.l-P.r)*(pts.length===1?.5:i/(pts.length-1));
  const Y=v=>P.t+(h-P.t-P.b)*(1-v/vmax);
  x.font='10px ui-monospace,monospace';
  for(let g=0;g<=3;g++){const gy=P.t+(h-P.t-P.b)*g/3;
    x.strokeStyle='#1b1f27';x.beginPath();x.moveTo(P.l,gy);x.lineTo(w-P.r,gy);x.stroke();
    x.fillStyle='#7d786d';x.fillText(tok(vmax*(1-g/3)),4,gy+3);}
  const st=Math.max(1,Math.floor(pts.length/8));
  x.fillStyle='#7d786d';
  for(let i=0;i<pts.length;i+=st)x.fillText(pts[i].d.slice(5),X(i)-12,h-6);
  x.beginPath();pts.forEach((p,i)=>i?x.lineTo(X(i),Y(p.v)):x.moveTo(X(i),Y(p.v)));
  x.strokeStyle='#a8d94f';x.lineWidth=1.8;x.stroke();
  x.lineTo(X(pts.length-1),h-P.b);x.lineTo(X(0),h-P.b);x.closePath();
  x.fillStyle='rgba(168,217,79,.12)';x.fill();
  const tip=tipEl(host);
  c.addEventListener('mousemove',e=>{
    const r=c.getBoundingClientRect();
    const f=(w-P.l-P.r)/Math.max(1,pts.length-1);
    const i=Math.max(0,Math.min(pts.length-1,Math.round((e.clientX-r.left-P.l)/f)));
    tip.style.display='block';tip.style.left=(e.clientX-r.left+12)+'px';tip.style.top=(e.clientY-r.top-10)+'px';
    tip.textContent=pts[i].d+' · '+tok(pts[i].v)+' '+(unit||'tokens');});
  c.addEventListener('mouseleave',()=>tip.style.display='none');
}
function drawHeat(host,byDay,weeks){
  weeks=weeks||16;
  const cs=16,gap=3,left=34,top=6;
  const today=new Date(),dow=(today.getDay()+6)%7;
  const end=new Date(today.getTime()+(6-dow)*864e5);
  const start=new Date(end.getTime()-(weeks*7-1)*864e5);
  let vmax=1;byDay.forEach(v=>{if(v>vmax)vmax=v;});
  const[c,x]=mkcv(host,top+7*(cs+gap)+16,left+weeks*(cs+gap)+6);
  x.font='9.5px ui-monospace,monospace';x.fillStyle='#7d786d';
  ['Mon','','Wed','','Fri','','Sun'].forEach((l,r)=>l&&x.fillText(l,2,top+r*(cs+gap)+11));
  const cells=[];
  for(let i=0;i<weeks*7;i++){
    const d=new Date(start.getTime()+i*864e5);
    if(d>today)break;
    const key=d.toISOString().slice(0,10),v=byDay.get(key)||0;
    const px=left+Math.floor(i/7)*(cs+gap),py=top+(i%7)*(cs+gap);
    x.fillStyle=v?'rgba(168,217,79,'+(0.15+0.85*Math.sqrt(v/vmax)).toFixed(3)+')':'#14171d';
    x.fillRect(px,py,cs,cs);
    cells.push({px,py,key,v});
  }
  const tip=tipEl(host);
  c.addEventListener('mousemove',e=>{
    const r=c.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
    const hit=cells.find(q=>mx>=q.px&&mx<q.px+cs&&my>=q.py&&my<q.py+cs);
    if(hit){tip.style.display='block';tip.style.left=(mx+12)+'px';tip.style.top=(my-8)+'px';
      tip.textContent=hit.key+' · '+tok(hit.v)+' tokens';}
    else tip.style.display='none';});
  c.addEventListener('mouseleave',()=>tip.style.display='none');
}
function drawBars(host,items){
  if(!items.length){host.innerHTML='<div class="sess-note">no data</div>';return;}
  const rh=26,[c,x,w]=mkcv(host,items.length*rh+8),lw=150;
  const vmax=items[0].v||1;
  x.font='11px system-ui,sans-serif';
  items.forEach((it,i)=>{
    const y=6+i*rh;
    x.fillStyle='#b3ada0';x.textAlign='right';x.fillText(it.label,lw-8,y+13,lw-14);
    const bw=Math.max(2,(w-lw-70)*it.v/vmax);
    x.fillStyle='rgba(168,217,79,.75)';x.fillRect(lw,y+3,bw,rh-10);
    x.fillStyle='#7d786d';x.textAlign='left';x.fillText(tok(it.v),lw+bw+8,y+13);
  });
}

/* ---- render dispatcher ---- */
function render(){
  if(loading){wrap.innerHTML='<div class="sess-note">loading .xo/sessions.json…</div>';return;}
  if(failed){
    const off=failed==='\x00offline';
    wrap.innerHTML='<div class="sess-err">'+(off
      ?'<b>xo-space is unreachable</b> — the request never reached the server '
        +'(stopped or restarting; the footer pill tracks it). Not a telemetry-source problem. '
      :'<b>Could not load .xo/sessions.json</b> ('+esc(failed)+'). '
        +'The API reads local telemetry for each runtime (Claude Code: <b>ARGUS_DB</b>; Codex: <b>CODEX_HOME</b>; Cursor: <b>CURSOR_HOME</b>). ')
      +'<button class="sess-refresh" id="sess-retry">Retry</button></div>';
    document.getElementById('sess-retry').addEventListener('click',load);return;
  }
  if(!SD){wrap.innerHTML='<div class="sess-note">Open this tab to load session telemetry.</div>';return;}
  const showWin=(sub==='overview'||sub==='tools');
  const sources=sourceDefs();
  wrap.innerHTML='<div class="sess-head">'
    +'<div class="sess-subnav">'+SUBS.map(([k,l])=>'<button data-sub="'+k+'" class="'+(k===sub?'is-on':'')+'">'+l+'</button>').join('')+'</div>'
    +'<fieldset class="sess-sources"><legend>Sources</legend>'
    +sources.map(source=>'<label class="'+(source.available===false?'is-unavailable':'')+'" title="'+esc(source.available===false?(source.message||source.label+' is unavailable'):source.label+' sessions')+'">'
      +'<input type="checkbox" data-agent="'+esc(source.id)+'" aria-controls="sess-body" '+(enabledAgents.has(source.id)?'checked ':'')+'>'
      +'<span>'+esc(source.label)+'</span>'+(source.available===false?'<i>offline</i>':'')+'</label>').join('')+'</fieldset>'
    +'<div class="sess-spacer"></div>'
    +(showWin?'<div class="sess-win">'+WINS.map(([k,l])=>'<button data-win="'+k+'" class="'+(k===win?'is-on':'')+'">'+l+'</button>').join('')+'</div>':'')
    +'<button class="sess-refresh" id="sess-refresh" title="Re-fetch (server rebuilds behind its 30s cache)">&#8635; Refresh</button>'
    +'</div><div id="sess-body"></div>';
  wrap.querySelectorAll('[data-sub]').forEach(b=>b.addEventListener('click',()=>{sub=b.dataset.sub;sel=null;render();}));
  wrap.querySelectorAll('[data-win]').forEach(b=>b.addEventListener('click',()=>{win=b.dataset.win;render();}));
  wrap.querySelectorAll('[data-agent]').forEach(input=>input.addEventListener('change',()=>{
    const changedAgent=input.dataset.agent;
    if(input.checked)enabledAgents.add(input.dataset.agent);else enabledAgents.delete(input.dataset.agent);
    const selected=SD.sessions.find(row=>sessionKey(row)===sel);
    if(selected&&!agentOn(selected))sel=null;
    render();
    [...wrap.querySelectorAll('[data-agent]')].find(next=>next.dataset.agent===changedAgent)?.focus();
  }));
  document.getElementById('sess-refresh').addEventListener('click',()=>{SD=null;load();});
  const el=document.getElementById('sess-body');
  if(!enabledAgents.size){
    el.innerHTML='<div class="scard sess-empty-filter"><b>No session sources selected</b><span>Turn on Claude Code, Codex, Cursor, or any combination to calculate this view.</span></div>';
    return;
  }
  if(!sources.some(source=>enabledAgents.has(source.id)&&source.available!==false)){
    el.innerHTML='<div class="scard sess-empty-filter"><b>Selected sources are unavailable</b><span>Refresh after the local telemetry store becomes readable, or choose another source.</span></div>';
    return;
  }
  if(sub==='overview')rOverview(el);
  else if(sub==='sessions'){if(sel)rDetail(el);else rList(el);}
  else if(sub==='tools')rTools(el);
  else if(sub==='models')rModels(el);
  else rTrends(el);
}

/* ---- Overview ---- */
function rOverview(el){
  const co=cutoff();
  const dm=SD.daily_models.filter(r=>agentOn(r)&&r.day>=co);
  const ds=SD.daily_sessions.filter(r=>agentOn(r)&&r.day>=co);
  const tokens=dm.reduce((a,r)=>a+num(r.tokens),0);
  const knownCostRows=dm.filter(r=>r.cost_known!==false);
  const knownCost=knownCostRows.reduce((a,r)=>a+num(r.cost),0);
  const costState=!dm.length
    ?{value:'—',label:'no data',title:'No telemetry rows in this window'}
    :!knownCostRows.length
      ?{value:'—',label:'unavailable',title:'Selected telemetry rows do not report cost'}
      :knownCostRows.length<dm.length
        ?{value:usd(knownCost),label:'partial estimate*',title:'Includes only selected telemetry rows that report cost'}
        :{value:usd(knownCost),label:'estimated',title:'Estimated from runtime pricing'};
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
  el.innerHTML='<div class="scard"><div class="sess-hero">'
    +'<div><span class="big">'+tok(tokens)+'</span> <span class="unit">TOKENS</span></div>'
    +'<div class="kv" title="'+costState.title+'"><b>'+costState.value+'</b> '+costState.label+'</div>'
    +'<div class="kv"><b>'+nsess+'</b> sessions</div>'
    +'<div class="kv"><b>'+(nsess?tok(tokens/nsess):'0')+'</b> tok/session</div>'
    +'</div></div>'
    +'<div class="scard"><div class="eyebrow2">Tokens over time</div><div id="ch-area"></div></div>'
    +'<div class="sgrid">'
    +'<div class="scard" style="overflow-x:auto"><div class="eyebrow2">Daily heatmap (last 16 weeks, all time)</div><div id="ch-heat"></div></div>'
    +'<div class="scard"><div class="eyebrow2">Top models by tokens (window)</div><div id="ch-models"></div></div>'
    +'</div>'
    +'<div class="scard" style="margin-top:16px"><div class="eyebrow2">Top sessions in window</div>'+topTable(top)+'</div>';
  drawArea(document.getElementById('ch-area'),days);
  const allByDay=new Map();SD.daily_models.filter(agentOn).forEach(r=>allByDay.set(r.day,(allByDay.get(r.day)||0)+r.tokens));
  drawHeat(document.getElementById('ch-heat'),allByDay);
  drawBars(document.getElementById('ch-models'),models.map(([m,v])=>({label:mshort(m),v})));
  el.querySelectorAll('[data-sid]').forEach(tr=>tr.addEventListener('click',()=>{sel=tr.dataset.sid;sub='sessions';render();}));
}
function topTable(rows){
  if(!rows.length)return'<div class="sess-note">no sessions in this window</div>';
  return'<table class="stbl"><tr><th>Started</th><th>Project</th><th>Source</th><th>Model</th><th class="num">Tokens (win)</th><th class="num">Cost (win)</th></tr>'
    +rows.map(r=>'<tr class="rowlink" data-sid="'+esc(r.key)+'"><td>'+dtfmt(r.m.started_at)+'</td>'
    +'<td><b>'+esc(r.m.project)+'</b></td><td><span class="source-chip source-'+esc(agentOf(r.m))+'">'+esc(agentLabel(agentOf(r.m)))+'</span></td>'
    +'<td><span class="chip">'+esc(mshort(r.m.model))+'</span></td>'
    +'<td class="num acc">'+tok(r.t)+'</td><td class="num">'+costfmt(r.c,r.known)+'</td></tr>').join('')+'</table>'
    +(rows.some(r=>!r.known)?costNote:'');
}

/* ---- Sessions list + detail ---- */
const COLS=[['started_at','Started'],['project','Project'],['agent','Source'],['model','Model'],['tokens','Tokens'],['cost','Cost'],['turns','Turns'],['duration_sec','Duration']];
function rList(el){
  const rows=SD.sessions.filter(agentOn);
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
  const cap=(selectedTotal>rows.length?'newest '+rows.length+' loaded of '+selectedTotal+' selected sessions':rows.length+' sessions (all time)')
    +(rows.length>PAGE_SIZE?' · showing '+(start+1)+'–'+(start+pageRows.length):'');
  el.innerHTML='<div class="scard"><div class="eyebrow2">'+cap+'</div>'
    +'<table class="stbl"><tr>'+COLS.map(([k,l])=>'<th data-k="'+k+'">'+l+(k===sortK?(sortD<0?' ↓':' ↑'):'')+'</th>').join('')+'<th class="num">Sub-agents</th></tr>'
    +pageRows.map(s=>'<tr class="rowlink" data-sid="'+esc(sessionKey(s))+'">'
      +'<td style="white-space:nowrap">'+dtfmt(s.started_at)+'</td><td><b>'+esc(s.project)+'</b></td>'
      +'<td><span class="source-chip source-'+esc(agentOf(s))+'">'+esc(agentLabel(agentOf(s)))+'</span></td>'
      +'<td><span class="chip">'+esc(mshort(s.model))+'</span></td>'
      +'<td class="num acc">'+tok(stok(s))+'</td><td class="num">'+costfmt(s.cost,s.cost_known)+'</td>'
      +'<td class="num">'+s.turns+'</td><td class="num">'+dur(s.duration_sec)+'</td>'
      +'<td class="num">'+(s.subagents.length||'')+'</td></tr>').join('')+'</table>'
    +(pages>1?'<div class="sess-pager">'
      +'<button id="sess-prev"'+(page?'':' disabled')+'>&larr; Prev</button>'
      +'<span>Page '+(page+1)+' of '+pages+'</span>'
      +'<button id="sess-next"'+(page<pages-1?'':' disabled')+'>Next &rarr;</button>'
      +'</div>':'')+'</div>';
  el.querySelectorAll('[data-k]').forEach(th=>th.addEventListener('click',()=>{
    const k=th.dataset.k;if(sortK===k)sortD=-sortD;else{sortK=k;sortD=-1;}page=0;render();}));
  el.querySelectorAll('[data-sid]').forEach(tr=>tr.addEventListener('click',()=>{sel=tr.dataset.sid;render();}));
  document.getElementById('sess-prev')?.addEventListener('click',()=>{if(page>0){page--;render();}});
  document.getElementById('sess-next')?.addEventListener('click',()=>{if(page<pages-1){page++;render();}});
}
const kv=(k,v)=>'<div class="kvrow"><dt>'+k+'</dt><dd>'+esc(v??'—')+'</dd></div>';
function rDetail(el){
  const s=SD.sessions.find(x=>sessionKey(x)===sel&&agentOn(x));
  if(!s){sel=null;rList(el);return;}
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
  const breakdownRows=kv('Fresh input',fresh.toLocaleString()+' tokens')
    +kv('Output',output.toLocaleString()+' tokens')
    +kv('Cache writes',cacheWrite.toLocaleString()+' tokens')
    +kv('Cache reads',cacheRead.toLocaleString()+' tokens')
    +(!breakdownKnown||unclassified>0?kv('Unclassified',unclassified.toLocaleString()+' tokens'):'')
    +(!breakdownKnown?kv('Breakdown status',breakdownStatus):'');
  const costKnown=s.cost_known!==false;
  el.innerHTML='<a class="sess-back" id="sess-back">&larr; All sessions</a>'
    +'<div class="scards4">'
    +'<div class="scard"><div class="eyebrow2">Session tokens</div><div class="v acc">'+tok(t)+'</div><div class="s">'+tokenSummary+'</div></div>'
    +'<div class="scard"><div class="eyebrow2">Cost '+(costKnown?'(est.)':'')+'</div><div class="v">'+costfmt(s.cost,costKnown)+'</div><div class="s">'+(costKnown?'pricing '+esc(SD.meta.pricing_version||'—'):'not reported by '+esc(agentLabel(agentOf(s))))+'</div></div>'
    +'<div class="scard"><div class="eyebrow2">Turns</div><div class="v" id="sess-turn-v">'+s.turns+'</div><div class="s" id="sess-turn-s">model iterations</div></div>'
    +'<div class="scard"><div class="eyebrow2">Duration</div><div class="v">'+dur(s.duration_sec)+'</div><div class="s">started '+dtfmt(s.started_at)+'</div></div>'
    +'</div>'
    +'<div class="scard"><div class="eyebrow2">Session</div><dl>'
    +kv('Source',agentLabel(agentOf(s)))+kv('Runtime version',s.agent_version)+kv('Project',s.project_path)
    +kv('Primary model',s.model)+kv('Started',dtfmt(s.started_at))+kv('Ended',dtfmt(s.ended_at))
    +kv('Breakdown scope',s.subagents.length?'session tree, including listed sub-agents':'session only')
    +breakdownRows+kv('Session id',s.id)+'</dl></div>'
    +'<div class="sgrid">'
    +'<div class="scard"><div class="eyebrow2">Tools ('+s.tools.reduce((a,x)=>a+x.calls,0)+' calls, top '+s.tools.length+')</div>'+toolTable(s.tools)+'</div>'
    +'<div class="scard"><div class="eyebrow2">Sub-agents ('+s.subagents.length+')</div>'+subTable(s.subagents)+'</div>'
    +'</div>'
    +'<div class="scard" style="margin-top:16px"><div class="eyebrow2">Prompts by turn</div>'
    +'<div id="sess-prompts"><div class="sess-note">loading prompts…</div></div></div>';
  document.getElementById('sess-back').addEventListener('click',()=>{sel=null;render();});
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
      host.innerHTML='<div class="sess-note">'+(res.status===404&&res.error!=='Not Found'
        ?'No transcript found for this session (its log may have been cleaned up).'
        :res.status===404
          ?'This server does not have the prompts endpoint yet — restart xo-space to pick it up.'
          :'Could not load prompts ('+esc(res.error||'error')+').')+'</div>';
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
    return'<div class="sess-note">Prompt capture is not available for '+esc(agentLabel(agentOf(s)))+' sessions yet.</div>';
  const ps=d.prompts||[];
  if(!ps.length)return'<div class="sess-note">no typed prompts recorded in this session</div>';
  return'<div class="sess-prompts-sub">'+d.total_prompts+' turn'+(d.total_prompts===1?'':'s')
    +' (one exchange per turn: your prompt plus every reply and tool call before the next prompt)'
    +(d.capped?' · newest '+ps.length+' shown':'')+'</div>'
    +ps.map(p=>'<div class="sess-prompt">'
      +'<div class="sess-prompt-meta"><span class="chip">Turn '+p.turn+'</span><span>'+dtfmt(p.timestamp)+'</span>'
      +(p.responses!==undefined?'<span>'+p.responses+' repl'+(p.responses===1?'y':'ies')+' · '+p.tool_uses+' tool call'+(p.tool_uses===1?'':'s')+'</span>':'')
      +'</div>'
      +'<div class="sess-prompt-text">'+esc(p.text)
      +(p.truncated?'<span class="sess-prompt-trunc"> [… truncated]</span>':'')+'</div>'
      +'</div>').join('');
}
function toolTable(ts){
  if(!ts.length)return'<div class="sess-note">no tool calls recorded</div>';
  return'<table class="stbl"><tr><th>Tool</th><th class="num">Calls</th><th class="num">Errors</th></tr>'
    +ts.map(x=>'<tr><td><b>'+esc(x.name)+'</b></td><td class="num">'+x.calls+'</td><td class="num'+(x.errors?' err':'')+'">'+(x.errors||'')+'</td></tr>').join('')+'</table>';
}
function subTable(ss){
  if(!ss.length)return'<div class="sess-note">no sub-agents spawned</div>';
  return'<table class="stbl"><tr><th>Id</th><th class="num">Tokens</th><th class="num">Cost</th><th class="num">Turns</th></tr>'
    +ss.map(x=>'<tr><td>'+esc(x.id.split('/').pop())+'</td><td class="num acc">'+tok(x.tokens)+'</td><td class="num">'+costfmt(x.cost,x.cost_known)+'</td><td class="num">'+x.turns+'</td></tr>').join('')+'</table>';
}

/* ---- Tools ---- */
function rTools(el){
  const co=cutoff();
  const rows=SD.daily_tools.filter(r=>agentOn(r)&&r.day>=co);
  const by=new Map();
  rows.forEach(r=>{const p=by.get(r.name)||{c:0,e:0};p.c+=r.calls;p.e+=r.errors;by.set(r.name,p);});
  const total=rows.reduce((a,r)=>a+r.calls,0),errs=rows.reduce((a,r)=>a+r.errors,0);
  const board=[...by.entries()].sort((a,b)=>b[1].c-a[1].c).slice(0,20);
  const mcp=new Map();
  by.forEach((v,name)=>{
    if(!name.startsWith('mcp__'))return;
    const rest=name.slice(5),i=rest.indexOf('__');
    if(i<0)return;
    const srv=rest.slice(0,i),p=mcp.get(srv)||{c:0,e:0,t:new Set()};
    p.c+=v.c;p.e+=v.e;p.t.add(name);mcp.set(srv,p);});
  el.innerHTML='<div class="scards4" style="grid-template-columns:1fr 1fr">'
    +'<div class="scard"><div class="eyebrow2">Tool calls</div><div class="v acc">'+total.toLocaleString()+'</div><div class="s">in window</div></div>'
    +'<div class="scard"><div class="eyebrow2">Errors</div><div class="v">'+errs.toLocaleString()+'</div><div class="s">'+(total?(100*errs/total).toFixed(1):'0.0')+'% error rate</div></div></div>'
    +'<div class="scard"><div class="eyebrow2">Leaderboard (top 20)</div><table class="stbl"><tr><th>Tool</th><th class="num">Calls</th><th class="num">Errors</th><th class="num">Error rate</th></tr>'
    +board.map(([n,v])=>'<tr><td><b>'+esc(n)+'</b></td><td class="num">'+v.c.toLocaleString()+'</td><td class="num'+(v.e?' err':'')+'">'+(v.e||'')+'</td><td class="num">'+(v.c?(100*v.e/v.c).toFixed(1)+'%':'—')+'</td></tr>').join('')+'</table></div>'
    +'<div class="scard"><div class="eyebrow2">MCP servers</div>'
    +(mcp.size?'<table class="stbl"><tr><th>Server</th><th class="num">Calls</th><th class="num">Errors</th><th class="num">Tools used</th></tr>'
      +[...mcp.entries()].sort((a,b)=>b[1].c-a[1].c).map(([sname,v])=>'<tr><td><b>'+esc(sname)+'</b></td><td class="num">'+v.c+'</td><td class="num'+(v.e?' err':'')+'">'+(v.e||'')+'</td><td class="num">'+v.t.size+'</td></tr>').join('')+'</table>'
      :'<div class="sess-note">no MCP tool calls in this window</div>')+'</div>';
}

/* ---- Models (all time) ---- */
function rModels(el){
  const by=new Map();
  SD.daily_models.filter(agentOn).forEach(r=>{const p=by.get(r.model)||{t:0,c:0,known:true};p.t+=r.tokens;p.c+=r.cost;p.known=p.known&&r.cost_known!==false;by.set(r.model,p);});
  const rows=[...by.entries()].sort((a,b)=>b[1].t-a[1].t);
  const tot=rows.reduce((a,[,v])=>a+v.t,0)||1;
  el.innerHTML='<div class="scard"><div class="eyebrow2">Tokens by model (all time)</div><div id="ch-mbars"></div></div>'
    +'<div class="scard"><div class="eyebrow2">Breakdown</div><table class="stbl"><tr><th>Model</th><th class="num">Tokens</th><th class="num">Share</th><th class="num">Cost</th></tr>'
    +rows.map(([m,v])=>'<tr><td><span class="chip">'+esc(mshort(m))+'</span></td><td class="num acc">'+tok(v.t)+'</td><td class="num">'+(100*v.t/tot).toFixed(1)+'%</td><td class="num">'+costfmt(v.c,v.known)+'</td></tr>').join('')+'</table>'
    +(rows.some(([,v])=>!v.known)?costNote:'')+'</div>';
  drawBars(document.getElementById('ch-mbars'),rows.slice(0,10).map(([m,v])=>({label:mshort(m),v:v.t})));
}

/* ---- Trends (weekly, all time) ---- */
function isoWeek(day){
  const d=new Date(day+'T00:00:00Z');
  const th=new Date(d);th.setUTCDate(d.getUTCDate()+3-((d.getUTCDay()+6)%7));
  const y=th.getUTCFullYear();
  const w=Math.ceil(((th-new Date(Date.UTC(y,0,1)))/864e5+1)/7);
  return y+'-W'+String(w).padStart(2,'0');
}
function rTrends(el){
  const wk=new Map();
  SD.daily_models.filter(agentOn).forEach(r=>{
    const k=isoWeek(r.day),p=wk.get(k)||{t:0,c:0,known:true,m:new Map()};
    p.t+=r.tokens;p.c+=r.cost;p.known=p.known&&r.cost_known!==false;p.m.set(r.model,(p.m.get(r.model)||0)+r.tokens);wk.set(k,p);});
  const weeks=[...wk.keys()].sort();
  if(!weeks.length){el.innerHTML='<div class="sess-note">no turn data</div>';return;}
  el.innerHTML='<div class="scard"><div class="eyebrow2">Tokens per week</div><div id="ch-wk"></div></div>'
    +'<div class="scard"><div class="eyebrow2">Weekly breakdown</div><table class="stbl"><tr><th>Week</th><th class="num">Tokens</th><th class="num">Cost</th><th>Top model</th></tr>'
    +weeks.slice().reverse().map(k=>{
      const v=wk.get(k),top=[...v.m.entries()].sort((a,b)=>b[1]-a[1])[0];
      return'<tr><td><b>'+k+'</b></td><td class="num acc">'+tok(v.t)+'</td><td class="num">'+costfmt(v.c,v.known)+'</td><td><span class="chip">'+esc(mshort(top[0]))+'</span> '+tok(top[1])+'</td></tr>';}).join('')+'</table>'
    +([...wk.values()].some(v=>!v.known)?costNote:'')+'</div>';
  drawArea(document.getElementById('ch-wk'),weeks.map(k=>({d:k,v:wk.get(k).t})));
}

/* redraw chart views when the panel width actually changes */
const hostSection=wrap.closest('.view');
let lastW=0;
new ResizeObserver(es=>{
  const w=es[0].contentRect.width;
  if(Math.abs(w-lastW)>4){lastW=w;
    if(SD&&hostSection.classList.contains('is-active'))render();}
}).observe(hostSection);

    _open=()=>{if(!SD&&!loading)load();render();};
  },
  show(){if(_open)_open();}
};
