/* Setup's Jobs panel uses the scheduler's definitions, executor and history.
   A Manual job has no interval; a Scheduled job's plain-language schedule is
   translated to every_seconds/first_run_at by core/jobs.js. Nothing is seeded
   or executed on mount. */
import {apiFetch,API_BASE} from '../core/api.js';
import {toast} from '../core/ui.js';
import {openCommandResults} from '../core/command-results.js?v=20260914-results1';
import {UNITS,WEEKDAYS,describeChoice,describeSchedule,durationText,isScheduled,jobToSchedule,
  scheduleToFields,splitDuration,statusText,utcOffset} from '../core/jobs.js?v=20260916-jobs1';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const path=id=>'/api/schedules/'+encodeURIComponent(id);
const duration=value=>value==null||!Number.isFinite(Number(value))?'—':Number(value).toFixed(2)+'s';
const createEndpoint=()=>new URL(API_BASE+'/api/schedules',location.href).href;
/* A new job may run for five minutes before it is stopped. */
const DEFAULT_TIMEOUT_SECONDS=300;
const TIMEOUT_UNITS=['hours','minutes','seconds'];

const localTime=value=>{
  const d=new Date(value);
  return Number.isNaN(d.getTime())?String(value):d.toLocaleString([],{dateStyle:'medium',timeStyle:'short'});
};

function agentPrompt(){
  return `---
name: xo-space-jobs
description: Add, schedule, edit or run jobs in XO Space (Setup → Jobs) through its /api/schedules API.
---

# XO Space jobs

Manage jobs through the Space API at /api/schedules, never by editing its files.

- Call the Space server on the machine where it runs (usually http://127.0.0.1:5002, or 5003). If you can't reach it, or a change is refused, stop and tell me.
- Jobs run on the server, as the server's user, without a shell. Always give an absolute cwd, an argv list and a timeout in seconds. Never put secrets in a command.
- A job is manual (every_seconds null, it runs only when someone runs it) unless I ask for a schedule. For a scheduled job, use every_seconds, plus first_run_at with a UTC offset when I give a time (my time zone is UTC${utcOffset()}); "every day at 9 pm" is every_seconds 86400 with first_run_at at the next 21:00. Check that next_run matches what I asked.
- Check for duplicates first. Edits replace the whole definition. Don't run anything unless I ask.
- When done, report each job's name, id, working directory and schedule.
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
  /* A custom interval reopened for editing keeps its anchor, so saving it
     unchanged does not move the job's run times. */
  let editingAnchor=null;
  const busy=new Set();
  root.innerHTML=`
    <div class="setup-card-head setup-command-head"><div class="setup-command-heading"><h3>Your jobs</h3>
      <span class="setup-command-help"><button type="button" id="command-help" aria-label="About adding jobs with an agent" aria-describedby="command-help-tip">i</button>
        <span id="command-help-tip" role="tooltip" hidden>Copy a short skill for your agent, then describe the jobs you want and it adds them here through /api/schedules.</span></span></div>
      <div class="setup-command-tools"><button type="button" class="setup-secondary" id="command-copy-prompt">Copy agent prompt</button><button type="button" class="setup-primary" id="command-add">New job</button></div></div>
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
        <h3 id="command-form-title">New job</h3>
        <fieldset class="setup-job-kind">
          <legend>What kind of job is this?</legend>
          <label class="setup-job-kind-option"><input type="radio" name="kind" value="scheduled"><span><b>Scheduled</b><small>Runs on its own, on a schedule you choose.</small></span></label>
          <label class="setup-job-kind-option"><input type="radio" name="kind" value="manual"><span><b>Manual</b><small>Saved for later. Runs only when you click Run now.</small></span></label>
        </fieldset>
        <div id="command-fields" class="setup-job-fields" hidden>
          <label for="command-name">Name</label>
          <input id="command-name" name="name" autocomplete="off" placeholder="Nightly tests">
          <small>Up to 64 letters, numbers, spaces, dots, dashes or underscores.</small>
          <label for="command-description">Description (optional)</label>
          <input id="command-description" name="description" autocomplete="off" placeholder="What this job does">
          <label for="command-line">Command</label>
          <textarea id="command-line" name="line" rows="2" spellcheck="false" placeholder="git status --short"></textarea>
          <small>The program and its arguments. It runs directly, without a shell, so |, &amp;&amp;, ; and &gt; are not allowed. For an argument that contains spaces, write a JSON list: ["git", "commit", "-m", "Two words"].</small>
          <label for="command-cwd">Run in folder (optional)</label>
          <input id="command-cwd" name="cwd" spellcheck="false" placeholder="/home/me/project">
          <small>An absolute path on this Space's machine. Leave it blank to use the server's own folder.</small>
          <fieldset id="command-schedule" class="setup-job-schedule">
            <legend>How often?</legend>
            <label class="setup-job-option"><input type="radio" name="repeat" value="custom"><span>Every</span><input name="every" type="number" min="1" step="1" inputmode="numeric" value="30" aria-label="Repeat every"><select name="unit" aria-label="Repeat unit"><option value="minutes">minutes</option><option value="hours">hours</option><option value="days">days</option><option value="seconds">seconds</option></select></label>
            <label class="setup-job-option"><input type="radio" name="repeat" value="hourly"><span>Every hour, at minute</span><input name="minute" type="number" min="0" max="59" step="1" inputmode="numeric" value="0" aria-label="Minute past the hour"></label>
            <label class="setup-job-option"><input type="radio" name="repeat" value="daily"><span>Every day at</span><input name="dailyTime" type="time" value="09:00" aria-label="Time of day"></label>
            <label class="setup-job-option"><input type="radio" name="repeat" value="weekly"><span>Every week on</span><select name="weekday" aria-label="Day of the week">${WEEKDAYS.map((day,index)=>`<option value="${index}">${day}</option>`).join('')}</select><span>at</span><input name="weeklyTime" type="time" value="09:00" aria-label="Time on that day"></label>
            <p id="command-schedule-preview" class="setup-job-preview" aria-live="polite"></p>
            <small>Times are in your time zone (UTC${utcOffset()}). A job keeps a fixed interval, so a daily time can move by an hour when daylight saving starts or ends. Scheduled jobs run only while <b>Update activity automatically</b> is on in Intelligence layer.</small>
          </fieldset>
          <label for="command-timeout">Stop it if a run takes longer than</label>
          <div class="setup-job-inline"><input id="command-timeout" name="timeout" type="number" min="0" step="any" inputmode="decimal"><select name="timeoutUnit" aria-label="Time limit unit"><option value="seconds">seconds</option><option value="minutes">minutes</option><option value="hours">hours</option></select></div>
          <small>A run still going at that point is stopped and marked Timed out.</small>
        </div>
        <div class="setup-actions">
          <button class="setup-primary" id="command-save" type="submit">Save job</button>
          <button class="setup-secondary" id="command-cancel" type="button">Cancel</button>
        </div>
      </form>
      <div id="command-list"><div class="setup-empty">Loading jobs…</div></div>
    </div>
`;
  const form=root.querySelector('#command-form');
  const error=root.querySelector('#command-error');
  const list=root.querySelector('#command-list');
  const fields=root.querySelector('#command-fields');
  const schedule=root.querySelector('#command-schedule');
  const preview=root.querySelector('#command-schedule-preview');
  const field=name=>form.elements.namedItem(name);
  const kind=()=>field('kind').value;
  const setRadio=(name,value)=>{const input=form.querySelector(`input[name="${name}"][value="${value}"]`);if(input)input.checked=true;};
  function showError(message){error.textContent=message||'';error.hidden=!message;}

  function scheduleChoice(){
    const repeat=field('repeat').value;
    return {kind:repeat,every:field('every').value,unit:field('unit').value,minute:field('minute').value,
      weekday:field('weekday').value,time:field(repeat==='weekly'?'weeklyTime':'dailyTime').value,anchor:editingAnchor};
  }
  /* The preview says in words what will be saved, before anything is sent. */
  function syncPreview(){
    if(kind()!=='scheduled'){preview.textContent='';return;}
    const choice=scheduleChoice(),out=scheduleToFields(choice);
    preview.classList.toggle('is-error',Boolean(out.error));
    if(out.error){preview.textContent=out.error;return;}
    const first=choice.kind==='custom'
      ?(choice.anchor?'It keeps its current run times.':'First run '+durationText(choice.every,choice.unit)+' after you save.')
      :'First run: '+localTime(out.first_run_at)+'.';
    preview.textContent='→ '+describeChoice(choice)+'. '+first;
  }
  /* Nothing but the type choice shows until a type is picked. */
  function syncForm(){
    const chosen=kind();
    fields.hidden=!chosen;
    schedule.hidden=chosen!=='scheduled';
    syncPreview();
  }
  form.addEventListener('change',event=>{
    const wasHidden=fields.hidden;
    syncForm();
    if(event.target.name==='kind'&&wasHidden&&!fields.hidden)field('name').focus();
  });
  form.addEventListener('input',syncPreview);
  /* Typing into an option's own inputs selects that option. */
  schedule.addEventListener('focusin',event=>{
    if(event.target.name==='repeat')return;
    const radio=event.target.closest('.setup-job-option')?.querySelector('input[name="repeat"]');
    if(radio&&!radio.checked){radio.checked=true;syncPreview();}
  });

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
      const scheduled=isScheduled(job);
      const status=running?'Running since '+localTime(job.running_since)
        :result?statusText(result.status)+' · '+relativeTime(result.finished_at)+' · '+duration(result.duration_seconds):'Not run yet';
      const when=scheduled
        ?describeSchedule(job)+(job.enabled?(job.next_run?' · next '+localTime(job.next_run):''):' · paused')
        :'Runs only when you click Run now';
      const disabled=busy.has(job.id)?' disabled':'';
      return `<article class="setup-command-row" data-command-id="${esc(job.id)}">
        <div class="setup-command-info"><div class="setup-job-title"><b>${esc(job.name)}</b><span class="setup-job-badge ${scheduled?'is-scheduled':'is-manual'}">${scheduled?'Scheduled':'Manual'}</span></div>
          ${job.description?`<p>${esc(job.description)}</p>`:''}
          <code>${esc(JSON.stringify(job.command.argv))}</code>
          <p class="setup-command-cwd">Runs in: <code>${esc(job.command.cwd||'Server working directory')}</code></p>
          <div class="setup-command-meta"><span class="setup-command-result ${running?'is-running':result?.status==='ok'?'is-good':result?'is-error':''}" role="status">${esc(status)}</span>
            <span>${esc(when)}</span></div>
          ${result?`<div class="setup-command-preview"><span>Latest result · exit ${esc(result.returncode??'—')}</span>
            <pre>${esc(String(result.output_tail||result.reason||'(no output)').trimEnd().slice(0,400))}</pre></div>`:''}
        </div>
        <div class="setup-actions">
          <button class="setup-primary" type="button" data-command-action="run"${running?' disabled':disabled}>${running?'Running…':'Run now'}</button>
          <button class="setup-secondary" type="button" data-command-action="runs" title="Open results and logs for this job"${disabled}>Results</button>
          <button class="setup-secondary" type="button" data-command-action="edit"${saving?' disabled':disabled}>Edit</button>
          <button class="setup-secondary is-danger" type="button" data-command-action="delete"${disabled}>Delete</button>
        </div></article>`;
    }).join(''):'<div class="setup-empty"><b>No jobs yet</b><span>Click New job to schedule a command, or save one to run when you choose.</span></div>';
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
    root.querySelector('#command-form-title').textContent=job?'Edit job':'New job';
    field('name').value=job?.name||'';
    field('description').value=job?.description||'';
    field('line').value=job?JSON.stringify(job.command.argv):'';
    field('cwd').value=job?.command.cwd||'';
    const limit=splitDuration(job?.command.timeout??DEFAULT_TIMEOUT_SECONDS,TIMEOUT_UNITS);
    field('timeout').value=limit.value;
    field('timeoutUnit').value=limit.unit;
    const plan=jobToSchedule(job);
    editingAnchor=plan?.kind==='custom'?plan.anchor:null;
    if(job)setRadio('kind',plan?'scheduled':'manual');
    setRadio('repeat',plan?.kind||'custom');
    if(plan?.kind==='custom'){field('every').value=plan.every;field('unit').value=plan.unit;}
    if(plan?.kind==='hourly')field('minute').value=plan.minute;
    if(plan?.kind==='daily')field('dailyTime').value=plan.time;
    if(plan?.kind==='weekly'){field('weekday').value=String(plan.weekday);field('weeklyTime').value=plan.time;}
    syncForm();
    form.hidden=false;
    (job?field('name'):form.querySelector('input[name="kind"]')).focus();
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if(saving)return;
    showError('');
    const chosen=kind();
    if(!chosen){showError('Choose Scheduled or Manual.');return;}
    const line=field('line').value.trim();
    let command;
    try{command=line.startsWith('[')?{argv:JSON.parse(line)}:{command:line};}
    catch(err){showError('The command starts like a JSON list but is not valid JSON: '+err.message);return;}
    const limit=Math.round(Number(field('timeout').value)*UNITS[field('timeoutUnit').value]*1000)/1000;
    if(!field('timeout').value.trim()||!(limit>0)){showError('Enter a time limit greater than zero, such as 5 minutes.');return;}
    command.timeout=limit;
    if(field('cwd').value.trim())command.cwd=field('cwd').value.trim();
    if(editing?.command.env)command.env=editing.command.env;
    let timing={every_seconds:null,first_run_at:null};
    if(chosen==='scheduled'){
      timing=scheduleToFields(scheduleChoice());
      if(timing.error){showError(timing.error);return;}
    }
    const body={name:field('name').value.trim(),description:field('description').value.trim(),command,
      every_seconds:timing.every_seconds,first_run_at:timing.first_run_at,
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
    if(!res.ok){
      showError(res.error);render();
      if(res.status===409)await refresh();
      return;
    }
    form.hidden=true;
    editing=null;editingAnchor=null;
    const saved=res.data;
    jobs=jobs.some(job=>job.id===saved.id)
      ?jobs.map(job=>job.id===saved.id?saved:job):[...jobs,saved];
    render();
    toast('Job saved');
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
    if(action==='delete'&&!confirm('Delete '+job.name+'? Its run history and logs stay on disk.'))return;
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
      if(editing?.id===id){form.hidden=true;editing=null;editingAnchor=null;}
    }
    render();
  });
  root.querySelector('#command-add').addEventListener('click',()=>edit());
  root.querySelector('#command-cancel').addEventListener('click',()=>{if(saving)return;form.hidden=true;editing=null;editingAnchor=null;showError('');});
  return {refresh};
}
