/* One results drawer for Setup's command Inbox and the Inbox Jobs section.
   Reads only: opening results never starts a command or creates inbox items. */
import {API_BASE,apiFetch,failText} from './api.js';
import {esc,toast} from './ui.js';

let drawer=null,current=null,timer=null,requestVersion=0,painted='';
const duration=value=>value==null||!Number.isFinite(Number(value))?'—':Number(value).toFixed(2)+'s';

function ensureDrawer(){
  if(drawer)return;
  drawer=document.createElement('dialog');
  drawer.id='command-runs';
  drawer.className='setup-runs-drawer';
  drawer.setAttribute('aria-labelledby','command-runs-title');
  drawer.innerHTML=`<div class="setup-card-head">
    <div><span>Command results</span><h2 id="command-runs-title">Inbox</h2></div>
    <button type="button" class="setup-secondary" id="command-runs-close">Close</button>
    </div><div class="setup-command-body">
      <div class="command-results-tools"><p>Latest 20 runs · newest first</p></div>
      <div id="command-runs-body"></div>
    </div>`;
  document.body.appendChild(drawer);
  drawer.querySelector('#command-runs-close').addEventListener('click',()=>drawer.close());
  drawer.addEventListener('click',async event=>{
    if(!event.target.closest('#command-log-copy')||!current?.history?.log_path)return;
    try{await navigator.clipboard.writeText(current.history.log_path);toast('Log path copied');}
    catch{toast('Could not copy the log path. Select the path below to copy it.');}
  });
  drawer.addEventListener('close',()=>{
    clearTimeout(timer);requestVersion++;
    const opener=current?.opener?.isConnected?current.opener
      :current?.focusSelector?document.querySelector(current.focusSelector):null;
    current=null;
    if(opener?.isConnected)opener.focus({preventScroll:true});
  });
  // Keep numbered tab shortcuts out of the modal, while preserving native Escape.
  drawer.addEventListener('keydown',event=>event.stopPropagation());
  addEventListener('hashchange',()=>{if(drawer.open)drawer.close();});
}

function renderResults(){
  const {job,history,errors}=current;
  const key=JSON.stringify([job,history,errors]);
  if(key===painted)return;
  painted=key;
  const records=Array.isArray(history?.runs)?history.runs.filter(run=>run&&typeof run==='object'):[];
  const body=drawer.querySelector('#command-runs-body');
  body.innerHTML=errors.map(message=>`<p class="setup-form-error" role="alert">${esc(message)}</p>`).join('')
    +(job?`<div class="command-results-context">
      <code>${esc(JSON.stringify(job.command?.argv||[]))}</code>
      <p>Working directory: <code>${esc(job.command?.cwd||'Server working directory')}</code></p>
      ${job.running?`<p role="status">Running since ${esc(job.running_since||'now')} · results update automatically</p>`:''}
    </div>`:'')
    +(history?.log_path?`<div class="command-results-log"><p>Full log on this machine</p>
      <code class="setup-command-log">${esc(history.log_path)}</code>
      <button type="button" class="setup-secondary" id="command-log-copy">Copy log path</button></div>`:'')
    +(records.length?records.map(run=>`<article class="setup-run">
      <b>${esc(run.status)} · ${esc(duration(run.duration_seconds))}</b>
      <p>${esc(run.started_at)} → ${esc(run.finished_at)}</p>
      <p>${esc(run.trigger||'interrupted')} · exit ${esc(run.returncode??'—')}</p>
      ${run.reason?`<p>${esc(run.reason)}</p>`:''}
      <pre>${esc(run.output_tail||'(no output)')}</pre></article>`).join('')
      :history?`<div class="setup-empty">${job?.running?'This command is running. Its result will appear here when it finishes.':'No results yet. Run this command from Setup to record its output.'}</div>`:'');
}

async function loadResults(){
  if(!current||!drawer.open)return;
  clearTimeout(timer);
  const mine=++requestVersion;
  const selected=current;
  const path=API_BASE+'/api/schedules/'+encodeURIComponent(selected.id);
  // A terminal status must precede its history read: parallel snapshots can
  // otherwise stop polling with the output from just before completion.
  const job=await apiFetch(path);
  if(mine!==requestVersion||current!==selected||!drawer.open)return;
  // Closing and reopening can leave an older history GET in flight. Give this
  // read its own key so apiFetch cannot share that pre-completion snapshot.
  const history=await apiFetch(path+'/runs?limit=20&read='+mine);
  if(mine!==requestVersion||current!==selected||!drawer.open)return;
  selected.errors=[];
  if(job.ok)selected.job=job.data;
  else selected.errors.push('Command status: '+failText(job));
  if(history.ok)selected.history=history.data;
  else selected.errors.push('Results: '+failText(history));
  renderResults();
  if(selected.job?.running&&job.status!==404)timer=setTimeout(loadResults,3000);
}

export async function openCommandResults({id,name}){
  if(typeof id!=='string'||!id)return;
  ensureDrawer();
  clearTimeout(timer);requestVersion++;
  const opener=document.activeElement;
  const command=opener?.closest('[data-command-id]');
  const focusSelector=command
    ?`[data-command-id="${CSS.escape(command.dataset.commandId)}"] [data-command-action="runs"]`
    :opener?.dataset.job?`button[data-act="${CSS.escape(opener.dataset.act||'')}"][data-job="${CSS.escape(opener.dataset.job)}"]`:'';
  current={id,job:null,history:null,errors:[],opener,focusSelector};
  painted='';
  drawer.querySelector('#command-runs-title').textContent=(name||id)+' · Inbox';
  drawer.querySelector('#command-runs-body').textContent='Loading results…';
  if(!drawer.open)drawer.showModal();
  await loadResults();
}
