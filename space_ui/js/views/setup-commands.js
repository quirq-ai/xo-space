/* Setup's Commands panel uses the scheduler's definitions, executor and history.
   No interval means manual only; nothing is seeded or executed on mount. */
import {apiFetch,API_BASE} from '../core/api.js';
import {toast} from '../core/ui.js';
import {openCommandResults} from '../core/command-results.js?v=20260914-results1';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const path=id=>'/api/schedules/'+encodeURIComponent(id);
const duration=value=>value==null||!Number.isFinite(Number(value))?'—':Number(value).toFixed(2)+'s';
const createEndpoint=()=>new URL(API_BASE+'/api/schedules',location.href).href;

/* The browser's offset at copy time, e.g. "+05:30", so the agent can turn
   "9 pm" into a first_run_at the server accepts. */
function utcOffset(date=new Date()){
  const minutes=-date.getTimezoneOffset(),abs=Math.abs(minutes);
  return (minutes<0?'-':'+')+String(Math.floor(abs/60)).padStart(2,'0')+':'+String(abs%60).padStart(2,'0');
}

const pad2=n=>String(n).padStart(2,'0');

/* A datetime-local value is the browser's local time; the scheduler needs an
   explicit offset, e.g. "2026-09-15T21:00:00+05:30". The offset is taken for
   that date, so a daylight-saving change between now and then is honoured. */
function localInputToIso(value){
  if(!value)return null;
  const d=new Date(value);
  if(Number.isNaN(d.getTime()))return null;
  return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate())
    +'T'+pad2(d.getHours())+':'+pad2(d.getMinutes())+':00'+utcOffset(d);
}

const localTime=value=>{
  const d=new Date(value);
  return Number.isNaN(d.getTime())?String(value):d.toLocaleString([],{dateStyle:'medium',timeStyle:'short'});
};

/* The stored UTC stamp shown back in the browser's local time. */
function isoToLocalInput(value){
  if(!value)return '';
  const d=new Date(value);
  if(Number.isNaN(d.getTime()))return '';
  return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate())+'T'+pad2(d.getHours())+':'+pad2(d.getMinutes());
}

function agentPrompt(){
  return `---
name: xo-space-saved-commands
description: Add, schedule, edit or run saved commands in XO Space (Setup → Commands) through its /api/schedules API.
---

# XO Space saved commands

Manage saved commands through the Space API at /api/schedules, never by editing its files.

- Call the Space server on the machine where it runs (usually http://127.0.0.1:5002, or 5003). If you can't reach it, or a change is refused, stop and tell me.
- Commands run on the server, as the server's user, without a shell. Always give an absolute cwd, an argv list and a timeout. Never put secrets in a command.
- Keep commands manual unless I ask for a schedule. For a schedule, use every_seconds, plus first_run_at with a UTC offset when I give a start time (my time zone is UTC${utcOffset()}). Check that next_run matches what I asked.
- Check for duplicates first. Edits replace the whole definition. Don't run anything unless I ask.
- When done, report each command's name, id, working directory and schedule.
`;
}
function relativeTime(value){
  const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(value))/1000));
  if(!Number.isFinite(seconds))return 'unknown time';
  if(seconds<60)return seconds+'s ago';
  if(seconds<3600)return Math.floor(seconds/60)+'m ago';
  if(seconds<86400)return Math.floor(seconds/3600)+'h ago';
  return Math.floor(seconds/86400)+'d ago';
}

export function mountCommands(root){
  let jobs=[],editing=null,timer=null,refreshing=false,refreshQueued=false;
  let saving=false,polling=false,revision=0;
  const busy=new Set();
  root.innerHTML=`
    <div class="setup-card-head setup-command-head"><div class="setup-command-heading"><h3>Saved commands</h3>
      <span class="setup-command-help"><button type="button" id="command-help" aria-label="About adding commands with an agent" aria-describedby="command-help-tip">i</button>
        <span id="command-help-tip" role="tooltip" hidden>Copy a short skill for your agent, then describe the commands you want and it adds them here through /api/schedules.</span></span></div>
      <div class="setup-command-tools"><button type="button" class="setup-secondary" id="command-copy-prompt">Copy agent prompt</button><button type="button" class="setup-secondary" id="command-add">Add command</button></div></div>
    <div class="setup-command-body">
      <p class="setup-command-endpoint"><span>Create job</span><code>POST ${esc(createEndpoint())}</code></p>
      <span id="command-prompt-status" class="setup-command-copy-status" role="status"></span>
      <div id="command-prompt-fallback" class="setup-command-prompt" hidden>
        <label for="command-prompt-text">Agent prompt — select and copy</label>
        <textarea id="command-prompt-text" readonly rows="8" spellcheck="false"></textarea>
        <button type="button" class="setup-secondary" id="command-prompt-close">Close</button>
      </div>
      <div class="setup-form-error" id="command-error" role="alert" hidden></div>
      <form id="command-form" class="setup-command-form" novalidate hidden>
        <h3 id="command-form-title">Add command</h3>
        <label for="command-name">Name</label>
        <input id="command-name" name="name" autocomplete="off" placeholder="Check checkout status">
        <label for="command-description">Description (optional)</label>
        <input id="command-description" name="description" autocomplete="off" placeholder="What this command does">
        <label for="command-line">Command line or argv JSON</label>
        <textarea id="command-line" name="line" rows="2" spellcheck="false" placeholder="git -C &lt;checkout&gt; status --short"></textarea>
        <small>Runs without a shell. JSON array example: ["git", "status", "--short"].</small>
        <label for="command-cwd">Working directory (optional)</label>
        <input id="command-cwd" name="cwd" spellcheck="false" placeholder="Server working directory">
        <div class="setup-command-numbers">
          <div><label for="command-timeout">Timeout (seconds)</label><input id="command-timeout" name="timeout" type="number" step="any" value="30"></div>
          <div><label for="command-interval">Interval (seconds, optional)</label><input id="command-interval" name="interval" type="number" placeholder="Manual only"></div>
          <div><label for="command-first-run">First run at (optional)</label><input id="command-first-run" name="firstRun" type="datetime-local" step="60" disabled></div>
        </div>
        <small>Leave the interval blank for manual runs. Intervals run automatically through the watcher. First run at needs an interval and uses your time zone (UTC${utcOffset()}); a past time keeps the same schedule and runs at the next slot.</small>
        <div class="setup-actions">
          <button class="setup-primary" id="command-save" type="submit">Save command</button>
          <button class="setup-secondary" id="command-cancel" type="button">Cancel</button>
        </div>
      </form>
      <div id="command-list"><div class="setup-empty">Loading commands…</div></div>
    </div>
`;
  const form=root.querySelector('#command-form');
  const error=root.querySelector('#command-error');
  const list=root.querySelector('#command-list');
  const field=name=>form.elements.namedItem(name);
  function showError(message){error.textContent=message||'';error.hidden=!message;}
  /* The scheduler refuses a start time on a manual-only command. */
  function syncFirstRun(){
    const scheduled=Boolean(field('interval').value.trim());
    field('firstRun').disabled=!scheduled;
    if(!scheduled)field('firstRun').value='';
  }
  field('interval').addEventListener('input',syncFirstRun);

  const help=root.querySelector('#command-help'),tip=root.querySelector('#command-help-tip');
  const helpArea=help.parentElement;
  helpArea.addEventListener('mouseenter',()=>{tip.hidden=false;});
  helpArea.addEventListener('mouseleave',()=>{if(document.activeElement!==help)tip.hidden=true;});
  help.addEventListener('focus',()=>{tip.hidden=false;});
  help.addEventListener('blur',()=>{tip.hidden=true;});
  help.addEventListener('click',()=>{tip.hidden=false;});
  help.addEventListener('keydown',event=>{if(event.key==='Escape'){tip.hidden=true;event.stopPropagation();}});
  root.querySelector('#command-copy-prompt').addEventListener('click',async()=>{
    const prompt=agentPrompt(),status=root.querySelector('#command-prompt-status');
    const fallback=root.querySelector('#command-prompt-fallback');
    try{
      await navigator.clipboard.writeText(prompt);
      fallback.hidden=true;status.textContent='Prompt copied. Paste it into your agent.';
    }catch{
      fallback.hidden=false;status.textContent='Clipboard unavailable. Copy the prompt below.';
      const textarea=root.querySelector('#command-prompt-text');
      textarea.value=prompt;textarea.focus();textarea.select();
    }
  });
  root.querySelector('#command-prompt-close').addEventListener('click',()=>{
    root.querySelector('#command-prompt-fallback').hidden=true;
    root.querySelector('#command-prompt-status').textContent='';
    root.querySelector('#command-copy-prompt').focus();
  });

  function render(){
    list.innerHTML=jobs.length?jobs.map(job=>{
      const result=job.last_result;
      const running=job.running;
      const status=running?'Running since '+job.running_since
        :result?result.status+' · '+relativeTime(result.finished_at)+' · '+duration(result.duration_seconds):'Not run yet';
      const disabled=busy.has(job.id)?' disabled':'';
      return `<article class="setup-command-row" data-command-id="${esc(job.id)}">
        <div class="setup-command-info"><b>${esc(job.name)}</b>
          ${job.description?`<p>${esc(job.description)}</p>`:''}
          <code>${esc(JSON.stringify(job.command.argv))}</code>
          <p class="setup-command-cwd">Working directory: <code>${esc(job.command.cwd||'Server working directory')}</code></p>
          <div class="setup-command-meta"><span class="setup-command-result ${running?'is-running':result?.status==='ok'?'is-good':result?'is-error':''}" role="status">${esc(status)}</span>
            <span>${job.every_seconds==null?'Manual only':'Runs every '+esc(job.every_seconds)+'s'+(job.enabled?(job.next_run?' · next '+esc(localTime(job.next_run)):''):' · disabled')}</span></div>
          ${result?`<div class="setup-command-preview"><span>Latest result · exit ${esc(result.returncode??'—')}</span>
            <pre>${esc(String(result.output_tail||result.reason||'(no output)').trimEnd().slice(0,400))}</pre></div>`:''}
        </div>
        <div class="setup-actions">
          <button class="setup-primary" type="button" data-command-action="run"${running?' disabled':disabled}>${running?'Running…':'Run'}</button>
          <button class="setup-secondary" type="button" data-command-action="runs" title="Open results and logs for this command"${disabled}>Inbox</button>
          <button class="setup-secondary" type="button" data-command-action="edit"${saving?' disabled':disabled}>Edit</button>
          <button class="setup-secondary is-danger" type="button" data-command-action="delete"${disabled}>Delete</button>
        </div></article>`;
    }).join(''):'<div class="setup-empty"><b>No commands yet</b><span>Save a command, then run it here.</span></div>';
    schedulePoll();
  }

  function schedulePoll(){
    clearTimeout(timer);
    if(jobs.some(job=>job.running))timer=setTimeout(pollRunning,3000);
  }
  async function pollRunning(){
    if(refreshing||polling){schedulePoll();return;}
    polling=true;
    const mine=revision;
    const active=jobs.filter(job=>job.running&&!busy.has(job.id));
    const results=await Promise.all(active.map(async job=>({id:job.id,res:await apiFetch(path(job.id))})));
    polling=false;
    if(mine!==revision){schedulePoll();return;}
    for(const {id,res} of results){
      if(res.ok)jobs=jobs.map(job=>job.id===id?res.data:job);
      else if(res.status===404)jobs=jobs.filter(job=>job.id!==id);
      else showError(res.error);
    }
    render();
  }
  async function refresh(){
    if(refreshing){refreshQueued=true;return;}
    refreshing=true;
    /* Writes invalidate older reads, and a full list refresh supersedes polls. */
    const mine=++revision;
    const res=await apiFetch('/api/schedules');
    refreshing=false;
    if(mine===revision){
      if(!res.ok)showError(res.error);
      else{jobs=res.data.jobs||[];render();}
    }
    if(refreshQueued){refreshQueued=false;await refresh();}
    else schedulePoll();
  }

  function edit(job=null){
    if(saving)return;
    editing=job;
    form.reset();
    showError('');
    root.querySelector('#command-form-title').textContent=job?'Edit command':'Add command';
    field('name').value=job?.name||'';
    field('description').value=job?.description||'';
    field('line').value=job?JSON.stringify(job.command.argv):'';
    field('cwd').value=job?.command.cwd||'';
    field('timeout').value=job?.command.timeout??30;
    field('interval').value=job?.every_seconds??'';
    field('firstRun').value=isoToLocalInput(job?.first_run_at);
    syncFirstRun();
    form.hidden=false;
    field('name').focus();
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if(saving)return;
    showError('');
    const line=field('line').value.trim();
    let command;
    try{command=line.startsWith('[')?{argv:JSON.parse(line)}:{command:line};}
    catch(err){showError('Invalid argv JSON: '+err.message);return;}
    command.timeout=Number(field('timeout').value);
    if(field('cwd').value.trim())command.cwd=field('cwd').value.trim();
    if(editing?.command.env)command.env=editing.command.env;
    const body={name:field('name').value.trim(),description:field('description').value.trim(),command,
      every_seconds:field('interval').value.trim()?Number(field('interval').value):null,
      first_run_at:field('interval').value.trim()?localInputToIso(field('firstRun').value):null,
      enabled:editing?.enabled??true,project_id:editing?.project_id??null};
    const controls=[...form.elements,root.querySelector('#command-add')];
    const editingId=editing?.id;
    saving=true;revision++;
    if(editingId)busy.add(editingId);
    controls.forEach(el=>el.disabled=true);
    render();
    const res=await apiFetch(editingId?path(editingId):'/api/schedules',{method:editingId?'PUT':'POST',body});
    saving=false;revision++;
    if(editingId)busy.delete(editingId);
    controls.forEach(el=>el.disabled=false);
    syncFirstRun();
    if(!res.ok){
      showError(res.error);render();
      if(res.status===409)await refresh();
      return;
    }
    form.hidden=true;
    editing=null;
    const saved=res.data;
    jobs=jobs.some(job=>job.id===saved.id)
      ?jobs.map(job=>job.id===saved.id?saved:job):[...jobs,saved];
    render();
    toast('Command saved');
    await refresh();
  });

  list.addEventListener('click',async event=>{
    const button=event.target.closest('[data-command-action]');
    if(!button||button.disabled)return;
    const id=button?.closest('[data-command-id]')?.dataset.commandId;
    const job=jobs.find(item=>item.id===id);
    if(!job||busy.has(id))return;
    const action=button.dataset.commandAction;
    if(action==='edit'){edit(job);return;}
    if(action==='runs'){
      await openCommandResults({id,name:job.name});
      return;
    }
    if(action==='delete'&&!confirm('Delete '+job.name+'? Saved run history and logs will be kept on disk.'))return;
    busy.add(id);
    revision++;
    render();
    const res=await apiFetch(path(id)+(action==='run'?'/run':''),{method:action==='run'?'POST':'DELETE'});
    busy.delete(id);
    revision++;
    if(!res.ok){
      showError(res.error);
      toast(res.error);
      render();
      if(res.status===409)await refresh();
      return;
    }
    showError('');
    if(action==='run')jobs=jobs.map(item=>item.id===id?res.data.job:item);
    else{
      jobs=jobs.filter(item=>item.id!==id);
      if(editing?.id===id){form.hidden=true;editing=null;}
    }
    render();
  });
  root.querySelector('#command-add').addEventListener('click',()=>edit());
  root.querySelector('#command-cancel').addEventListener('click',()=>{if(saving)return;form.hidden=true;editing=null;showError('');});
  return {refresh};
}
