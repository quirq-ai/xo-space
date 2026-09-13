/* Setup's Commands card uses the scheduler's definitions, executor and history.
   No interval means manual only; nothing is seeded or executed on mount. */
import {apiFetch} from '../core/api.js';
import {toast} from '../core/ui.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const path=id=>'/api/schedules/'+encodeURIComponent(id);
const duration=value=>value==null?'—':Number(value).toFixed(2)+'s';
function relativeTime(value){
  const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(value))/1000));
  if(!Number.isFinite(seconds))return 'unknown time';
  if(seconds<60)return seconds+'s ago';
  if(seconds<3600)return Math.floor(seconds/60)+'m ago';
  if(seconds<86400)return Math.floor(seconds/3600)+'h ago';
  return Math.floor(seconds/86400)+'d ago';
}

export function mountCommands(root){
  let jobs=[],editing=null,timer=null,refreshing=false,historyId=null;
  const busy=new Set();
  root.innerHTML=`
    <div class="setup-card-head"><div><span>05 · Local execution</span><h2>Commands</h2></div>
      <button type="button" class="setup-secondary" id="command-add">Add command</button></div>
    <div class="setup-command-body">
      <div class="setup-form-error" id="command-error" role="alert" hidden></div>
      <form id="command-form" class="setup-command-form" novalidate hidden>
        <h3 id="command-form-title">Add command</h3>
        <label for="command-name">Name</label>
        <input id="command-name" name="name" autocomplete="off" placeholder="Check checkout status">
        <label for="command-description">Description</label>
        <input id="command-description" name="description" autocomplete="off" placeholder="What this command does">
        <label for="command-line">Command line or argv JSON</label>
        <textarea id="command-line" name="line" rows="2" spellcheck="false" placeholder="git -C &lt;checkout&gt; status --short"></textarea>
        <small>Arguments run without a shell. For explicit arguments use a JSON array, such as ["git", "status", "--short"].</small>
        <label for="command-cwd">Working directory (optional)</label>
        <input id="command-cwd" name="cwd" spellcheck="false" placeholder="Server working directory">
        <div class="setup-command-numbers">
          <div><label for="command-timeout">Timeout (seconds)</label><input id="command-timeout" name="timeout" type="number" step="any" value="30"></div>
          <div><label for="command-interval">Interval (seconds, optional)</label><input id="command-interval" name="interval" type="number" placeholder="Manual only"></div>
        </div>
        <small>Leave the interval empty to run only when you click Run. Intervals use the watcher and the same concurrency limit.</small>
        <div class="setup-actions">
          <button class="setup-primary" id="command-save" type="submit">Save command</button>
          <button class="setup-secondary" id="command-cancel" type="button">Cancel</button>
        </div>
      </form>
      <div id="command-list"><div class="setup-empty">Loading commands…</div></div>
    </div>
    <dialog class="setup-runs-drawer" id="command-runs" aria-labelledby="command-runs-title">
      <div class="setup-card-head"><h2 id="command-runs-title">Runs</h2><button type="button" class="setup-secondary" id="command-runs-close">Close</button></div>
      <div class="setup-command-body" id="command-runs-body"></div>
    </dialog>`;
  const form=root.querySelector('#command-form');
  const error=root.querySelector('#command-error');
  const list=root.querySelector('#command-list');
  const drawer=root.querySelector('#command-runs');
  const field=name=>form.elements.namedItem(name);
  function showError(message){error.textContent=message||'';error.hidden=!message;}

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
          <div class="setup-command-meta"><span class="setup-command-result ${running?'is-running':result?.status==='ok'?'is-good':result?'is-error':''}" role="status">${esc(status)}</span>
            <span>${job.every_seconds==null?'Manual only':'Runs every '+esc(job.every_seconds)+'s'+(job.enabled?'':' · disabled')}</span></div>
        </div>
        <div class="setup-actions">
          <button class="setup-primary" type="button" data-command-action="run"${running?' disabled':disabled}>${running?'Running…':'Run'}</button>
          <button class="setup-secondary" type="button" data-command-action="runs"${disabled}>Runs</button>
          <button class="setup-secondary" type="button" data-command-action="edit"${disabled}>Edit</button>
          <button class="setup-secondary is-danger" type="button" data-command-action="delete"${disabled}>Delete</button>
        </div></article>`;
    }).join(''):'<div class="setup-empty"><b>No commands yet</b><span>Add a command to run it here and keep every result.</span></div>';
    schedulePoll();
  }

  function schedulePoll(){
    clearTimeout(timer);
    if(jobs.some(job=>job.running))timer=setTimeout(pollRunning,3000);
  }
  async function pollRunning(){
    const active=jobs.filter(job=>job.running&&!busy.has(job.id));
    const results=await Promise.all(active.map(async job=>({id:job.id,res:await apiFetch(path(job.id))})));
    for(const {id,res} of results){
      if(res.ok)jobs=jobs.map(job=>job.id===id?res.data:job);
      else if(res.status===404)jobs=jobs.filter(job=>job.id!==id);
      else showError(res.error);
    }
    render();
    if(drawer.open&&results.some(({id})=>id===historyId))await loadRuns(historyId);
  }
  async function refresh(){
    if(refreshing)return;
    refreshing=true;
    const res=await apiFetch('/api/schedules');
    refreshing=false;
    if(!res.ok){showError(res.error);return;}
    jobs=res.data.jobs||[];
    render();
  }

  function edit(job=null){
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
    controls.forEach(el=>el.disabled=true);
    const res=await apiFetch(editing?path(editing.id):'/api/schedules',{method:editing?'PUT':'POST',body});
    controls.forEach(el=>el.disabled=false);
    if(!res.ok){showError(res.error);return;}
    form.hidden=true;
    editing=null;
    toast('Command saved');
    await refresh();
  });

  async function loadRuns(id){
    const res=await apiFetch(path(id)+'/runs?limit=20');
    if(historyId!==id||!drawer.open)return;
    const body=root.querySelector('#command-runs-body');
    if(!res.ok){body.textContent=res.error;return;}
    body.innerHTML=`<p>Full output on this machine: <code class="setup-command-log">${esc(res.data.log_path)}</code></p>`
      +(res.data.runs.length?res.data.runs.map(run=>`<article class="setup-run">
        <b>${esc(run.status)} · ${esc(duration(run.duration_seconds))}</b>
        <p>${esc(run.started_at)} → ${esc(run.finished_at)}</p>
        <p>${esc(run.trigger||'interrupted')} · exit ${esc(run.returncode??'—')}</p>
        ${run.reason?`<p>${esc(run.reason)}</p>`:''}
        <pre>${esc(run.output_tail||'(no output)')}</pre></article>`).join(''):'<div class="setup-empty">No runs yet</div>');
  }
  list.addEventListener('click',async event=>{
    const button=event.target.closest('[data-command-action]');
    const id=button?.closest('[data-command-id]')?.dataset.commandId;
    const job=jobs.find(item=>item.id===id);
    if(!job||busy.has(id))return;
    const action=button.dataset.commandAction;
    if(action==='edit'){edit(job);return;}
    if(action==='runs'){
      historyId=id;
      root.querySelector('#command-runs-title').textContent=job.name+' · latest 20 runs';
      root.querySelector('#command-runs-body').textContent='Loading runs…';
      drawer.showModal();
      await loadRuns(id);
      return;
    }
    if(action==='delete'&&!confirm('Delete '+job.name+'? Saved run history and logs will be kept on disk.'))return;
    busy.add(id);
    render();
    const res=await apiFetch(path(id)+(action==='run'?'/run':''),{method:action==='run'?'POST':'DELETE'});
    busy.delete(id);
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
  root.querySelector('#command-cancel').addEventListener('click',()=>{form.hidden=true;editing=null;showError('');});
  root.querySelector('#command-runs-close').addEventListener('click',()=>drawer.close());
  drawer.addEventListener('close',()=>{historyId=null;});
  return {refresh};
}
