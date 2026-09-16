/* Machine-local Quirq state explorer.

   The API returns operational summaries and a filesystem catalog only. Secret
   values, source cursor paths, and raw native-session data never reach this
   view. */
import {apiFetch,failText} from '../core/api.js';
import {toast} from '../core/ui.js';

const esc=value=>String(value??'').replace(
  /[&<>"]/g,
  char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char])
);

let root=null;
let timer=null;
let loading=false;
let go=()=>{};
let healthLoading=false;
let lastHealth=null;
let pendingMove=null;

/* No top-level tab: Quirq opens from the Technical details button in Setup's Server section (and stays
   deep-linkable at #/quirq); Setup's tab lights up while it is open. It stays
   its own view rather than a card inside Setup so its 10s refresh and full
   state tree keep a page to themselves. */
export default {
  id:'quirq',route:'setup/server/details',aliases:['quirq'],
  label:'Quirq',
  order:8,nav:false,parent:'setup',
  async mount(el,ctx){
    root=el;
    go=ctx.switchTo;
    renderShell();
    root.querySelector('#quirq-refresh').addEventListener('click',()=>loadCatalog(true));
    root.querySelector('#quirq-health-run').addEventListener('click',()=>loadHealth());
    root.querySelector('#quirq-health').addEventListener('click',handleHealthClick);
    root.addEventListener('click',handleCrossViewNavigation);
    await Promise.all([loadCatalog(),loadHealth()]);
  },
  show(){
    if(root){
      loadCatalog(false);
      loadHealth();
    }
    /* Only the catalog polls. Health checks run on open and on "Run checks". */
    if(root&&!timer){
      timer=setInterval(()=>loadCatalog(false),10000);
    }
  },
  hide(){
    clearInterval(timer);
    timer=null;
  }
};

function renderShell(){
  root.innerHTML=
    '<div class="quirq-page">'
      +'<header class="quirq-hero">'
        +'<div>'
          +'<div class="quirq-kicker"><span></span>Machine-local control plane</div>'
          +'<h1>Inside <em>.quirq</em></h1>'
          +'<p>A live, privacy-aware map of installation state, watcher cursors, runtime configuration, credentials, and ephemeral activity.</p>'
        +'</div>'
        /* This view has no tab of its own — every other control on the page
           leads further away, so the way home belongs in the hero. */
        +'<div class="quirq-hero-actions">'
          +'<button id="quirq-back" type="button" data-go-view="setup/server">&#8592; Setup</button>'
          +'<button id="quirq-refresh" type="button">Refresh data</button>'
        +'</div>'
      +'</header>'
      +'<section class="quirq-panel quirq-health" id="quirq-health">'
        +'<header><div><span>State health</span><h2>Health checks</h2></div>'
          +'<div class="quirq-health-actions"><b id="quirq-health-level">—</b>'
          +'<button id="quirq-health-run" type="button">Run checks</button></div>'
        +'</header>'
        +'<div id="quirq-health-list"><div class="quirq-empty">Checking state…</div></div>'
      +'</section>'
      +'<section class="quirq-path" id="quirq-path"><div class="quirq-skeleton"></div></section>'
      +'<section class="quirq-metrics" id="quirq-metrics" aria-label="Quirq state metrics"></section>'
      +'<section class="quirq-panel quirq-storage-map">'
        +'<header><div><span>Watcher ownership boundary</span><h2>Where the watcher writes</h2></div><b>Two destinations</b></header>'
        +'<div class="quirq-storage-columns" id="quirq-storage-columns"><div class="quirq-empty">Mapping watcher outputs…</div></div>'
        +'<div class="quirq-storage-actions">'
          +'<p id="quirq-legacy-note"></p>'
          +'<div>'
            +'<button type="button" data-go-view="projects/data/list">Open project data</button>'
            +'<button type="button" data-wiki-page="xo-data">Read the .xo catalog</button>'
            +'<button type="button" data-wiki-page="watcher">Read watcher internals</button>'
          +'</div>'
        +'</div>'
      +'</section>'
      +'<section class="quirq-flow">'
        +'<div class="flow-node"><span>01 · Inputs</span><b>Native sessions</b><p>Mounted CLI stores</p></div>'
        +'<i>→</i>'
        +'<div class="flow-node is-live"><span>02 · Process</span><b>Watcher</b><p id="quirq-flow-watcher">Inspecting…</p></div>'
        +'<i>→</i>'
        +'<div class="flow-node is-state"><span>03 · State</span><b>.quirq + .xo</b><p>Local control · portable history</p></div>'
        +'<i>→</i>'
        +'<div class="flow-node"><span>04 · Views</span><b>Local APIs</b><p>Timeline · projects · this map</p></div>'
      +'</section>'
      +'<div class="quirq-grid">'
        +'<section class="quirq-panel">'
          +'<header><div><span>Live pulse</span><h2>Watcher activity</h2></div><b id="quirq-activity-badge">—</b></header>'
          +'<div id="quirq-activity"></div>'
        +'</section>'
        +'<section class="quirq-panel">'
          +'<header><div><span>Safe summary</span><h2>Runtime &amp; credentials</h2></div><b id="quirq-secret-count">—</b></header>'
          +'<div id="quirq-runtime"></div>'
        +'</section>'
      +'</div>'
      +'<section class="quirq-panel quirq-files">'
        +'<header><div><span>On disk now</span><h2>State tree</h2></div><b id="quirq-file-count">—</b></header>'
        +'<div class="quirq-file-head"><span>Path</span><span>Purpose</span><span>Size</span><span>Updated</span></div>'
        +'<div id="quirq-tree"><div class="quirq-empty">Reading the state directory…</div></div>'
        +'<footer>Symlinks are skipped. Secret values and watcher source paths are never returned by this API.</footer>'
      +'</section>'
    +'</div>';
}

async function loadCatalog(notify=false){
  if(loading||!root)return;
  loading=true;
  const button=root.querySelector('#quirq-refresh');
  button.disabled=true;
  button.textContent='Refreshing…';
  const response=await apiFetch('/api/quirq');
  loading=false;
  button.disabled=false;
  button.textContent='Refresh data';
  if(!response.ok){
    renderFailure(response.error);
    return;
  }
  renderCatalog(response.data);
  if(notify)toast('Quirq state refreshed');
}

function renderCatalog(data){
  const path=data.root||{};
  root.querySelector('#quirq-path').className='quirq-path '+(path.writable?'is-good':'is-warn');
  root.querySelector('#quirq-path').innerHTML=
    '<div><span>Host root</span><b title="'+esc(path.host_path||'')+'">'+esc(path.host_path||'Not reported by this runtime')+'</b></div>'
    +'<div><span>Container root</span><code>'+esc(path.container_path||'—')+'</code></div>'
    +'<div class="quirq-path-state"><i></i><b>'+(path.writable?'Readable & writable':path.readable?'Read-only':'Unavailable')+'</b>'
      +(data.root_change_required?'<em>New roots waiting for installer</em>':'')+'</div>';

  const totals=data.totals||{};
  const activity=data.activity||{};
  const projectOutputs=data.project_outputs||{};
  root.querySelector('#quirq-metrics').innerHTML=
    metric('Files',totals.files||0,'state artifacts')
    +metric('Storage',formatBytes(totals.bytes||0),'excluding directories')
    +metric('Projects',projectOutputs.project_count||0,'portable .xo stores')
    +metric('Open now',activity.workspace_open_sessions||0,'workspace sessions');

  const watcher=data.watcher||{};
  root.querySelector('#quirq-flow-watcher').textContent=watcher.enabled
    ?'Every '+watcher.interval_seconds+'s · '+(watcher.source_mode==='all'?'all stores':'active store')
    :'Paused';
  renderStorageMap(data);
  renderActivity(activity,watcher);
  renderRuntime(data);
  renderTree(data.tree||[],totals);
}

function renderStorageMap(data){
  const tree=data.tree||[];
  const projectOutputs=data.project_outputs||{};
  const machineFiles=tree.filter(item=>item.kind==='file'&&(
    /^projects\/[^/]*offsets\.json$/.test(item.path)
    ||item.path.startsWith('.locks/')
    ||item.path.startsWith('cache/activity/')
    ||item.path==='cache/heartbeat.json'
  ));
  const sourceCursorFiles=machineFiles.filter(item=>
    /(^|\/)[^/]+-offsets\.json$/.test(item.path)
  );
  const lockFiles=machineFiles.filter(item=>item.path.startsWith('.locks/'));
  const projectActivityFiles=machineFiles.filter(item=>item.path.startsWith('cache/activity/projects/'));
  const workspaceActivityFiles=machineFiles.filter(item=>item.path==='cache/activity/workspace.json');
  const machineContract=[
    {
      path:'projects/offsets.json',
      purpose:'Shared JSONL byte and inode cursors; source paths remain hidden here.',
      status:(data.watcher?.offsets_present?'1 present · ':'0 present · ')+(data.watcher?.tracked_files||0)+' tracked'
    },
    {
      path:'projects/*-offsets.json',
      purpose:'Optional per-source cursor stores for runtimes that do not tail JSONL.',
      status:sourceCursorFiles.length+' present'
    },
    {
      path:'.locks/*.lock',
      purpose:'Advisory writer coordination; lock filenames use safe path hashes.',
      status:lockFiles.length+' present'
    },
    {
      path:'cache/activity/projects/<id>.json',
      purpose:'Ephemeral “open now” heartbeat for each discovered XO project.',
      status:projectActivityFiles.length+' present'
    },
    {
      path:'cache/activity/workspace.json',
      purpose:'Workspace union of live project sessions, refreshed every tick.',
      status:workspaceActivityFiles.length+' present'
    }
  ];
  const projectRows=projectOutputs.project_contract||[];
  const workspaceRows=projectOutputs.workspace_contract||[];
  const machineRoot=(data.root?.host_path||data.root?.container_path||'~/.quirq');
  const xoRoot=projectOutputs.root?.host_path||projectOutputs.root?.container_path||'XO_PROJECTS_ROOT';
  const machineList=machineContract.map(item=>storageRow(
    item.path,
    item.purpose,
    item.status,
    'local'
  )).join('');
  const portableRows=[
    ...projectRows.map(item=>({...item,scope:'project'})),
    ...workspaceRows.map(item=>({...item,scope:'workspace'}))
  ];
  const portableList=portableRows.map(item=>storageRow(
    item.location,
    item.purpose,
    item.present_count+' present',
    item.scope
  )).join('');
  root.querySelector('#quirq-storage-columns').innerHTML=
    '<article class="quirq-storage-side is-local">'
      +'<header><div><span>Machine-local · ephemeral</span><h3>.quirq watcher state</h3></div><b>'+machineFiles.length+' files</b></header>'
      +'<code class="quirq-storage-path" title="'+esc(machineRoot)+'">'+esc(machineRoot)+'</code>'
      +'<p>Read cursors, coordination locks, and “open now” heartbeats. These help this installation keep up and never become project history.</p>'
      +'<div class="quirq-storage-list">'+machineList+'</div>'
    +'</article>'
    +'<article class="quirq-storage-side is-portable">'
      +'<header><div><span>XO project · portable</span><h3>.xo derived metadata</h3></div><b>'+esc(projectOutputs.project_count||0)+' projects</b></header>'
      +'<code class="quirq-storage-path" title="'+esc(xoRoot)+'">'+esc(xoRoot)+'/&lt;project&gt;/.xo</code>'
      +'<p>Identity, session indexes, todos, statistics, and timeline history. These files describe durable work without copying full conversations.</p>'
      +'<div class="quirq-storage-list">'+portableList+'</div>'
    +'</article>';
  const legacy=projectOutputs.legacy_activity_files||0;
  root.querySelector('#quirq-legacy-note').innerHTML=legacy
    ?'<b>'+legacy+' legacy .xo/activity.json file'+(legacy===1?'':'s')+'</b> found. '+esc(projectOutputs.legacy_activity_note)
    :'Current live activity is correctly stored only under <code>.quirq/cache/activity</code>.';
}

function storageRow(path,purpose,status,tone){
  return '<div class="quirq-storage-row">'
    +'<div><code>'+esc(path)+'</code><p>'+esc(purpose)+'</p></div>'
    +'<span class="is-'+esc(tone)+'">'+esc(status)+'</span>'
  +'</div>';
}

async function handleCrossViewNavigation(event){
  const viewButton=event.target.closest('[data-go-view]');
  if(viewButton){
    await go(viewButton.dataset.goView);
    return;
  }
  const wikiButton=event.target.closest('[data-wiki-page]');
  if(!wikiButton)return;
  await go('wiki');
  dispatchEvent(new CustomEvent('space:wiki-page',{
    detail:wikiButton.dataset.wikiPage
  }));
}

function metric(label,value,note){
  return '<div><span>'+esc(label)+'</span><b>'+esc(value)+'</b><p>'+esc(note)+'</p></div>';
}

// Three states, not two. `enabled` is configuration — it says what the
// watcher was asked to do, never whether the loop is running — so a
// crashed watcher used to keep showing "Live". `alive` is the observed
// heartbeat: enabled but not beating is the failure worth shouting about.
function watcherBadge(watcher){
  if(!watcher.enabled)return{text:'Paused',cls:'is-muted',title:'Watcher disabled by runtime configuration'};
  if(watcher.alive)return{
    text:'Live',
    cls:'is-live',
    title:'Last tick '+(watcher.last_tick_at||'unknown')+' · '+(watcher.tick_count??'?')+' ticks'
  };
  return{
    text:'Stalled',
    cls:'is-stalled',
    title:watcher.last_tick_at
      ?'Watcher is enabled but has not ticked since '+watcher.last_tick_at
      :'Watcher is enabled but has never written a heartbeat'
  };
}

function renderActivity(activity,watcher){
  const projects=activity.projects||[];
  const active=projects.filter(project=>project.open_sessions>0).length;
  const badge=root.querySelector('#quirq-activity-badge');
  const state=watcherBadge(watcher);
  badge.textContent=state.text;
  badge.className=state.cls;
  badge.title=state.title;
  const target=root.querySelector('#quirq-activity');
  const summary=
    '<div class="quirq-stat-row">'
      +'<div><span>Workspace sessions</span><b>'+esc(activity.workspace_open_sessions||0)+'</b></div>'
      +'<div><span>Active projects</span><b>'+esc(active)+'</b></div>'
      +'<div><span>Cursor files</span><b>'+esc(watcher.tracked_files||0)+'</b></div>'
    +'</div>';
  if(!projects.length){
    target.innerHTML=summary+'<div class="quirq-empty">No project activity snapshots have been written yet.</div>';
    return;
  }
  target.innerHTML=summary+'<div class="quirq-projects">'+projects.map(project=>
    '<div class="quirq-project '+(project.open_sessions?'is-active':'')+'">'
      +'<span class="quirq-pulse"></span>'
      +'<div><b>'+esc(project.project_id)+'</b><small>'+esc(project.runtimes.join(', ')||'No runtime currently open')+'</small></div>'
      +'<strong>'+esc(project.open_sessions)+' open</strong>'
      +'<time title="'+esc(project.updated_at||'')+'">'+esc(relativeTime(project.updated_at))+'</time>'
    +'</div>'
  ).join('')+'</div>';
}

function renderRuntime(data){
  const runtime=data.runtime||{};
  const credentials=data.credentials||[];
  root.querySelector('#quirq-secret-count').textContent=credentials.length+' configured';
  root.querySelector('#quirq-runtime').innerHTML=
    '<dl class="quirq-config">'
      +configRow('Agent backend',pretty(runtime.agent_name))
      +configRow('Watcher',runtime.watcher_enabled?'Enabled':'Disabled')
      +configRow('Source coverage',runtime.watcher_source_mode==='all'?'All mounted stores':'Active store only')
      +configRow('Tick interval',(runtime.watcher_interval_seconds??'—')+' seconds')
      +configRow('Onboarding',data.install_state?.onboarding_completed?'Complete':'Not complete')
    +'</dl>'
    +'<div class="quirq-credentials">'
      +'<span>Credential names <i>values masked</i></span>'
      +(credentials.length
        ?'<div>'+credentials.map(item=>'<code>'+esc(item.key)+' <b>'+esc(item.value)+'</b></code>').join('')+'</div>'
        :'<p>No environment credentials are saved.</p>')
    +'</div>';
}

function configRow(label,value){
  return '<div><dt>'+esc(label)+'</dt><dd>'+esc(value)+'</dd></div>';
}

function renderTree(items,totals){
  const nOf=(v,w)=>(v||0)+' '+w+((v||0)===1?'':'s');
  root.querySelector('#quirq-file-count').textContent=nOf(totals.files,'file')+' · '+nOf(totals.directories,'folder');
  const target=root.querySelector('#quirq-tree');
  if(!items.length){
    target.innerHTML='<div class="quirq-empty">The Quirq state directory is empty or unavailable.</div>';
    return;
  }
  target.innerHTML=items.map(item=>{
    const indent=Math.min(item.depth||0,5)*18;
    return '<div class="quirq-file-row '+(item.kind==='directory'?'is-directory':'')+(item.sensitive?' is-sensitive':'')+'">'
      +'<div style="padding-left:'+indent+'px"><i>'+(item.kind==='directory'?'▾':'·')+'</i><code>'+esc(item.name)+'</code>'
        +(item.sensitive?'<em>masked</em>':'')+'</div>'
      +'<span>'+esc(item.description)+'</span>'
      +'<span>'+esc(item.kind==='directory'?'—':formatBytes(item.size_bytes))+'</span>'
      +'<time title="'+esc(item.modified_at)+'">'+esc(relativeTime(item.modified_at))+'</time>'
    +'</div>';
  }).join('')+(totals.truncated?'<div class="quirq-empty">Tree capped at 500 entries.</div>':'');
}

function renderFailure(message){
  root.querySelector('#quirq-path').className='quirq-path is-warn';
  root.querySelector('#quirq-path').innerHTML='<div><span>State unavailable</span><b>'+esc(message||'Could not inspect .quirq')+'</b></div>';
  root.querySelector('#quirq-tree').innerHTML='<div class="quirq-empty">Reconnect to the local server and refresh.</div>';
}

function formatBytes(bytes){
  const value=Number(bytes)||0;
  if(value<1024)return value+' B';
  if(value<1024*1024)return (value/1024).toFixed(value<10240?1:0)+' KB';
  return (value/1024/1024).toFixed(1)+' MB';
}

function relativeTime(value){
  if(!value)return 'Never';
  const time=new Date(value).getTime();
  if(!Number.isFinite(time))return 'Unknown';
  const seconds=Math.max(0,Math.round((Date.now()-time)/1000));
  if(seconds<60)return seconds+'s ago';
  if(seconds<3600)return Math.floor(seconds/60)+'m ago';
  if(seconds<86400)return Math.floor(seconds/3600)+'h ago';
  return Math.floor(seconds/86400)+'d ago';
}

function pretty(value){
  return String(value||'Not configured').split('_').map(
    part=>part?part[0].toUpperCase()+part.slice(1):''
  ).join(' ');
}

/* Health: GET /api/doctor (services/doctor). One finding kind carries an
   action: moving a leftover runtime folder into quarantine/, confirmed inline. */
async function loadHealth(){
  if(healthLoading||!root)return;
  healthLoading=true;
  const button=root.querySelector('#quirq-health-run');
  button.disabled=true;
  button.textContent='Checking…';
  const response=await apiFetch('/api/doctor');
  healthLoading=false;
  button.disabled=false;
  button.textContent='Run checks';
  if(!response.ok){
    renderHealthFailure(failText(response));
    return;
  }
  lastHealth=response.data;
  renderHealth(lastHealth);
}

function renderHealth(report){
  const level=report.level||'OK';
  const badge=root.querySelector('#quirq-health-level');
  badge.textContent=level==='OK'?'Healthy':level;
  badge.className='is-'+level.toLowerCase();
  const findings=(report.checks||[]).flatMap(check=>check.error
    ?[{id:check.id,key:'error:'+check.id,level:'ERROR',subject:check.id,path:'',
       observed:'This check could not run: '+check.error,why_it_matters:'This part of the state was not checked.'}]
    :(check.findings||[]).filter(finding=>finding.level!=='OK'));
  root.querySelector('#quirq-health-list').innerHTML=findings.length
    ?findings.map(healthRow).join('')
    :'<div class="quirq-empty">No problems found. Checked '+esc(relativeTime(report.checked_at))+'.</div>';
}

function healthRow(finding){
  const confirming=finding.action&&pendingMove===finding.subject;
  const action=!finding.action?''
    :confirming
      ?'<div class="quirq-health-confirm"><p>Move this folder into quarantine? You can move it back by hand.</p>'
        +'<button type="button" data-move-confirm="'+esc(finding.subject)+'">Move aside</button>'
        +'<button type="button" data-move-cancel>Cancel</button>'
        +'<em id="quirq-health-move-error"></em></div>'
      :'<button type="button" data-move-aside="'+esc(finding.subject)+'">Move aside…</button>';
  return '<div class="quirq-health-row is-'+esc(String(finding.level).toLowerCase())+'" data-finding="'+esc(finding.key)+'">'
    +'<div><span>'+esc(finding.level)+' · '+esc(finding.id)+'</span>'
      +'<b>'+esc(finding.observed)+'</b>'
      +'<p>'+esc(finding.why_it_matters)+'</p>'
      +(finding.path?'<code>'+esc(finding.path)+'</code>':'')
    +'</div>'+action
  +'</div>';
}

async function handleHealthClick(event){
  const start=event.target.closest('[data-move-aside]');
  if(start){pendingMove=start.dataset.moveAside;renderHealth(lastHealth);return;}
  if(event.target.closest('[data-move-cancel]')){pendingMove=null;renderHealth(lastHealth);return;}
  const confirm=event.target.closest('[data-move-confirm]');
  if(!confirm||confirm.disabled)return;
  confirm.disabled=true;
  const key=confirm.dataset.moveConfirm;
  const response=await apiFetch('/api/doctor/runtime-leftovers/'+encodeURIComponent(key)+'/move-aside',{method:'POST',body:{}});
  if(response.ok&&response.data?.moved===true&&response.data?.key===key){
    pendingMove=null;
    toast('Moved to '+response.data.to);
    await loadHealth();
    return;
  }
  confirm.disabled=false;
  const error=root.querySelector('#quirq-health-move-error');
  if(error)error.textContent=response.ok?'The move could not be confirmed. Run checks again.':failText(response);
}

function renderHealthFailure(message){
  const badge=root.querySelector('#quirq-health-level');
  badge.textContent='—';
  badge.className='';
  root.querySelector('#quirq-health-list').innerHTML='<div class="quirq-empty">Health checks unavailable: '+esc(message)+'</div>';
}
