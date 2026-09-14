/* Setup's Commands panel uses the scheduler's definitions, executor and history.
   No interval means manual only; nothing is seeded or executed on mount. */
import {apiFetch,API_BASE} from '../core/api.js';
import {toast} from '../core/ui.js';
import {openCommandResults} from '../core/command-results.js?v=20260914-results1';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const path=id=>'/api/schedules/'+encodeURIComponent(id);
const duration=value=>value==null||!Number.isFinite(Number(value))?'—':Number(value).toFixed(2)+'s';
const createEndpoint=()=>new URL(API_BASE+'/api/schedules',location.href).href;

function agentPrompt(){
  // Deliberately omit the page query string: it can contain proxy credentials.
  const endpoint=createEndpoint();
  return `Add the commands I describe to Saved commands in XO Space using this API.

Create job: POST ${endpoint}
Content-Type: application/json

This is the same endpoint used by Add command → Save command. Run API calls on the machine hosting Space; writes require a local client. If this URL is remote, use the server's configured loopback address and port. If it is a read-only preview, report that and use the running Space API instead.

First GET ${endpoint} and check for an existing job. Adapt this complete request to the command I want:

${'```sh'}
curl --fail-with-body --silent --show-error --request POST '${endpoint}' \\
  --header 'Content-Type: application/json' \\
  --data-binary @- <<'JSON'
{
  "name": "Check repository",
  "description": "Show local Git changes",
  "command": {"argv": ["git", "status", "--short"], "timeout": 30},
  "every_seconds": null,
  "enabled": true
}
JSON
${'```'}

A successful create returns HTTP 201 and the saved job, including its id. Without command.cwd, execution uses the Space server's working directory. Set command.cwd to the intended absolute project directory when needed. Commands use argv without a shell; shell operators are not interpreted. Keep credentials out of arguments and descriptions.

Keep every_seconds null for manual runs unless I ask for a schedule. For an interval, use an integer in seconds at least as long as the configured watcher tick. Automatic runs require the watcher and scheduler to be enabled.

Verify the saved command with GET ${endpoint}/{id} and report its name and ID. Do not execute it unless I ask. I can click Run in Setup → Commands and open its Inbox for results.

If I request a run: POST ${endpoint}/{id}/run. Check GET ${endpoint}/{id} for completion and GET ${endpoint}/{id}/runs for results and the exact log_path. Default logs: ~/.quirq/logs/scheduler/{id}.log; history: ~/.quirq/scheduler/runs/{id}.jsonl. Use the API rather than editing scheduler files directly.`;
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
        <span id="command-help-tip" role="tooltip" hidden>Copy a complete curl request for POST /api/schedules and paste it into your agent to add commands here.</span></span></div>
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
        </div>
        <small>Leave blank for manual runs. Intervals run automatically through the watcher.</small>
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
            <span>${job.every_seconds==null?'Manual only':'Runs every '+esc(job.every_seconds)+'s'+(job.enabled?'':' · disabled')}</span></div>
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
