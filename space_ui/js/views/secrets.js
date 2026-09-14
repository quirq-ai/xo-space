/* Runtime Setup tab.

   Runtime controls are typed and restart-aware. Credentials remain write-only:
   this view receives configured status and fixed masks, never saved plaintext.
   The page intentionally separates Quirq's machine-local state from portable
   project `.xo` data. */
import {apiFetch} from '../core/api.js';
import {toast} from '../core/ui.js';
import {pollServer} from '../core/server-widget.js?v=20260914-commands2';
import {mountCommands} from './setup-commands.js?v=20260914-setupflow1';
import {setupSteps} from '../core/setup-state.js?v=20260914-setupflow1';

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
let commands=null;
let serverData=null;
let restarting=false;
let currentPanel='workspace';
let runtimeUnavailable=false;
const drafts={workspace:false,agent:false,activity:false};
const touched=new Set();
const writes=new Set();
let runtimeRevision=0,refreshQueued=false;

export default {
  id:'secrets',label:'Setup',order:9,
  async mount(el,ctx){
    root=el;
    switchTo=ctx.switchTo;
    renderShell();
    bindEvents();
    commands=mountCommands(root.querySelector('#setup-commands'));
    await loadAll();
  },
  show(){commands?.refresh(); /* Preserve in-progress forms while switching tabs. */}
};

let switchTo=()=>{}; /* ctx.switchTo, captured on mount (opens the Quirq view) */

function renderShell(){
  root.innerHTML=`<div class="setup-page">
    <header class="setup-hero">
      <h1>Setup</h1>
      <button class="setup-refresh" id="setup-refresh" type="button">Refresh status</button>
    </header>
    <div class="setup-alert" id="setup-alert" role="status"><div><b>Checking settings…</b></div></div>
    <div class="setup-form-error" id="setup-restart-error" role="alert" hidden></div>
    <div class="setup-layout">
      <nav class="setup-nav" id="setup-nav" aria-label="Setup sections">
        <p>Set up</p>
        ${[['workspace','1','Workspace'],['agent','2','Agent & access'],['activity','3','Activity']].map(([id,n,label])=>`
          <button type="button" data-setup-go="${id}" aria-controls="setup-panel-${id}"${id==='workspace'?' aria-current="step"':''}>
            <span class="setup-nav-icon">${n}</span><span><b>${label}</b><small id="setup-step-${id}">Checking…</small></span>
          </button>`).join('')}
        <p>Manage</p>
        <button type="button" data-setup-go="commands" aria-controls="setup-panel-commands"><span class="setup-nav-icon" aria-hidden="true">›</span><span><b>Commands</b><small>Run and view results</small></span></button>
        <button type="button" data-setup-go="server" aria-controls="setup-panel-server"><span class="setup-nav-icon" aria-hidden="true">›</span><span><b>Server</b><small>Updates and restart</small></span></button>
      </nav>
      <div class="setup-content">
        <section class="setup-panel" id="setup-panel-workspace" aria-labelledby="setup-workspace-title">
          <header class="setup-section-head"><h2 id="setup-workspace-title" tabindex="-1">Workspace</h2><p>Choose your project folder and where Space keeps its settings.</p></header>
          <section class="setup-card setup-roots">
            <div class="setup-card-head"><h3>Folders</h3><i id="roots-badge">Checking</i></div>
            <form id="roots-form" novalidate>
              <div class="setup-root-fields">
                <div><label for="xo-root-input">Projects folder</label><input id="xo-root-input" type="text" autocomplete="off" spellcheck="false" placeholder="/Users/you/xo-projects">
                  <small>Contains your project folders. Changing this does not move them.</small><code id="xo-root-applied"></code></div>
                <div><label for="quirq-root-input">Space data folder</label><input id="quirq-root-input" type="text" autocomplete="off" spellcheck="false" placeholder="/Users/you/.quirq">
                  <small>Settings, credentials and activity data. Default: ~/.quirq/</small><code id="quirq-root-applied"></code></div>
              </div>
              <div class="setup-form-error" id="roots-error" role="alert" hidden></div>
              <div class="setup-root-apply" id="roots-apply" hidden>
                <div><b>Apply folder changes</b><p>Restart to use the saved folders. For an installer-managed container, run this command to update its mounts.</p></div>
                <pre id="roots-command"></pre>
              </div>
              <div class="setup-actions"><button class="setup-primary" id="roots-save" type="submit">Save folders</button><button class="setup-secondary" id="roots-copy" type="button" hidden>Copy installer command</button></div>
            </form>
          </section>
          <details class="setup-details"><summary>Folder and connection details</summary><section class="setup-overview" id="setup-overview" aria-label="Installation paths"></section></details>
          <footer class="setup-step-footer"><button class="setup-secondary" type="button" data-setup-go="agent">Next: Agent &amp; access →</button></footer>
        </section>

        <section class="setup-panel" id="setup-panel-agent" aria-labelledby="setup-agent-title" hidden>
          <header class="setup-section-head"><h2 id="setup-agent-title" tabindex="-1">Agent &amp; access</h2><p>Choose the agent for new chats and add credentials if needed.</p></header>
          <section class="setup-card setup-runtime">
            <form id="runtime-form" novalidate>
              <div class="setup-agent-label"><label for="runtime-agent">Agent for new chats</label><i id="setup-applied-badge">Checking</i></div><select id="runtime-agent" name="agent_name" required><option>Loading…</option></select>
              <div class="setup-form-error" id="runtime-error" role="alert" hidden></div>
              <div class="setup-actions"><button class="setup-primary" id="runtime-save" type="submit">Save agent</button></div>
            </form>
            <div class="setup-sources" id="setup-sources"><div class="setup-empty">Checking agents…</div></div>
          </section>
          <section class="setup-card setup-credentials">
            <div class="setup-card-head"><h3>Keys and credentials <span id="secret-count">—</span></h3><button class="setup-secondary" id="secret-add" type="button">Add credential</button></div>
            <div id="secret-list"><div class="setup-empty">Loading credentials…</div></div>
            <div class="setup-form-error" id="secret-error" role="alert" hidden></div>
            <form id="secret-form" novalidate hidden>
              <h4 id="secret-form-title">Add credential</h4>
              <label for="secret-key">Key</label><input id="secret-key" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="ANTHROPIC_API_KEY" required>
              <label for="secret-value">Value</label><div class="setup-value-wrap"><input id="secret-value" type="password" autocomplete="new-password" spellcheck="false" placeholder="Paste a value" required><button id="secret-toggle" type="button" aria-label="Show value">Show</button></div>
              <small>Saved values stay hidden. Restart to apply changes.</small>
              <div class="setup-actions"><button class="setup-primary" id="secret-save" type="submit">Save credential</button><button class="setup-secondary" id="secret-cancel" type="button">Cancel</button></div>
            </form>
          </section>
          <footer class="setup-step-footer"><button class="setup-secondary" type="button" data-setup-go="activity">Next: Activity →</button></footer>
        </section>

        <section class="setup-panel" id="setup-panel-activity" aria-labelledby="setup-activity-title" hidden>
          <header class="setup-section-head"><h2 id="setup-activity-title" tabindex="-1">Activity</h2><p>Choose which agent activity appears in Space.</p></header>
          <section class="setup-card setup-runtime">
            <form id="activity-form" novalidate>
              <div class="setup-check-row"><label class="setup-switch" for="runtime-watcher"><input id="runtime-watcher" type="checkbox"><span></span></label><div><b>Update activity automatically</b><small>Keep sessions and project history up to date.</small></div></div>
              <label for="runtime-source-mode">Activity sources</label><select id="runtime-source-mode"><option value="all">All agents</option><option value="active">Chat agent only</option></select>
              <small>Scheduled commands also need automatic activity updates enabled.</small>
              <details class="setup-inline-details"><summary>Advanced</summary><label for="runtime-interval">Check every</label><div class="setup-number"><input id="runtime-interval" type="number" min=".25" max="60" step=".25" inputmode="decimal"><span>seconds</span></div><small>0.25–60 seconds. Default: 1.</small></details>
              <div class="setup-form-error" id="activity-error" role="alert" hidden></div>
              <div class="setup-actions"><button class="setup-primary" id="activity-save" type="submit">Save activity settings</button></div>
            </form>
          </section>
          <div class="setup-usage-reporting" id="usage-reporting" hidden></div>
          <footer class="setup-step-footer"><button class="setup-primary" id="setup-open-projects" type="button">Open Projects →</button></footer>
        </section>

        <section class="setup-panel" id="setup-panel-commands" aria-labelledby="setup-commands-title" hidden>
          <header class="setup-section-head"><h2 id="setup-commands-title" tabindex="-1">Commands</h2><p>Save commands, run them here, and open Inbox for results.</p></header>
          <section class="setup-card setup-commands" id="setup-commands" aria-label="Commands"></section>
        </section>

        <section class="setup-panel" id="setup-panel-server" aria-labelledby="setup-server-title" hidden>
          <header class="setup-section-head"><h2 id="setup-server-title" tabindex="-1">Server</h2><p>Apply saved changes and keep Space up to date.</p></header>
          <section class="setup-card setup-maintenance">
            <div class="setup-card-head"><h3>Restart</h3><button class="setup-restart" id="setup-restart" data-restart type="button" disabled>Restart server</button></div>
            <div class="setup-server-body"><p class="setup-restart-hint" id="setup-restart-hint" role="status"></p><button class="setup-restart" id="runtime-restart" data-restart type="button" hidden>Apply &amp; restart</button></div>
          </section>
          <section class="setup-card setup-version">
            <div class="setup-card-head"><h3>Updates</h3><i id="update-badge">Not checked</i></div>
            <div class="setup-version-body"><div class="setup-version-state" id="update-state"><p>Check for a newer version of Space.</p></div><div class="setup-actions"><button class="setup-secondary" id="update-check" type="button">Check for updates</button><button class="setup-primary" id="update-apply" type="button" hidden>Update now</button><button class="setup-restart" id="update-restart" data-restart type="button" hidden>Restart server</button></div></div>
          </section>
          <button class="setup-secondary" id="setup-quirq" type="button">Technical details →</button>
        </section>
      </div>
    </div>
  </div>`;
  runtimeForm=root.querySelector('#runtime-form');
  secretForm=root.querySelector('#secret-form');
  keyInput=root.querySelector('#secret-key');
  valueInput=root.querySelector('#secret-value');
  secretSaveButton=root.querySelector('#secret-save');
  secretCancelButton=root.querySelector('#secret-cancel');
  secretError=root.querySelector('#secret-error');
}

function hasDraft(panel){
  const configured=runtimeData?.configured||{};
  if(panel==='workspace'){
    const folders=runtimeData?.roots?.configured||{};
    return root.querySelector('#xo-root-input').value!==(folders.xo_projects_root||'')
      ||root.querySelector('#quirq-root-input').value!==(folders.quirq_state_root||'');
  }
  if(panel==='agent')return root.querySelector('#runtime-agent').value!==(configured.agent_name||'');
  return root.querySelector('#runtime-watcher').checked!==configured.watcher_enabled
    ||root.querySelector('#runtime-source-mode').value!==configured.watcher_source_mode
    ||Number(root.querySelector('#runtime-interval').value)!==configured.watcher_interval_seconds;
}

function selectPanel(panel,{focus=false}={}){
  const target=root.querySelector('#setup-panel-'+panel);
  if(!target)return;
  currentPanel=panel;
  root.querySelectorAll('.setup-panel').forEach(el=>el.hidden=el!==target);
  root.querySelectorAll('#setup-nav [data-setup-go]').forEach(button=>{
    if(button.dataset.setupGo===panel)button.setAttribute('aria-current','step');
    else button.removeAttribute('aria-current');
  });
  if(focus){
    target.querySelector('h2').focus({preventScroll:true});
    root.scrollTop=0;
  }
}

function bindEvents(){
  root.addEventListener('click',event=>{
    const button=event.target.closest('[data-setup-go]');
    if(button)selectPanel(button.dataset.setupGo,{focus:true});
    if(event.target.closest('[data-setup-retry]'))loadAll();
  });
  addEventListener('space:setup-section',event=>selectPanel(event.detail?.panel,{focus:true}));
  root.querySelector('#setup-open-projects').addEventListener('click',()=>switchTo('projects'));
  root.querySelector('#setup-quirq').addEventListener('click',()=>switchTo('quirq'));
  for(const [panel,selector] of [['workspace','#roots-form'],['agent','#runtime-form'],['activity','#activity-form']]){
    root.querySelector(selector).addEventListener('input',event=>{
      touched.add(event.target.id);
      drafts[panel]=hasDraft(panel);
      if(panel==='workspace')renderRoots();
      if(panel==='agent')renderSources();
      renderJourney();
    });
  }
  root.querySelector('#activity-form').addEventListener('submit',saveRuntime);
  root.querySelector('#secret-add').addEventListener('click',()=>{
    if(writes.size)return;
    resetSecretForm();secretForm.hidden=false;keyInput.focus();
  });
  root.querySelector('#setup-refresh').addEventListener('click',loadAll);
  runtimeForm.addEventListener('submit',saveRuntime);
  root.querySelector('#roots-form').addEventListener('submit',saveRoots);
  root.querySelector('#roots-copy').addEventListener('click',copyRootCommand);
  root.querySelectorAll('[data-restart]').forEach(button=>button.addEventListener('click',restartRuntime));
  secretForm.addEventListener('submit',saveSecret);
  secretForm.addEventListener('input',renderJourney);
  secretCancelButton.addEventListener('click',resetSecretForm);
  root.querySelector('#secret-toggle').addEventListener('click',toggleSecretValue);
  root.querySelector('#setup-sources').addEventListener('click',handleRecommendedSecret);
  root.querySelector('#secret-list').addEventListener('click',handleSecretListAction);
  root.querySelector('#update-check').addEventListener('click',checkForUpdate);
  root.querySelector('#update-apply').addEventListener('click',applyUpdate);
}

/* ── Server updates ──────────────────────────────────────────────────────
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
  renderUpdateState('<div class="setup-empty">Checking for updates…</div>','Checking');
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
    rows.push('<p>You have the latest version.</p>');
    renderUpdateState(rows.join(''),'Up to date');
  }else{
    rows.push(`<p><b>Latest</b> ${commitLine(s.latest)}</p>`);
    rows.push(`<p>${s.behind} commit${s.behind===1?'':'s'} behind${s.ahead?` · ${s.ahead} local commit${s.ahead===1?'':'s'} not on the remote`:''}${s.dirty?' · local changes present':''}.</p>`);
    if(s.dirty)rows.push('<p>Save your local changes before updating.</p>');
    else if(s.ahead)rows.push('<p>Local and remote changes need to be merged before updating.</p>');
    else applyButton.hidden=false;
    renderUpdateState(rows.join(''),`${s.behind} behind`);
  }
}

async function applyUpdate(){
  const applyButton=root.querySelector('#update-apply');
  setBusy(applyButton,true);
  renderUpdateState('<div class="setup-empty">Updating Space…</div>','Updating');
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
    +(r.requirements_changed?'':'<p>Restart to load the new version.</p>'),
    'Restart needed'
  );
  root.querySelector('#update-restart').hidden=false;
  renderRestartButtons();
}

async function loadAll(){
  if(loading||writes.size){refreshQueued=true;return;}
  loading=true;refreshQueued=false;
  const mine=runtimeRevision;
  root.querySelector('#setup-refresh').disabled=true;
  const [runtimeRes,secretsRes,serverRes]=await Promise.all([
    apiFetch('/api/runtime-config'),apiFetch('/api/secrets'),pollServer(),commands.refresh()
  ]);
  loading=false;
  root.querySelector('#setup-refresh').disabled=false;
  if(mine!==runtimeRevision){refreshQueued=true;}
  else{
    serverData=serverRes.ok?serverRes.data:null;
    if(runtimeRes.ok){runtimeData=runtimeRes.data;runtimeUnavailable=false;renderRuntime();}
    else{runtimeUnavailable=true;renderRuntimeFailure(runtimeRes);}
    if(secretsRes.ok){
      secretItems=secretsRes.data.items||[];renderSecretList();
      if(runtimeData&&!runtimeUnavailable)renderSources();
    }else{
      root.querySelector('#secret-count').textContent='—';
      root.querySelector('#secret-list').innerHTML='<div class="setup-empty is-error">'+esc(secretsRes.error)+'</div>';
    }
    renderRestartButtons();
    setConfigBusy(false);
  }
  if(refreshQueued&&!writes.size)loadAll();
}

function renderRuntime(){
  const configured=runtimeData.configured;
  const agents=runtimeData.agents||[];
  const select=root.querySelector('#runtime-agent');
  const selected=drafts.agent?select.value:configured.agent_name;
  select.innerHTML=agents.map(agent=>'<option value="'+esc(agent.name)+'">'+esc(prettyName(agent.name))+'</option>').join('');
  if(selected&&!agents.some(agent=>agent.name===selected)){
    select.insertAdjacentHTML('beforeend','<option value="'+esc(selected)+'">'+esc(prettyName(selected))+' (unavailable)</option>');
  }
  select.value=selected;
  for(const [id,key,property] of [['runtime-watcher','watcher_enabled','checked'],['runtime-source-mode','watcher_source_mode','value'],['runtime-interval','watcher_interval_seconds','value']]){
    if(!drafts.activity||!touched.has(id))root.querySelector('#'+id)[property]=configured[key];
  }
  const agentPending=configured.agent_name!==runtimeData.applied.agent_name;
  const badge=root.querySelector('#setup-applied-badge');
  badge.textContent=agentPending?'Restart to apply':'In use';
  badge.className=agentPending?'is-pending':'is-good';
  root.querySelector('#runtime-restart').hidden=!runtimeData.restart_required;
  renderRestartButtons();
  renderUsageReporting();renderOverview();renderRoots();renderSources();renderJourney();
}

function renderJourney(){
  const steps=setupSteps(runtimeUnavailable?null:runtimeData);
  const credentialDraft=!secretForm.hidden&&Boolean(valueInput.value||(!editingKey&&keyInput.value));
  const dirty=panel=>drafts[panel]||(panel==='agent'&&credentialDraft);
  for(const panel of ['workspace','agent','activity']){
    const state=steps[panel],el=root.querySelector('#setup-step-'+panel);
    el.textContent=dirty(panel)?'Unsaved changes':runtimeUnavailable?'Unavailable'
      :panel==='agent'&&state.label===runtimeData?.configured.agent_name?prettyName(state.label):state.label;
    el.className='is-'+(dirty(panel)?'pending':state.tone);
  }
  if(restarting||runtimeUnavailable)return;
  const unsaved=Object.keys(drafts).filter(dirty);
  const alert=root.querySelector('#setup-alert');
  if(unsaved.length){
    const labels={workspace:'Workspace',agent:'Agent & access',activity:'Activity'};
    alert.className='setup-alert is-pending';
    alert.innerHTML='<div><b>Unsaved changes</b><p>'+esc(unsaved.map(panel=>labels[panel]).join(', '))+'</p></div>'
      +'<button class="setup-secondary" type="button" data-setup-go="'+unsaved[0]+'">Review changes</button>';
  }else if(steps.next){
    const {panel,label,message}=steps.next;
    alert.className='setup-alert is-pending';
    alert.innerHTML='<div><b>'+esc(label)+'</b><p>'+esc(message)+'</p></div>'
      +'<button class="setup-secondary" type="button" data-setup-go="'+panel+'">'+esc(label)+' →</button>';
  }else{
    alert.className='setup-alert'+(runtimeData?' is-good':'');
    alert.innerHTML='<div><b>'+(runtimeData?'Settings applied':'Checking settings…')+'</b></div>';
  }
}

function setConfigBusy(busy){
  busy=busy||writes.size>0;
  setSecretBusy(busy);
  for(const id of ['roots-save','runtime-save','activity-save']){
    const button=root.querySelector('#'+id);
    setBusy(button,busy);button.disabled=busy||runtimeUnavailable||!runtimeData;
  }
}

/* Usage-reporting status (GET /api/runtime-config → usage_reporting).
   States mirror services/usage_sync.py usage_reporting_status(): off (no
   key — nothing is sent), on (key accepted; shows the last reported day),
   blocked (key rejected — nothing is sent), pending (key set, no
   conclusive probe yet). Absent field (older server): stay hidden. */
function renderUsageReporting(){
  const el=root.querySelector('#usage-reporting');
  if(!el)return;
  const ur=runtimeData.usage_reporting;
  if(!ur){el.hidden=true;return;}
  const link=' <a href="https://github.com/quirq-ai/xo-space#what-leaves-your-machine" '
    +'target="_blank" rel="noopener noreferrer">What leaves your machine &#8599;</a>';
  const states={
    on:['on','Daily totals: tokens, cost, sessions and tools. Prompts, responses and files stay local.'
      +(ur.last_synced_date?' Last sent '+esc(ur.last_synced_date)+'.':'')],
    blocked:['blocked','XO_API_KEY was rejected. Replace or remove it; nothing is sent.'],
    pending:['pending','Key saved; waiting for verification. Nothing is sent yet.'],
    off:['off','No XO_API_KEY. Nothing is sent.']
  };
  const [state,detail]=states[ur.status]||states.off;
  el.className='setup-usage-reporting is-'+state;
  el.innerHTML='<span aria-hidden="true"></span><div>'
    +'<b>Usage reporting: '+state+'</b>'
    +'<p>'+detail+link+'</p>'
    +'</div>';
  el.hidden=false;
}

function renderOverview(){
  const paths=runtimeData.paths||{};
  const publicUrl=runtimeData.network?.public_url||location.origin;
  const listenPort=runtimeData.network?.listen_port||'—';
  root.querySelector('#setup-overview').innerHTML=
    overviewCard('Browser address',publicUrl,'Server port '+listenPort)
    +overviewCard(
      'Projects folder',
      paths.projects?.host_path||paths.projects?.container_path,
      pathState(paths.projects)+' · execution root '+(paths.ai_workspace?.container_path||'not set')
    )
    +overviewCard('Space data folder',paths.state?.host_path||paths.state?.container_path,pathState(paths.state));
}

function renderRoots(){
  if(!runtimeData)return;
  const roots=runtimeData.roots||{};
  const configured=roots.configured||{};
  const applied=roots.applied||{};
  if(!drafts.workspace||!touched.has('xo-root-input'))root.querySelector('#xo-root-input').value=configured.xo_projects_root||'';
  if(!drafts.workspace||!touched.has('quirq-root-input'))root.querySelector('#quirq-root-input').value=configured.quirq_state_root||'';
  for(const [input,label,key] of [['xo-root-input','xo-root-applied','xo_projects_root'],['quirq-root-input','quirq-root-applied','quirq_state_root']]){
    const using=applied[key];
    root.querySelector('#'+label).textContent=using&&using!==root.querySelector('#'+input).value?'Currently using: '+using:'';
  }
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
  if(!runtimeData||runtimeUnavailable)return;
  const configuredKeys=new Set(secretItems.filter(item=>item.is_set).map(item=>item.key));
  const sources=runtimeData.agents||[];
  const target=root.querySelector('#setup-sources');
  if(!sources.length){target.innerHTML='<div class="setup-empty">No agents found.</div>';return;}
  const selectedName=root.querySelector('#runtime-agent').value||runtimeData.configured.agent_name;
  const otherOpen=target.querySelector('.setup-other-agents')?.open||false;
  const detailsOpen=new Set([...target.querySelectorAll('.source-details[open]')].map(el=>el.dataset.source));
  const row=source=>{
    const selected=source.name===selectedName;
    const keys=source.secrets||[];
    const secretButtons=keys.length?'<div class="source-secrets">'+keys.map(item=>
      '<button type="button" data-secret-key="'+esc(item.key)+'" title="'+esc(item.description)+'" class="'+(configuredKeys.has(item.key)?'is-set':'')+'">'
        +'<span>'+(configuredKeys.has(item.key)?'✓':'+')+'</span>'+esc(item.label)+'</button>'
    ).join('')+'</div>':'<p class="source-note">Uses its own sign-in.</p>';
    return '<article class="source-row '+(source.active?'is-active ':'')+(selected?'is-selected':'')+'">'
      +'<div class="source-title"><b>'+esc(prettyName(source.name))+'</b>'
        +(source.active?'<span>In use</span>':selected?'<span class="is-pending">'+(drafts.agent?'Selected':'Restart to apply')+'</span>':'')
        +(runtimeData.applied.watcher_enabled&&source.watched?'<span class="is-watched">Included in activity</span>':'')+'</div>'
      +'<div class="source-facts">'+fact(source.binary_available?'Installed':'Not installed',source.binary_available?'good':'muted')
        +fact(source.home?.exists?'Agent folder found':'Agent folder missing',source.home?.exists?'good':'bad')
        +((!source.home?.exists||!source.binary_available)&&source.install_url?'<a class="source-install" href="'+esc(source.install_url)+'" target="_blank" rel="noopener noreferrer">Install '+esc(prettyName(source.name))+' ↗</a>':'')+'</div>'
      +secretButtons
      +'<details class="source-details" data-source="'+esc(source.name)+'"'+(detailsOpen.has(source.name)?' open':'')+'><summary>Agent folder details</summary>'
        +'<div class="source-path"><span>Host</span><code>'+esc(source.home?.host_path||'Not reported')+'</code></div>'
        +'<div class="source-path"><span>Server</span><code>'+esc(source.home?.container_path||'Not reported')+'</code></div>'
      +'</details></article>';
  };
  const chosen=sources.find(source=>source.name===selectedName);
  const others=sources.filter(source=>source!==chosen);
  target.innerHTML=(chosen?row(chosen):'')+(others.length?'<details class="setup-other-agents"'+(otherOpen?' open':'')+'><summary>Other agents ('+others.length+')</summary>'+others.map(row).join('')+'</details>':'');
}

function fact(text,tone){
  return '<span class="source-fact is-'+tone+'">'+esc(text)+'</span>';
}

function renderRuntimeFailure(res){
  const alert=root.querySelector('#setup-alert');
  alert.className='setup-alert is-error';
  alert.innerHTML='<div><b>Settings unavailable</b><p>'+esc(res.offline?'Space is restarting or unreachable.':res.error)+'</p></div><button class="setup-secondary" type="button" data-setup-retry>Retry</button>';
  renderJourney();
  root.querySelector('#usage-reporting').hidden=true;
  root.querySelector('#setup-overview').innerHTML='';
  root.querySelector('#setup-sources').innerHTML='<div class="setup-empty is-error">Could not check agents.</div>';
}

async function saveRuntime(event){
  event.preventDefault();
  if(writes.size||!runtimeData||runtimeUnavailable)return;
  const panel=event.currentTarget.id==='activity-form'?'activity':'agent';
  const error=root.querySelector(panel==='activity'?'#activity-error':'#runtime-error');
  error.hidden=true;error.textContent='';
  const configured=runtimeData.configured;
  const body={agent_name:configured.agent_name,watcher_enabled:configured.watcher_enabled,
    watcher_interval_seconds:configured.watcher_interval_seconds,watcher_source_mode:configured.watcher_source_mode};
  if(panel==='agent')body.agent_name=root.querySelector('#runtime-agent').value;
  else{
    const interval=Number(root.querySelector('#runtime-interval').value);
    if(!Number.isFinite(interval)||interval<.25||interval>60){
      error.textContent='Use an interval between 0.25 and 60 seconds.';error.hidden=false;return;
    }
    body.watcher_enabled=root.querySelector('#runtime-watcher').checked;
    body.watcher_interval_seconds=interval;
    body.watcher_source_mode=root.querySelector('#runtime-source-mode').value;
  }
  const form=event.currentTarget;
  writes.add(panel);runtimeRevision++;setConfigBusy(true);
  [...form.elements].forEach(control=>control.disabled=true);
  const res=await apiFetch('/api/runtime-config',{method:'PUT',body});
  writes.delete(panel);runtimeRevision++;
  [...form.elements].forEach(control=>control.disabled=false);setConfigBusy(false);
  if(!res.ok){error.textContent=res.error;error.hidden=false;}
  else{
    drafts[panel]=false;runtimeData=res.data.status;renderRuntime();
    toast(runtimeData.restart_required?'Saved. Restart to apply.':'Settings saved');
  }
  if(refreshQueued)loadAll();
}

async function saveRoots(event){
  event.preventDefault();
  if(writes.size||!runtimeData||runtimeUnavailable)return;
  const error=root.querySelector('#roots-error');error.hidden=true;error.textContent='';
  const body={xo_projects_root:root.querySelector('#xo-root-input').value.trim(),quirq_state_root:root.querySelector('#quirq-root-input').value.trim()};
  const form=event.currentTarget;
  writes.add('workspace');runtimeRevision++;setConfigBusy(true);
  [...form.elements].forEach(control=>control.disabled=true);
  const res=await apiFetch('/api/runtime-config/roots',{method:'PUT',body});
  writes.delete('workspace');runtimeRevision++;
  [...form.elements].forEach(control=>control.disabled=false);setConfigBusy(false);
  if(!res.ok){error.textContent=res.error||'Could not save folders.';error.hidden=false;}
  else{
    drafts.workspace=false;runtimeData=res.data.status;renderRuntime();
    toast(runtimeData.roots?.change_required?'Folders saved. Restart to apply.':'Folders saved');
  }
  if(refreshQueued)loadAll();
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
  if(restarting||!['managed','native'].includes(serverData?.restart_mode))return;
  if(!confirm('Restart Space? The page will reconnect automatically.'))return;
  restarting=true;
  renderRestartButtons();
  const error=root.querySelector('#setup-restart-error');
  error.hidden=true;
  const previousInstance=serverData.instance_id;
  const res=await apiFetch('/space/server/restart',{method:'POST'});
  if(!res.ok&&!res.offline){
    restarting=false;
    renderRestartButtons();
    error.textContent=res.error;
    error.hidden=false;
    return;
  }
  const alert=root.querySelector('#setup-alert');
  alert.className='setup-alert is-pending';
  alert.innerHTML='<div><b>Restarting Space…</b><p>The page will reconnect automatically.</p></div>';
  for(let attempt=0;attempt<60;attempt+=1){
    await delay(1000);
    const probe=await pollServer();
    if(probe.ok&&probe.data?.instance_id&&probe.data.instance_id!==(res.data?.instance_id||previousInstance)){
      location.reload();
      return;
    }
  }
  restarting=false;
  renderRestartButtons();
  error.textContent='The restart is taking longer than expected. Refresh status or check the server log.';
  error.hidden=false;
}

function renderRestartButtons(){
  const supported=['managed','native'].includes(serverData?.restart_mode);
  const hint=!serverData?'Server status unavailable. Refresh status to retry.'
    :supported?'':'Ctrl-C and re-run Space in the terminal where it started.';
  root.querySelector('#setup-restart-hint').textContent=restarting?'Restarting…':hint;
  const installerNeeded=runtimeData?.managed_container&&runtimeData?.roots?.change_required;
  const pending=Boolean(runtimeData?.restart_required)&&!installerNeeded;
  const updatePending=!root.querySelector('#update-restart').hidden;
  root.querySelector('#runtime-restart').hidden=!pending||updatePending;
  root.querySelector('#setup-restart').hidden=pending||updatePending;
  root.querySelectorAll('[data-restart]').forEach(button=>{
    setBusy(button,restarting);button.disabled=restarting||!supported;button.title=hint;
    button.textContent=restarting?'Restarting…':button.id==='runtime-restart'?'Apply & restart':'Restart server';
  });
}

function handleRecommendedSecret(event){
  const button=event.target.closest('button[data-secret-key]');
  if(!button)return;
  beginSecret(button.dataset.secretKey);
}

function renderSecretList(){
  root.querySelector('#secret-count').textContent=String(secretItems.length);
  const list=root.querySelector('#secret-list');
  if(!secretItems.length){
    list.innerHTML='<div class="setup-empty"><b>No credentials added.</b><span>Use your agent’s sign-in or add a key above.</span></div>';
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
  if(writes.size)return;
  selectPanel('agent');
  secretForm.hidden=false;
  editingKey=key;
  keyInput.value=key;
  keyInput.readOnly=true;
  valueInput.value='';
  valueInput.type='password';
  root.querySelector('#secret-toggle').textContent='Show';
  root.querySelector('#secret-toggle').setAttribute('aria-label','Show value');
  root.querySelector('#secret-form-title').textContent=secretItems.some(item=>item.key===key)?'Replace credential':'Set credential';
  secretSaveButton.textContent=secretItems.some(item=>item.key===key)?'Replace value':'Save credential';
  secretCancelButton.hidden=false;
  clearSecretError();
  valueInput.focus();
  secretForm.scrollIntoView({behavior:'smooth',block:'nearest'});
  renderJourney();
}

function resetSecretForm(){
  editingKey=null;
  secretForm.hidden=true;
  secretForm.reset();
  keyInput.readOnly=false;
  valueInput.type='password';
  root.querySelector('#secret-toggle').textContent='Show';
  root.querySelector('#secret-toggle').setAttribute('aria-label','Show value');
  root.querySelector('#secret-form-title').textContent='Add credential';
  secretSaveButton.textContent='Save credential';
  secretCancelButton.hidden=false;
  clearSecretError();
  if(currentPanel==='agent')root.querySelector('#secret-add').focus({preventScroll:true});
  renderJourney();
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
  if(writes.size)return;
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
  writes.add('secret');runtimeRevision++;
  setConfigBusy(true);
  const res=await apiFetch('/api/secrets/'+encodeURIComponent(key),{method:'PATCH',body:{value}});
  valueInput.value='';
  writes.delete('secret');runtimeRevision++;setConfigBusy(false);
  if(!res.ok){
    showSecretError(res.error);
    if(refreshQueued)loadAll();
    return;
  }
  resetSecretForm();
  toast('Credential saved');
  await loadAll();
}

async function removeSecret(key,button){
  if(writes.size||!confirm('Remove '+key+'? The saved value cannot be recovered.'))return;
  writes.add('secret');runtimeRevision++;setConfigBusy(true);
  const res=await apiFetch('/api/secrets/'+encodeURIComponent(key),{method:'DELETE'});
  writes.delete('secret');runtimeRevision++;setConfigBusy(false);
  if(!res.ok){showSecretError(res.error);if(refreshQueued)loadAll();return;}
  if(editingKey===key)resetSecretForm();
  toast(res.data.deleted?'Credential removed':'Credential was already absent');
  await loadAll();
}

function setSecretBusy(busy){
  setBusy(secretSaveButton,busy);setBusy(secretCancelButton,busy);
  keyInput.disabled=busy;valueInput.disabled=busy;
  root.querySelectorAll('#secret-add,#secret-toggle,#setup-sources button[data-secret-key],#secret-list button').forEach(button=>button.disabled=busy);
  secretSaveButton.textContent=writes.has('secret')?'Saving…':(editingKey?'Replace value':'Save credential');
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
