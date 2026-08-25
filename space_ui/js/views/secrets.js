/* Runtime Setup tab.

   Runtime controls are typed and restart-aware. Credentials remain write-only:
   this view receives configured status and fixed masks, never saved plaintext.
   The page intentionally separates Quirq's machine-local state from portable
   project `.xo` data. */
import {apiFetch} from '../core/api.js';
import {toast} from '../core/ui.js';

const KEY_RE=/^[A-Z_][A-Z0-9_]*$/;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));

let root=null;
let runtimeData=null;
let secretItems=[];
let runtimeForm=null;
let secretForm=null;
let keyInput=null;
let valueInput=null;
let secretSaveButton=null;
let secretCancelButton=null;
let secretError=null;
let editingKey=null;
let loading=false;

export default {
  id:'secrets',label:'Setup',order:9,
  async mount(el,ctx){
    root=el;
    switchTo=ctx.switchTo;
    renderShell();
    bindEvents();
    await loadAll();
  },
  show(){/* Preserve an in-progress credential while switching tabs. */}
};

let switchTo=()=>{}; /* ctx.switchTo, captured on mount (opens the Quirq view) */

function renderShell(){
  root.innerHTML=
    '<div class="setup-page">'
      +'<header class="setup-hero">'
        +'<div>'
          +'<div class="setup-kicker">Self-contained local runtime</div>'
          +'<h1>Runtime setup</h1>'
          +'<p>Choose the active agent, connect its native session store, tune the watcher, and provide credentials from one place.</p>'
        +'</div>'
        +'<div class="setup-hero-actions">'
          +'<button class="setup-refresh" id="setup-quirq" type="button">Open Quirq state</button>'
          +'<button class="setup-refresh" id="setup-refresh" type="button">Refresh status</button>'
        +'</div>'
      +'</header>'
      +'<div class="setup-alert" id="setup-alert">'
        +'<span aria-hidden="true">◆</span>'
        +'<div><b>Loading effective configuration…</b><p>Checking storage, runtime sources, and restart state.</p></div>'
      +'</div>'
      +'<section class="setup-overview" id="setup-overview" aria-label="Installation paths"></section>'
      +'<section class="setup-card setup-roots">'
        +'<div class="setup-card-head"><div><span>00 · Storage</span><h2>XO and Quirq roots</h2></div><i id="roots-badge">Checking</i></div>'
        +'<form id="roots-form" novalidate>'
          +'<div class="setup-root-fields">'
            +'<div>'
              +'<label for="xo-root-input">XO root</label>'
              +'<input id="xo-root-input" type="text" autocomplete="off" spellcheck="false" placeholder="/Users/you/xo-projects">'
              +'<small>Host folder containing project directories — the one root the whole app reads. Selecting a new root does not move project files.</small>'
              +'<code id="xo-root-applied">Mounted now: checking…</code>'
            +'</div>'
            +'<div>'
              +'<label for="quirq-root-input">.quirq root</label>'
              +'<input id="quirq-root-input" type="text" autocomplete="off" spellcheck="false" placeholder="/Users/you/.quirq">'
              +'<small>Machine-local state. If the new directory is empty, the installer copies the current state into it.</small>'
              +'<code id="quirq-root-applied">Mounted now: checking…</code>'
            +'</div>'
          +'</div>'
          +'<div class="setup-form-error" id="roots-error" role="alert" hidden></div>'
          +'<div class="setup-root-apply" id="roots-apply" hidden>'
            +'<div><b>Server restart required</b><p>Saved roots are read at startup: restart the server and every tab reads the new XO root. On installer-managed containers, run the one-command installer instead — it also remaps the bind mounts.</p></div>'
            +'<pre id="roots-command"></pre>'
          +'</div>'
          +'<div class="setup-actions">'
            +'<button class="setup-primary" id="roots-save" type="submit">Save roots</button>'
            +'<button class="setup-secondary" id="roots-copy" type="button" hidden>Copy apply command</button>'
          +'</div>'
        +'</form>'
      +'</section>'
      +'<div class="setup-grid">'
        +'<section class="setup-card setup-runtime">'
          +'<div class="setup-card-head"><div><span>01 · Process</span><h2>Agent and watcher</h2></div><i id="setup-applied-badge">Checking</i></div>'
          +'<form id="runtime-form" novalidate>'
            +'<label for="runtime-agent">Active agent backend</label>'
            +'<select id="runtime-agent" name="agent_name" required><option>Loading…</option></select>'
            +'<small>The active backend handles new chats. Watcher source coverage is configured separately below.</small>'
            +'<div class="setup-check-row">'
              +'<label class="setup-switch" for="runtime-watcher"><input id="runtime-watcher" type="checkbox"><span></span></label>'
              +'<div><b>Run the watcher</b><small>Continuously materialize session metadata, todos, stats, and timelines.</small></div>'
            +'</div>'
            +'<label for="runtime-source-mode">Session stores to watch</label>'
            +'<select id="runtime-source-mode">'
              +'<option value="all">All mounted runtimes</option>'
              +'<option value="active">Active agent only</option>'
            +'</select>'
            +'<small>“All” combines Claude and other supported session sources while keeping one active chat backend.</small>'
            +'<label for="runtime-interval">Watcher tick interval</label>'
            +'<div class="setup-number"><input id="runtime-interval" type="number" min=".25" max="60" step=".25" inputmode="decimal"><span>seconds</span></div>'
            +'<small>0.25–60 seconds. One second is the safe default for local use.</small>'
            +'<div class="setup-form-error" id="runtime-error" role="alert" hidden></div>'
            +'<div class="setup-actions">'
              +'<button class="setup-primary" id="runtime-save" type="submit">Save runtime</button>'
              +'<button class="setup-restart" id="runtime-restart" type="button" hidden>Apply &amp; restart</button>'
            +'</div>'
          +'</form>'
        +'</section>'
        +'<section class="setup-card setup-sources-card">'
          +'<div class="setup-card-head"><div><span>02 · Inputs</span><h2>Native session sources</h2></div><i id="source-count">—</i></div>'
          +'<div class="setup-sources" id="setup-sources"><div class="setup-empty">Inspecting mounted runtimes…</div></div>'
        +'</section>'
      +'</div>'
      +'<section class="setup-card setup-credentials">'
        +'<div class="setup-card-head"><div><span>03 · Authentication</span><h2>Credentials</h2></div><i>Write-only</i></div>'
        +'<div class="setup-secret-layout">'
          +'<form id="secret-form" novalidate>'
            +'<div class="setup-secret-title"><b id="secret-form-title">Set a credential</b><span>Saved under .quirq, never inside a project.</span></div>'
            +'<label for="secret-key">Environment key</label>'
            +'<input id="secret-key" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="ANTHROPIC_API_KEY" required>'
            +'<small>Choose a recommended key above or enter an advanced uppercase variable.</small>'
            +'<label for="secret-value">Secret value</label>'
            +'<div class="setup-value-wrap">'
              +'<input id="secret-value" type="password" autocomplete="new-password" spellcheck="false" placeholder="Paste a value" required>'
              +'<button id="secret-toggle" type="button" aria-label="Show value">Show</button>'
            +'</div>'
            +'<div class="setup-form-error" id="secret-error" role="alert" hidden></div>'
            +'<div class="setup-actions">'
              +'<button class="setup-primary" id="secret-save" type="submit">Save credential</button>'
              +'<button class="setup-secondary" id="secret-cancel" type="button" hidden>Cancel</button>'
            +'</div>'
          +'</form>'
          +'<div class="setup-secret-store">'
            +'<div class="setup-secret-store-head"><b>Configured variables</b><span id="secret-count">—</span></div>'
            +'<div id="secret-list"><div class="setup-empty">Loading credentials…</div></div>'
          +'</div>'
        +'</div>'
      +'</section>'
      +'<section class="setup-card setup-version">'
        +'<div class="setup-card-head"><div><span>04 · Version</span><h2>xo-space updates</h2></div><i id="update-badge">Not checked</i></div>'
        +'<div class="setup-version-body">'
          +'<div class="setup-version-state" id="update-state">'
            +'<div class="setup-empty">Check the git remote for a newer xo-space.</div>'
          +'</div>'
          +'<div class="setup-actions">'
            +'<button class="setup-secondary" id="update-check" type="button">Check for updates</button>'
            +'<button class="setup-primary" id="update-apply" type="button" hidden>Update now</button>'
          +'</div>'
        +'</div>'
      +'</section>'
      +'<section class="setup-boundary">'
        +'<div><span>Portable project data</span><b>&lt;project&gt;/.xo/</b><p>Session indexes, todos, timelines, stats, memory, and project identity. The watcher owns writes.</p></div>'
        +'<em>stays separate from</em>'
        +'<div><span>Machine-local runtime data</span><b id="setup-state-boundary">~/.quirq/</b><p>Runtime settings, credentials, watcher cursors, locks, and live activity snapshots.</p></div>'
      +'</section>'
    +'</div>';

  runtimeForm=root.querySelector('#runtime-form');
  secretForm=root.querySelector('#secret-form');
  keyInput=root.querySelector('#secret-key');
  valueInput=root.querySelector('#secret-value');
  secretSaveButton=root.querySelector('#secret-save');
  secretCancelButton=root.querySelector('#secret-cancel');
  secretError=root.querySelector('#secret-error');
}

function bindEvents(){
  root.querySelector('#setup-quirq').addEventListener('click',()=>switchTo('quirq'));
  root.querySelector('#setup-refresh').addEventListener('click',loadAll);
  runtimeForm.addEventListener('submit',saveRuntime);
  root.querySelector('#roots-form').addEventListener('submit',saveRoots);
  root.querySelector('#roots-copy').addEventListener('click',copyRootCommand);
  root.querySelector('#runtime-restart').addEventListener('click',restartRuntime);
  secretForm.addEventListener('submit',saveSecret);
  secretCancelButton.addEventListener('click',resetSecretForm);
  root.querySelector('#secret-toggle').addEventListener('click',toggleSecretValue);
  root.querySelector('#setup-sources').addEventListener('click',handleRecommendedSecret);
  root.querySelector('#secret-list').addEventListener('click',handleSecretListAction);
  root.querySelector('#update-check').addEventListener('click',checkForUpdate);
  root.querySelector('#update-apply').addEventListener('click',applyUpdate);
}

/* ── Self-update (04 · Version) ────────────────────────────────────────────
   Git-backed: GET /space/update/status fetches the checkout's remote and
   reports how far HEAD is behind; POST /space/update/apply fast-forwards.
   The server keeps running the old code until restarted. */
let updateStatus=null;

function renderUpdateState(html,badge){
  root.querySelector('#update-state').innerHTML=html;
  if(badge)root.querySelector('#update-badge').textContent=badge;
}

function commitLine(info){
  if(!info)return'unknown';
  return`<code>${esc(info.sha)}</code> · ${esc(info.date)} · ${esc(info.subject)}`;
}

async function checkForUpdate(){
  const button=root.querySelector('#update-check');
  setBusy(button,true);
  renderUpdateState('<div class="setup-empty">Asking the git remote…</div>','Checking');
  const res=await apiFetch('/space/update/status');
  setBusy(button,false);
  if(!res.ok){
    renderUpdateState(`<div class="setup-empty">${esc(res.error||'The version check failed.')}</div>`,'Error');
    return;
  }
  updateStatus=res.data;
  const s=updateStatus;
  const applyButton=root.querySelector('#update-apply');
  applyButton.hidden=true;
  if(!s.supported){
    renderUpdateState(`<p>${esc(s.message)}</p>`,'Unavailable');
    return;
  }
  const rows=[`<p><b>Installed</b> ${commitLine(s.current)} <span class="setup-version-branch">on ${esc(s.branch)}</span></p>`];
  if(!s.fetch_ok){
    rows.push(`<p>${esc(s.message)}</p>`);
    renderUpdateState(rows.join(''),'Offline');
    return;
  }
  if(s.up_to_date){
    rows.push('<p>This is the latest version on the remote.</p>');
    renderUpdateState(rows.join(''),'Up to date');
  }else{
    rows.push(`<p><b>Latest</b> ${commitLine(s.latest)}</p>`);
    rows.push(`<p>${s.behind} commit${s.behind===1?'':'s'} behind${s.ahead?` · ${s.ahead} local commit${s.ahead===1?'':'s'} not on the remote`:''}${s.dirty?' · local changes present':''}.</p>`);
    if(s.dirty)rows.push('<p>Updating needs a clean checkout: commit, stash, or discard the local changes first.</p>');
    else if(s.ahead)rows.push('<p>The branches diverged; self-update only fast-forwards. Reconcile manually.</p>');
    else applyButton.hidden=false;
    renderUpdateState(rows.join(''),`${s.behind} behind`);
  }
}

async function applyUpdate(){
  const applyButton=root.querySelector('#update-apply');
  setBusy(applyButton,true);
  renderUpdateState('<div class="setup-empty">Fast-forwarding the checkout…</div>','Updating');
  const res=await apiFetch('/space/update/apply',{method:'POST'});
  setBusy(applyButton,false);
  applyButton.hidden=true;
  if(!res.ok){
    renderUpdateState(`<div class="setup-empty">${esc(res.error||'The update failed.')}</div>`,'Error');
    return;
  }
  const r=res.data;
  if(!r.updated){
    renderUpdateState(`<p>${esc(r.message)}</p>`,r.reason==='up_to_date'?'Up to date':'Blocked');
    return;
  }
  toast(`Updated to ${r.to?.sha||'latest'}`);
  renderUpdateState(
    `<p><b>Updated</b> ${commitLine(r.to)} (${r.commits} commit${r.commits===1?'':'s'}).</p>`
    +`<p>${esc(r.message)}</p>`
    +(r.requirements_changed?'':'<p>Use Apply &amp; restart above (managed installs), or Ctrl-C and re-run the server, to start the new version.</p>'),
    'Restart needed'
  );
}

async function loadAll(){
  if(loading)return;
  loading=true;
  root.querySelector('#setup-refresh').disabled=true;
  const [runtimeRes,secretsRes]=await Promise.all([
    apiFetch('/api/runtime-config'),
    apiFetch('/api/secrets')
  ]);
  loading=false;
  root.querySelector('#setup-refresh').disabled=false;

  if(runtimeRes.ok){
    runtimeData=runtimeRes.data;
    renderRuntime();
  }else{
    renderRuntimeFailure(runtimeRes);
  }
  if(secretsRes.ok){
    secretItems=secretsRes.data.items||[];
    renderSecretList();
    if(runtimeData)renderSources();
  }else{
    root.querySelector('#secret-count').textContent='—';
    root.querySelector('#secret-list').innerHTML='<div class="setup-empty is-error">'+esc(secretsRes.error)+'</div>';
  }
}

function renderRuntime(){
  const configured=runtimeData.configured;
  const applied=runtimeData.applied;
  const agents=runtimeData.agents||[];
  const select=root.querySelector('#runtime-agent');
  select.innerHTML=agents.map(agent=>
    '<option value="'+esc(agent.name)+'">'+esc(prettyName(agent.name))+'</option>'
  ).join('');
  select.value=configured.agent_name;
  root.querySelector('#runtime-watcher').checked=configured.watcher_enabled;
  root.querySelector('#runtime-source-mode').value=configured.watcher_source_mode;
  root.querySelector('#runtime-interval').value=String(configured.watcher_interval_seconds);
  root.querySelector('#source-count').textContent=String(agents.length);

  const appliedBadge=root.querySelector('#setup-applied-badge');
  appliedBadge.textContent=runtimeData.restart_required?'Pending restart':'Applied';
  appliedBadge.className=runtimeData.restart_required?'is-pending':'is-good';

  const restartButton=root.querySelector('#runtime-restart');
  restartButton.hidden=!runtimeData.restart_required;
  restartButton.disabled=!runtimeData.restart_supported;
  /* a re-render after a restart lands on the same element: nothing is in
     flight any more, whatever the previous pass left on it */
  restartButton.classList.remove('is-busy');
  restartButton.textContent=runtimeData.restart_supported?'Apply & restart':'Restart from terminal';
  /* the disabled state is the instruction; say so on hover instead of
     letting a dead button look like a stuck one */
  restartButton.title=runtimeData.restart_supported?''
    :'This process is not installer-managed — restart it from the terminal where you launched it.';

  const alert=root.querySelector('#setup-alert');
  const rootPending=Boolean(runtimeData.roots?.change_required);
  if(rootPending){
    alert.className='setup-alert is-pending';
    alert.innerHTML='<span aria-hidden="true">◆</span><div><b>New storage roots are saved but not in use yet.</b>'
      +'<p>Restart the server to load them — every tab then reads the new XO root. On an installer-managed container, run the command below instead; it also remaps the bind mounts and applies any pending runtime or credential changes.</p></div>';
  }else if(runtimeData.restart_required){
    const reasons=runtimeData.restart_reasons||[];
    const runtimePending=reasons.includes('runtime');
    const secretsPending=reasons.includes('secrets');
    let detail='';
    if(runtimePending&&secretsPending){
      detail='Restart to activate '+esc(prettyName(configured.agent_name))+' and load the changed credentials.';
    }else if(secretsPending){
      detail='Restart to load the changed credentials and rerun '+esc(prettyName(configured.agent_name))+' setup.';
    }else{
      detail='Currently running '+esc(prettyName(applied.agent_name))+'. Restart to activate '
        +esc(prettyName(configured.agent_name))+' and rebuild the watcher from that source.';
    }
    alert.className='setup-alert is-pending';
    alert.innerHTML='<span aria-hidden="true">◆</span><div><b>Saved configuration is waiting for a restart.</b>'
      +'<p>'+detail+'</p></div>';
  }else{
    alert.className='setup-alert is-good';
    alert.innerHTML='<span aria-hidden="true">◆</span><div><b>Runtime configuration is applied.</b>'
      +'<p>'+esc(prettyName(applied.agent_name))+' is active; watcher '
      +(applied.watcher_enabled
        ?'ticks every '+esc(applied.watcher_interval_seconds)+' seconds across '
          +(applied.watcher_source_mode==='all'?'all mounted runtimes.':'the active runtime.')
        :'is disabled.')
      +'</p></div>';
  }

  renderOverview();
  renderRoots();
  renderSources();
}

function renderOverview(){
  const paths=runtimeData.paths||{};
  const publicUrl=runtimeData.network?.public_url||location.origin;
  const listenPort=runtimeData.network?.listen_port||'—';
  root.querySelector('#setup-overview').innerHTML=
    overviewCard('Browser address',publicUrl,'Container listens on port '+listenPort)
    +overviewCard(
      'Projects',
      paths.projects?.host_path||paths.projects?.container_path,
      pathState(paths.projects)+' · execution root '+(paths.ai_workspace?.container_path||'not set')
    )
    +overviewCard('Quirq state',paths.state?.host_path||paths.state?.container_path,pathState(paths.state));
  root.querySelector('#setup-state-boundary').textContent=paths.state?.host_path||paths.state?.container_path||'~/.quirq/';
}

function renderRoots(){
  const roots=runtimeData.roots||{};
  const configured=roots.configured||{};
  const applied=roots.applied||{};
  root.querySelector('#xo-root-input').value=configured.xo_projects_root||'';
  root.querySelector('#quirq-root-input').value=configured.quirq_state_root||'';
  root.querySelector('#xo-root-applied').textContent='Mounted now: '+(applied.xo_projects_root||'not reported');
  root.querySelector('#quirq-root-applied').textContent='Mounted now: '+(applied.quirq_state_root||'not reported');
  const badge=root.querySelector('#roots-badge');
  badge.textContent=roots.change_required?'Pending restart':'In use';
  badge.className=roots.change_required?'is-pending':'is-good';
  const apply=root.querySelector('#roots-apply');
  const copy=root.querySelector('#roots-copy');
  apply.hidden=!roots.change_required;
  copy.hidden=!roots.change_required;
  root.querySelector('#roots-command').textContent=roots.apply_command||'';
}

function overviewCard(label,value,note){
  return '<div><span>'+esc(label)+'</span><b title="'+esc(value||'Not configured')+'">'+esc(value||'Not configured')+'</b><p>'+esc(note)+'</p></div>';
}

function pathState(path){
  if(!path?.exists)return 'Missing — run the installer again to create and mount it';
  if(!path.readable)return 'Mounted but not readable';
  return path.writable?'Mounted · readable and writable':'Mounted · read-only';
}

function renderSources(){
  if(!runtimeData)return;
  const configuredKeys=new Set(secretItems.filter(item=>item.is_set).map(item=>item.key));
  const sources=runtimeData.agents||[];
  const target=root.querySelector('#setup-sources');
  if(!sources.length){
    target.innerHTML='<div class="setup-empty">No agent manifests were discovered.</div>';
    return;
  }
  target.innerHTML=sources.map(source=>{
    const selected=source.name===runtimeData.configured.agent_name;
    const secrets=(source.secrets||[]).map(item=>({...item,configured:configuredKeys.has(item.key)}));
    const secretButtons=secrets.length
      ?'<div class="source-secrets">'+secrets.map(item=>
        '<button type="button" data-secret-key="'+esc(item.key)+'" title="'+esc(item.description)+'" class="'+(item.configured?'is-set':'')+'">'
          +'<span>'+(item.configured?'✓':'＋')+'</span>'+esc(item.label)
        +'</button>'
      ).join('')+'</div>'
      :'<p class="source-note">This runtime uses its native login files rather than environment credentials.</p>';
    return '<article class="source-row '+(source.active?'is-active ':'')+(selected?'is-selected':'')+'">'
      +'<div class="source-main">'
        +'<div class="source-title"><b>'+esc(prettyName(source.name))+'</b>'
          +(source.active?'<span>Chat backend</span>':selected?'<span class="is-pending">Chat after restart</span>':'')
          +(source.watched?'<span class="is-watched">Watched</span>':'')
        +'</div>'
        +'<div class="source-facts">'
          +fact(source.home?.exists?'Mounted':'Missing',source.home?.exists?'good':'bad')
          +fact(
            source.binary_available
              ?'CLI ready'
              :source.bootstrap_available
                ?'CLI setup runs after credentials + restart'
                :'CLI unavailable',
            source.binary_available?'good':'muted'
          )
          +fact(source.session_files+' session file'+(source.session_files===1?'':'s'),source.session_files?'good':'muted')
          /* a Missing / CLI unavailable badge is a dead end without a way
             forward: link the runtime's own install docs when the manifest
             names them, only while something is actually absent */
          +((!source.home?.exists||!source.binary_available)&&source.install_url
            ?'<a class="source-install" href="'+esc(source.install_url)+'" target="_blank" rel="noopener noreferrer">'
              +'Install '+esc(prettyName(source.name))+' &#8599;</a>'
            :'')
        +'</div>'
        +'<div class="source-path"><span>Host</span><code>'+esc(source.home?.host_path||'not reported')+'</code></div>'
        +'<div class="source-path"><span>Container</span><code>'+esc(source.home?.container_path||'not reported')+'</code></div>'
        +secretButtons
      +'</div>'
    +'</article>';
  }).join('');
}

function fact(text,tone){
  return '<span class="source-fact is-'+tone+'">'+esc(text)+'</span>';
}

function renderRuntimeFailure(res){
  const alert=root.querySelector('#setup-alert');
  alert.className='setup-alert is-error';
  alert.innerHTML='<span aria-hidden="true">!</span><div><b>Runtime status is unavailable.</b><p>'+esc(res.offline?'Quirq is restarting or unreachable.':res.error)+'</p></div>';
  root.querySelector('#setup-overview').innerHTML='';
  root.querySelector('#setup-sources').innerHTML='<div class="setup-empty is-error">Could not inspect native session sources.</div>';
}

async function saveRuntime(event){
  event.preventDefault();
  clearRuntimeError();
  const button=root.querySelector('#runtime-save');
  const interval=Number(root.querySelector('#runtime-interval').value);
  if(!Number.isFinite(interval)||interval<.25||interval>60){
    showRuntimeError('Watcher interval must be between 0.25 and 60 seconds.');
    return;
  }
  setBusy(button,true);
  button.textContent='Saving…';
  const res=await apiFetch('/api/runtime-config',{
    method:'PUT',
    body:{
      agent_name:root.querySelector('#runtime-agent').value,
      watcher_enabled:root.querySelector('#runtime-watcher').checked,
      watcher_interval_seconds:interval,
      watcher_source_mode:root.querySelector('#runtime-source-mode').value
    }
  });
  setBusy(button,false);
  button.textContent='Save runtime';
  if(!res.ok){
    showRuntimeError(res.error);
    return;
  }
  runtimeData=res.data.status;
  renderRuntime();
  toast(runtimeData.restart_required?'Runtime saved — restart to apply':'Runtime configuration saved');
}

async function saveRoots(event){
  event.preventDefault();
  const error=root.querySelector('#roots-error');
  error.hidden=true;
  error.textContent='';
  const button=root.querySelector('#roots-save');
  setBusy(button,true);
  button.textContent='Saving…';
  const res=await apiFetch('/api/runtime-config/roots',{
    method:'PUT',
    body:{
      xo_projects_root:root.querySelector('#xo-root-input').value.trim(),
      quirq_state_root:root.querySelector('#quirq-root-input').value.trim()
    }
  });
  setBusy(button,false);
  button.textContent='Save roots';
  if(!res.ok){
    error.textContent=res.error||'The roots could not be saved.';
    error.hidden=false;
    return;
  }
  runtimeData=res.data.status;
  renderRuntime();
  toast(runtimeData.roots?.change_required?'Roots saved — restart to apply':'Roots already match the folders in use');
}

async function copyRootCommand(){
  const command=runtimeData?.roots?.apply_command||'';
  if(!command)return;
  try{
    await navigator.clipboard.writeText(command);
    toast('Installer command copied');
  }catch(_error){
    const range=document.createRange();
    range.selectNodeContents(root.querySelector('#roots-command'));
    const selection=getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast('Select and copy the highlighted command');
  }
}

async function restartRuntime(){
  if(!runtimeData?.restart_supported){
    showRuntimeError('Run the installer command again to restart this non-managed process.');
    return;
  }
  if(!confirm('Restart Quirq now? The page will reconnect automatically.'))return;
  const button=root.querySelector('#runtime-restart');
  setBusy(button,true);
  button.textContent='Restarting…';
  const res=await apiFetch('/api/runtime-config/restart',{method:'POST'});
  if(!res.ok){
    setBusy(button,false);
    button.textContent='Apply & restart';
    showRuntimeError(res.error);
    return;
  }
  const alert=root.querySelector('#setup-alert');
  alert.className='setup-alert is-pending';
  alert.innerHTML='<span aria-hidden="true">◆</span><div><b>Quirq is restarting…</b><p>Waiting for the container to become healthy with the saved runtime.</p></div>';
  for(let attempt=0;attempt<60;attempt+=1){
    await delay(1000);
    const probe=await apiFetch('/health?setup_restart_probe='+attempt);
    if(probe.ok){
      toast('Runtime restarted');
      await loadAll();
      return;
    }
  }
  setBusy(button,false);
  button.textContent='Retry restart';
  showRuntimeError('The restart is taking longer than expected. Refresh status after the container becomes healthy.');
}

function handleRecommendedSecret(event){
  const button=event.target.closest('button[data-secret-key]');
  if(!button)return;
  beginSecret(button.dataset.secretKey);
  root.querySelector('.setup-credentials').scrollIntoView({behavior:'smooth',block:'start'});
}

function renderSecretList(){
  root.querySelector('#secret-count').textContent=String(secretItems.length);
  const list=root.querySelector('#secret-list');
  if(!secretItems.length){
    list.innerHTML='<div class="setup-empty"><b>No credentials configured.</b><span>Select a recommended key from a runtime source.</span></div>';
    return;
  }
  list.innerHTML=secretItems.map(item=>
    '<div class="setup-secret-row" data-secret-key="'+esc(item.key)+'">'
      +'<div><b>'+esc(item.key)+'</b><span>'+(item.is_set?'Configured':'Empty')+'</span></div>'
      +'<code>'+(item.is_set?'••••••':'not set')+'</code>'
      +'<button type="button" data-action="replace">Replace</button>'
      +'<button type="button" data-action="delete" class="is-danger">Remove</button>'
    +'</div>'
  ).join('');
}

function handleSecretListAction(event){
  const button=event.target.closest('button[data-action]');
  const row=button?.closest('[data-secret-key]');
  const key=row?.dataset.secretKey;
  if(!button||!key)return;
  if(button.dataset.action==='replace')beginSecret(key);
  if(button.dataset.action==='delete')removeSecret(key,button);
}

function beginSecret(key){
  editingKey=key;
  keyInput.value=key;
  keyInput.readOnly=true;
  valueInput.value='';
  valueInput.type='password';
  root.querySelector('#secret-toggle').textContent='Show';
  root.querySelector('#secret-form-title').textContent=secretItems.some(item=>item.key===key)?'Replace credential':'Set credential';
  secretSaveButton.textContent=secretItems.some(item=>item.key===key)?'Replace value':'Save credential';
  secretCancelButton.hidden=false;
  clearSecretError();
  valueInput.focus();
}

function resetSecretForm(){
  editingKey=null;
  secretForm.reset();
  keyInput.readOnly=false;
  valueInput.type='password';
  root.querySelector('#secret-toggle').textContent='Show';
  root.querySelector('#secret-toggle').setAttribute('aria-label','Show value');
  root.querySelector('#secret-form-title').textContent='Set a credential';
  secretSaveButton.textContent='Save credential';
  secretCancelButton.hidden=true;
  clearSecretError();
}

function toggleSecretValue(event){
  const showing=valueInput.type==='text';
  valueInput.type=showing?'password':'text';
  event.currentTarget.textContent=showing?'Show':'Hide';
  event.currentTarget.setAttribute('aria-label',showing?'Show value':'Hide value');
  valueInput.focus();
}

async function saveSecret(event){
  event.preventDefault();
  clearSecretError();
  const key=(editingKey||keyInput.value).trim();
  const value=valueInput.value;
  if(!KEY_RE.test(key)){
    showSecretError('Key must use uppercase letters, numbers, and underscores.');
    keyInput.focus();
    return;
  }
  if(!value){
    showSecretError('Enter a value, or remove the configured variable.');
    valueInput.focus();
    return;
  }
  setSecretBusy(true);
  const res=await apiFetch('/api/secrets/'+encodeURIComponent(key),{method:'PATCH',body:{value}});
  valueInput.value='';
  setSecretBusy(false);
  if(!res.ok){
    showSecretError(res.error);
    return;
  }
  resetSecretForm();
  toast('Credential saved');
  await loadAll();
}

async function removeSecret(key,button){
  if(!confirm('Remove '+key+'? The saved value cannot be recovered.'))return;
  setBusy(button,true);
  const res=await apiFetch('/api/secrets/'+encodeURIComponent(key),{method:'DELETE'});
  if(!res.ok){
    setBusy(button,false);
    showSecretError(res.error);
    return;
  }
  if(editingKey===key)resetSecretForm();
  toast(res.data.deleted?'Credential removed':'Credential was already absent');
  await loadAll();
}

function setSecretBusy(busy){
  setBusy(secretSaveButton,busy);
  setBusy(secretCancelButton,busy);
  keyInput.disabled=busy;
  valueInput.disabled=busy;
  secretSaveButton.textContent=busy?'Saving…':(editingKey?'Replace value':'Save credential');
}

/* Disabled is not busy. A button that cannot act here (the restart on a
   process the installer does not manage) and one that is mid-request look
   identical to :disabled, but only the second has anything to wait for —
   the first is an instruction. Every in-flight path goes through here so
   the wait cursor means exactly one thing. */
function setBusy(button,busy){
  button.disabled=busy;
  button.classList.toggle('is-busy',busy);
}

function showRuntimeError(message){
  const el=root.querySelector('#runtime-error');
  el.textContent=message||'The runtime configuration could not be saved.';
  el.hidden=false;
}

function clearRuntimeError(){
  const el=root.querySelector('#runtime-error');
  el.textContent='';
  el.hidden=true;
}

function showSecretError(message){
  secretError.textContent=message||'The credential could not be saved.';
  secretError.hidden=false;
}

function clearSecretError(){
  secretError.textContent='';
  secretError.hidden=true;
}

function prettyName(value){
  return String(value||'').split('_').map(part=>part?part[0].toUpperCase()+part.slice(1):'').join(' ');
}
