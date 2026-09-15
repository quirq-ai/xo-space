/* Setup's Jobs panel uses the scheduler's definitions, executor and history.
   A Manual job has no interval; a Scheduled job's plain-language schedule is
   translated to every_seconds/first_run_at by core/jobs.js. The editor is its
   own card above the list, and shows what sets the chosen kind apart first.
   Nothing is seeded or executed on mount. */
import {apiFetch,API_BASE} from '../core/api.js';
import {toast} from '../core/ui.js';
import {openCommandResults} from '../core/command-results.js?v=20260914-results1';
import {UNITS,WEEKDAYS,clockTime,describeChoice,describeSchedule,durationText,isScheduled,jobToSchedule,
  runsPerDay,scheduleToFields,splitDuration,statusText,upcomingRuns,utcOffset} from '../core/jobs.js?v=20260916-jobs2';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const path=id=>'/api/schedules/'+encodeURIComponent(id);
const duration=value=>value==null||!Number.isFinite(Number(value))?'—':Number(value).toFixed(2)+'s';
const createEndpoint=()=>new URL(API_BASE+'/api/schedules',location.href).href;
/* A new job may run for five minutes before it is stopped. */
const DEFAULT_TIMEOUT_SECONDS=300;
const TIMEOUT_UNITS=['hours','minutes','seconds'];

const icon=paths=>`<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const ICONS={
  custom:icon('<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>'),
  hourly:icon('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  daily:icon('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  weekly:icon('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>'),
  manual:icon('<circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l6-3.5z"/>'),
};
const PRESETS=[['custom','Every…','Minutes, hours or days'],['hourly','Hourly','At a minute past each hour'],
  ['daily','Daily','At a time each day'],['weekly','Weekly','On one day each week']];

const localTime=value=>{
  const d=new Date(value);
  return Number.isNaN(d.getTime())?String(value):d.toLocaleString([],{dateStyle:'medium',timeStyle:'short'});
};
const shortTime=d=>d.toLocaleString([],{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});

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
    <section class="setup-card setup-job-editor" id="command-editor" aria-labelledby="command-form-title" hidden>
      <div class="setup-card-head"><h3 id="command-form-title">New job</h3>
        <button type="button" class="setup-job-close" id="command-close" aria-label="Close without saving">✕</button></div>
      <form id="command-form" class="setup-command-form" novalidate>
        <fieldset class="setup-job-kind">
          <legend>What kind of job is this?</legend>
          <label class="setup-job-kind-option"><input type="radio" name="kind" value="scheduled"><span><b>Scheduled</b><small>Runs on its own, on a schedule you choose.</small></span></label>
          <label class="setup-job-kind-option"><input type="radio" name="kind" value="manual"><span><b>Manual</b><small>Saved for later. Runs only when you click Run now.</small></span></label>
        </fieldset>
        <fieldset id="command-schedule" class="setup-job-section" hidden>
          <legend class="setup-job-heading">When should it run?</legend>
          <div class="setup-job-presets">${PRESETS.map(([value,label,hint])=>`<label class="setup-job-preset"><input type="radio" name="repeat" value="${value}"><span class="setup-job-icon">${ICONS[value]}</span><span><b>${label}</b><small>${hint}</small></span></label>`).join('')}</div>
          <div class="setup-job-detail" data-repeat="custom"><span>Every</span><input name="every" type="number" min="1" step="1" inputmode="numeric" value="30" aria-label="Repeat every"><select name="unit" aria-label="Repeat unit"><option value="minutes">minutes</option><option value="hours">hours</option><option value="days">days</option><option value="seconds">seconds</option></select></div>
          <div class="setup-job-detail" data-repeat="hourly"><span>At minute</span><input name="minute" type="number" min="0" max="59" step="1" inputmode="numeric" value="0" aria-label="Minute past the hour"><span>past each hour</span></div>
          <div class="setup-job-detail" data-repeat="daily"><span>At</span><input name="dailyTime" type="time" value="09:00" aria-label="Time of day"></div>
          <div class="setup-job-detail" data-repeat="weekly"><span>On</span><select name="weekday" aria-label="Day of the week">${WEEKDAYS.map((day,index)=>`<option value="${index}">${day}</option>`).join('')}</select><span>at</span><input name="weeklyTime" type="time" value="09:00" aria-label="Time on that day"><small>or pick a day below</small></div>
          <div class="setup-job-week" id="command-week" role="group" aria-label="Runs over the next 7 days"></div>
          <p id="command-schedule-preview" class="setup-job-preview" aria-live="polite"></p>
          <p id="command-next-runs" class="setup-job-next"></p>
          <small>Times are in your time zone (UTC${utcOffset()}). A job keeps a fixed interval, so a daily time can move by an hour when daylight saving starts or ends. Scheduled jobs run only while <b>Update activity automatically</b> is on in Intelligence layer.</small>
        </fieldset>
        <div id="command-manual" class="setup-job-manual" hidden>${ICONS.manual}<div><b>No schedule</b>
          <p>It waits for you. Click <b>Run now</b> on this job, here or in Inbox → Jobs, whenever you want it to run.</p></div></div>
        <div id="command-fields" class="setup-job-fields" hidden>
          <h4 class="setup-job-heading">What should it run?</h4>
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
          <label for="command-timeout">Stop it if a run takes longer than</label>
          <div class="setup-job-inline"><input id="command-timeout" name="timeout" type="number" min="0" step="any" inputmode="decimal"><select name="timeoutUnit" aria-label="Time limit unit"><option value="seconds">seconds</option><option value="minutes">minutes</option><option value="hours">hours</option></select></div>
          <small>A run still going at that point is stopped and marked Timed out.</small>
        </div>
        <div class="setup-form-error" id="command-error" role="alert" hidden></div>
        <div class="setup-actions">
          <button class="setup-primary" id="command-save" type="submit">Save job</button>
          <button class="setup-secondary" id="command-cancel" type="button">Cancel</button>
        </div>
      </form>
    </section>
    <section class="setup-card" aria-labelledby="command-list-title">
      <div class="setup-card-head setup-command-head"><div class="setup-command-heading"><h3 id="command-list-title">Your jobs</h3>
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
        <div class="setup-form-error" id="command-list-error" role="alert" hidden></div>
        <div id="command-list"><div class="setup-empty">Loading jobs…</div></div>
      </div>
    </section>
`;
  const editor=root.querySelector('#command-editor');
  const form=root.querySelector('#command-form');
  const error=root.querySelector('#command-error');
  const listError=root.querySelector('#command-list-error');
  const list=root.querySelector('#command-list');
  const fields=root.querySelector('#command-fields');
  const schedule=root.querySelector('#command-schedule');
  const manual=root.querySelector('#command-manual');
  const details=[...root.querySelectorAll('.setup-job-detail')];
  const week=root.querySelector('#command-week');
  const preview=root.querySelector('#command-schedule-preview');
  const nextRuns=root.querySelector('#command-next-runs');
  const field=name=>form.elements.namedItem(name);
  const kind=()=>field('kind').value;
  const setRadio=(name,value)=>{const input=form.querySelector(`input[name="${name}"][value="${value}"]`);if(input)input.checked=true;};
  /* Form problems show in the editor card; list and run problems in the list card. */
  function showError(message){error.textContent=message||'';error.hidden=!message;}
  function showListError(message){listError.textContent=message||'';listError.hidden=!message;}

  function scheduleChoice(){
    const repeat=field('repeat').value;
    return {kind:repeat,every:field('every').value,unit:field('unit').value,minute:field('minute').value,
      weekday:field('weekday').value,time:field(repeat==='weekly'?'weeklyTime':'dailyTime').value,anchor:editingAnchor};
  }
  /* The preview says in words what will be saved, and the week strip shows
     where the runs land, before anything is sent. On Weekly the strip's days
     are buttons that pick the day. */
  function syncPreview(){
    const clear=()=>{nextRuns.textContent='';week.innerHTML='';};
    if(kind()!=='scheduled'){preview.textContent='';clear();return;}
    const choice=scheduleChoice(),out=scheduleToFields(choice);
    preview.classList.toggle('is-error',Boolean(out.error));
    if(out.error){preview.textContent=out.error;clear();return;}
    const now=new Date(),unanchored=choice.kind==='custom'&&!choice.anchor;
    const first=choice.kind==='custom'
      ?(choice.anchor?'It keeps its current run times.':'First run '+durationText(choice.every,choice.unit)+' after you save.')
      :'First run: '+localTime(out.first_run_at)+'.';
    preview.textContent='→ '+describeChoice(choice)+'. '+first;
    nextRuns.textContent='Next runs'+(unanchored?' if saved now':'')+': '+upcomingRuns(out,now,3).map(shortTime).join(' · ');
    const picking=choice.kind==='weekly';
    const focused=week.contains(document.activeElement)?document.activeElement.dataset.weekday:null;
    week.innerHTML=runsPerDay(out,now,7).map((day,index)=>{
      const weekday=day.date.getDay();
      const detail=day.count===0?'—':day.count===1?clockTime(day.first):day.count+' runs';
      const inner=`<span>${index===0?'Today':WEEKDAYS[weekday].slice(0,3)}</span><b>${day.date.getDate()}</b><small>${detail}</small>`;
      const cls='setup-job-day'+(day.count?' has-runs':'');
      return picking
        ?`<button type="button" class="${cls}" data-weekday="${weekday}" aria-pressed="${String(weekday)===choice.weekday}" aria-label="Run on ${WEEKDAYS[weekday]}s">${inner}</button>`
        :`<div class="${cls}">${inner}</div>`;
    }).join('');
    if(focused!=null)week.querySelector(`[data-weekday="${focused}"]`)?.focus();
  }
  /* Nothing but the kind choice shows until a kind is picked; then the part
     that differs (schedule, or the no-schedule note) comes first. */
  function syncForm(){
    const chosen=kind();
    fields.hidden=!chosen;
    schedule.hidden=chosen!=='scheduled';
    manual.hidden=chosen!=='manual';
    const repeat=field('repeat').value;
    for(const detail of details)detail.hidden=detail.dataset.repeat!==repeat;
    syncPreview();
  }
  form.addEventListener('change',syncForm);
  form.addEventListener('input',syncPreview);
  week.addEventListener('click',event=>{
    const day=event.target.closest('[data-weekday]');
    if(!day||saving)return;
    field('weekday').value=day.dataset.weekday;
    syncPreview();
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
      else showListError(res.error);
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
      if(!res.ok)showListError(res.error);
      else{jobs=res.data.jobs||[];render();}
    }
    if(refreshQueued){refreshQueued=false;await refresh();}
    else schedulePoll();
  }

  function openEditor(job=null){
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
    editor.hidden=false;
    editor.scrollIntoView({block:'start'});
    (job?field('name'):form.querySelector('input[name="kind"]')).focus({preventScroll:true});
  }
  function closeEditor(){
    editor.hidden=true;editing=null;editingAnchor=null;showError('');
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
    const controls=[...form.elements,root.querySelector('#command-add'),root.querySelector('#command-close')];
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
    closeEditor();
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
    if(action==='edit'){openEditor(job);return;}
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
      showListError(res.error);
      toast(res.error);
      render();
      if(res.status===409)await refresh();
      return;
    }
    showListError('');
    if(action==='run')jobs=jobs.map(item=>item.id===id?res.data.job:item);
    else{
      jobs=jobs.filter(item=>item.id!==id);
      if(editing?.id===id)closeEditor();
    }
    render();
  });
  root.querySelector('#command-add').addEventListener('click',()=>openEditor());
  for(const id of ['#command-cancel','#command-close'])root.querySelector(id).addEventListener('click',()=>{if(!saving)closeEditor();});
  return {refresh};
}
