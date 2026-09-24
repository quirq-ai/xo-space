/* Setup controller.

   Runtime controls are typed and restart-aware. Credentials remain write-only:
   this view receives configured status and fixed masks, never saved plaintext.
   The page intentionally separates Quirq's machine-local state from portable
   project `.xo` data. */
import {apiFetch} from '../core/api.js';
import {toast} from '../core/ui.js';
import {pollServer} from '../core/server-widget.js?v=20260914-commands2';
import {mountCommands} from './setup-commands.js?v=20260921-refresh1';
import {setupSteps} from '../core/setup-state.js?v=20260914-manage1';
import {mountIdentity} from './setup-identity.js?v=20260915-typesync1';
import {mountBranding} from './setup-branding.js?v=20260921-branding2';
import {mountTheme} from './setup-theme.js?v=20260922-theme4';
import {mountSetupSearch} from './setup-search.js?v=20260922-theme4';
import {renderSetupShell} from './setup-shell.js?v=20260922-theme5';
import {SETUP_STEPS,SETUP_SECTIONS,resolveSetupSection,setupSectionRoute} from '../core/setup-sections.js?v=20260916-jobs3';

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
let identity=null;
let branding=null;
let brandingDraft=false;
let theme=null,themeDraft=false;
let serverData=null;
let restarting=false;
let currentPanel='workspace';
let runtimeUnavailable=false;
const formDrafts={workspace:false,agent:false,activity:false};
const touched=new Set();
const writes=new Set();
let runtimeRevision=0,refreshQueued=false;
let setupMount=null,connectorMount=null,connectorController=null;
let setupSearch=null;
const toolbarRefreshers=new Set();
const refreshSetupToolbar=()=>{for(const refresh of toolbarRefreshers)refresh?.();};
const setupToolbar=()=>currentPanel==='connectors'?connectorController?.toolbar:setupSearch?.toolbar;

/* Each section is a registered route. They share a mounted shell so changing
   the URL never rebuilds forms. Connectors mount only on their first visit. */
export function createSetupViews(controller){
  connectorController=controller;
  return SETUP_SECTIONS.map(section=>({
    id:section.route,
    route:section.route,aliases:section.aliases,
    label:section.label,
    nav:false,parent:'setup',section:'setup',
    toolbar:setupToolbar,mount:mountSetup,
    show(){
      selectPanel(section.id);
      if(section.id==='commands')commands?.refresh();
    },
  }));
}

function mountSetup(el,ctx){
  toolbarRefreshers.add(ctx.refreshToolbar);
  if(!setupMount)setupMount=(async()=>{
    root=el;
    switchTo=ctx.switchTo;
    renderSetupShell(root);
    bindFormReferences();
    bindEvents();
    setupSearch=mountSetupSearch(root,openPanel,refreshSetupToolbar);
    commands=mountCommands(root.querySelector('#setup-commands'));
    identity=mountIdentity(root.querySelector('#setup-identity'));
    identity.refresh();
    branding=mountBranding(root.querySelector('#setup-branding'),dirty=>{brandingDraft=dirty;renderJourney();});
    branding.refresh();
    theme=mountTheme(root.querySelector('#setup-theme'),dirty=>{themeDraft=dirty;renderJourney();});
    theme.refresh();
    /* Connector links must not wait for unrelated settings/status reads. */
    loadAll().catch(err=>{
      console.error('Setup status failed to load:',err);
      loading=false;runtimeUnavailable=true;
      renderRuntimeFailure({error:'Could not load settings. Try refreshing status.'});
      setConfigBusy(false);
    });
  })();
  return setupMount;
}

let switchTo=()=>{}; /* ctx.switchTo, captured on mount (opens the Quirq view) */

function bindFormReferences(){
  runtimeForm=root.querySelector('#runtime-form');
  secretForm=root.querySelector('#secret-form');
  keyInput=root.querySelector('#secret-key');
  valueInput=root.querySelector('#secret-value');
  secretSaveButton=root.querySelector('#secret-save');
  secretCancelButton=root.querySelector('#secret-cancel');
  secretError=root.querySelector('#secret-error');
}

function hasFormDraft(panel){
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

function selectPanel(requested){
  const panel=resolveSetupSection(requested);
  if(!panel)return false;
  const target=root.querySelector('#setup-panel-'+panel);
  if(!target)return false;
  setupSearch?.clear();
  currentPanel=panel;
  root.querySelectorAll('.setup-panel').forEach(el=>el.hidden=el!==target);
  root.querySelectorAll('#setup-nav [data-setup-go]').forEach(button=>{
    if(button.dataset.setupGo===panel)button.setAttribute('aria-current','step');
    else button.removeAttribute('aria-current');
  });
  if(panel==='connectors'&&!connectorMount&&connectorController){
    const host=root.querySelector('#setup-connectors');
    connectorMount=connectorController.mount(host).catch(err=>{
      console.error('Connectors failed to load:',err);
      host.innerHTML='<div class="setup-empty is-error" role="alert">Connectors could not load. <button type="button" data-connectors-retry>Try again</button></div>';
      connectorMount=null;
    });
  }
  refreshSetupToolbar();
  return true;
}

async function openPanel(requested,{focus=false,target=null}={}){
  if(requested==='projects')return switchTo('projects/manage');
  const panel=resolveSetupSection(requested);
  const route=setupSectionRoute(requested);
  if(!route)return false;
  if(currentPanel===panel&&location.hash==='#/'+route&&root.classList.contains('is-active')){
    setupSearch?.clear();
    refreshSetupToolbar();
  }else await switchTo(route);
  /* A later navigation wins even if it arrived while this route mounted. */
  if(currentPanel!==panel||location.hash!=='#/'+route||!root.classList.contains('is-active'))return false;
  if(focus){
    root.scrollTop=0;
    const selector=target||(requested==='agent'?'#runtime-agent':requested==='activity'?'#runtime-source-mode':null);
    const control=selector?root.querySelector(selector):root.querySelector('#setup-panel-'+panel+' h2');
    const details=control?.closest('details');
    if(details)details.open=true;
    if(control&&!control.disabled)control.focus({preventScroll:true});
    control?.scrollIntoView({block:'nearest'});
  }
  return true;
}

function bindEvents(){
  root.addEventListener('click',event=>{
    const link=event.target.closest('[data-setup-go]');
    if(link){
      if(link.tagName==='A'&&(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey))return;
      event.preventDefault();
      const panel=link.dataset.setupGo;
      openPanel(panel,{focus:true});
    }
    if(event.target.closest('[data-connectors-retry]'))selectPanel('connectors');
    if(event.target.closest('[data-setup-retry]'))loadAll();
  });
  addEventListener('space:setup-section',event=>{
    /* Legacy project-management events are handled before Setup mounts by
       core/project-actions.js, which owns that cross-section handoff. */
    if(event.detail?.panel!=='projects')openPanel(event.detail?.panel,{focus:true});
  });
  root.querySelector('#setup-quirq').addEventListener('click',()=>switchTo('setup/server/details'));
  for(const [panel,selector] of [['workspace','#roots-form'],['agent','#runtime-form'],['activity','#activity-form']]){
    root.querySelector(selector).addEventListener('input',event=>{
      touched.add(event.target.id);
      formDrafts[panel]=hasFormDraft(panel);
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
  const [runtimeRes,secretsRes,serverRes]=await Promise.all([
    apiFetch('/api/runtime-config'),apiFetch('/api/secrets'),pollServer(),commands.refresh()
  ]);
  loading=false;
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
  const selected=formDrafts.agent?select.value:configured.agent_name;
  select.innerHTML=agents.map(agent=>'<option value="'+esc(agent.name)+'">'+esc(prettyName(agent.name))+'</option>').join('');
  if(selected&&!agents.some(agent=>agent.name===selected)){
    select.insertAdjacentHTML('beforeend','<option value="'+esc(selected)+'">'+esc(prettyName(selected))+' (unavailable)</option>');
  }
  select.value=selected;
  for(const [id,key,property] of [['runtime-watcher','watcher_enabled','checked'],['runtime-source-mode','watcher_source_mode','value'],['runtime-interval','watcher_interval_seconds','value']]){
    if(!formDrafts.activity||!touched.has(id))root.querySelector('#'+id)[property]=configured[key];
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
  const dirty={workspace:formDrafts.workspace||brandingDraft||themeDraft,intelligence:formDrafts.agent||formDrafts.activity,secrets:credentialDraft};
  const secretsStep=root.querySelector('#setup-step-secrets');
  secretsStep.textContent=credentialDraft?'Unsaved changes':'Environment values';
  secretsStep.className=credentialDraft?'is-pending':'';
  for(const {id} of SETUP_STEPS){
    const state=steps[id],el=root.querySelector('#setup-step-'+id);
    const unavailable=runtimeUnavailable;
    el.textContent=dirty[id]?'Unsaved changes':unavailable?'Unavailable':state.label;
    el.className='is-'+(dirty[id]?'pending':unavailable?'error':state.tone);
  }
  if(restarting||runtimeUnavailable)return;
  const unsaved=[...SETUP_STEPS.map(step=>step.id),'secrets'].filter(id=>dirty[id]);
  const alert=root.querySelector('#setup-alert');
  if(unsaved.length){
    const labels=Object.fromEntries([...SETUP_STEPS.map(({id,label})=>[id,label]),['secrets','Secrets']]);
    alert.className='setup-alert is-pending';
    alert.innerHTML='<div><b>Unsaved changes</b><p>'+esc(unsaved.map(id=>labels[id]).join(', '))+'</p></div>'
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
        +'. Portable project data lives in each project’s .xo/.'
    )
    +overviewCard('Space data folder',paths.state?.host_path||paths.state?.container_path,
      pathState(paths.state)+'. Machine-local settings, credentials and activity state live here. '
        +'When changing this folder, the installer copies current state into an empty destination; '
        +'otherwise it uses the existing contents without merging.');
}

function renderRoots(){
  if(!runtimeData)return;
  const roots=runtimeData.roots||{};
  const configured=roots.configured||{};
  const applied=roots.applied||{};
  if(!formDrafts.workspace||!touched.has('xo-root-input'))root.querySelector('#xo-root-input').value=configured.xo_projects_root||'';
  if(!formDrafts.workspace||!touched.has('quirq-root-input'))root.querySelector('#quirq-root-input').value=configured.quirq_state_root||'';
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
    // The runtime scans manifest session globs, stopping at 10,000 files.
    // A count describes discovered files, not sign-in or watcher health.
    const sessionCount=Number.isInteger(source.session_files)&&source.session_files>=0?source.session_files:null;
    const sessionNote=sessionCount===null?'Session files not reported.'
      :sessionCount>=10000?'At least '+sessionCount.toLocaleString('en-US')+' session files found (scan limit).'
        :sessionCount+' session file'+(sessionCount===1?'':'s')+' found.';
    const secretButtons=keys.length?'<div class="source-secrets">'+keys.map(item=>
      '<button type="button" data-secret-key="'+esc(item.key)+'" title="'+esc(item.description)+'" class="'+(configuredKeys.has(item.key)?'is-set':'')+'">'
        +'<span>'+(configuredKeys.has(item.key)?'✓':'+')+'</span>'+esc(item.label)+'</button>'
    ).join('')+'</div>':'<p class="source-note">Uses its own sign-in.</p>';
    return '<article class="source-row '+(source.active?'is-active ':'')+(selected?'is-selected':'')+'">'
      +'<div class="source-title"><b>'+esc(prettyName(source.name))+'</b>'
        +(source.active?'<span>In use</span>':selected?'<span class="is-pending">'+(formDrafts.agent?'Selected':'Restart to apply')+'</span>':'')
        +(runtimeData.applied.watcher_enabled&&source.watched?'<span class="is-watched">Included in activity</span>':'')+'</div>'
      +'<div class="source-facts">'+fact(source.binary_available?'Installed':'Not installed',source.binary_available?'good':'muted')
        +fact(source.home?.exists?'Agent folder found':'Agent folder missing',source.home?.exists?'good':'bad')
        +((!source.home?.exists||!source.binary_available)&&source.install_url?'<a class="source-install" href="'+esc(source.install_url)+'" target="_blank" rel="noopener noreferrer">Install '+esc(prettyName(source.name))+' ↗</a>':'')+'</div>'
      +secretButtons
      +'<details class="source-details" data-source="'+esc(source.name)+'"'+(detailsOpen.has(source.name)?' open':'')+'><summary>Agent details</summary>'
        +'<div class="source-path"><span>Host</span><code>'+esc(source.home?.host_path||'Not reported')+'</code></div>'
        +'<div class="source-path"><span>Server</span><code>'+esc(source.home?.container_path||'Not reported')+'</code></div>'
        +'<p class="source-note">'+esc(sessionNote)+'</p>'
        +(!source.binary_available&&source.bootstrap_available
          ?'<p class="source-note">Setup can install the CLI when this agent is selected and the server restarts.</p>':'')
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
    formDrafts[panel]=false;runtimeData=res.data.status;renderRuntime();
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
    formDrafts.workspace=false;runtimeData=res.data.status;renderRuntime();
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
  error.textContent='The restart is taking longer than expected. Refresh the page or check the server log.';
  error.hidden=false;
}

function renderRestartButtons(){
  const supported=['managed','native'].includes(serverData?.restart_mode);
  const hint=!serverData?'Server status unavailable. Refresh the page to retry.'
    :supported?'':'Ctrl-C and re-run Space in the terminal where it started.';
  const reasons=runtimeData?.restart_reasons||[];
  const changes=[runtimeData?.roots?.change_required?'folders':'',
    reasons.includes('runtime')?'agent or activity settings':'',
    reasons.includes('secrets')?'credentials':''].filter(Boolean);
  const pendingHint=changes.length?'Pending changes: '+changes.join(', ')+'.':'';
  root.querySelector('#setup-restart-hint').textContent=restarting?'Restarting…':[pendingHint,hint].filter(Boolean).join(' ');
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
    list.innerHTML='<div class="setup-empty"><b>No secrets added.</b><span>Add an environment key and its value.</span></div>';
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

async function beginSecret(key){
  if(writes.size)return;
  if(!await openPanel('secrets'))return;
  secretForm.hidden=false;
  editingKey=key;
  keyInput.value=key;
  keyInput.readOnly=true;
  valueInput.value='';
  valueInput.type='password';
  root.querySelector('#secret-toggle').textContent='Show';
  root.querySelector('#secret-toggle').setAttribute('aria-label','Show value');
  root.querySelector('#secret-form-title').textContent=secretItems.some(item=>item.key===key)?'Replace secret':'Set secret';
  secretSaveButton.textContent=secretItems.some(item=>item.key===key)?'Replace value':'Save secret';
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
  root.querySelector('#secret-form-title').textContent='Add secret';
  secretSaveButton.textContent='Save secret';
  secretCancelButton.hidden=false;
  clearSecretError();
  if(currentPanel==='secrets')root.querySelector('#secret-add').focus({preventScroll:true});
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
  toast('Secret saved');
  identity?.refresh();
  await loadAll();
}

async function removeSecret(key,button){
  if(writes.size||!confirm('Remove '+key+'? The saved value cannot be recovered.'))return;
  writes.add('secret');runtimeRevision++;setConfigBusy(true);
  const res=await apiFetch('/api/secrets/'+encodeURIComponent(key),{method:'DELETE'});
  writes.delete('secret');runtimeRevision++;setConfigBusy(false);
  if(!res.ok){showSecretError(res.error);if(refreshQueued)loadAll();return;}
  if(editingKey===key)resetSecretForm();
  toast(res.data.deleted?'Secret removed':'Secret was already absent');
  identity?.refresh();
  await loadAll();
}

function setSecretBusy(busy){
  setBusy(secretSaveButton,busy);setBusy(secretCancelButton,busy);
  keyInput.disabled=busy;valueInput.disabled=busy;
  root.querySelectorAll('#secret-add,#secret-toggle,#setup-sources button[data-secret-key],#secret-list button').forEach(button=>button.disabled=busy);
  secretSaveButton.textContent=writes.has('secret')?'Saving…':(editingKey?'Replace value':'Save secret');
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
