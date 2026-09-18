/* Connectors section: workspace integrations and account apps inside Setup.

   Account apps run on the user's OWN Composio API key, stored on this machine
   (bring your own key). GET /api/connectors/composio/backend says whether a key
   is configured; without one the tiles read NEEDS_KEY and the key panel is the
   only call to action. No XO sign-in and no session header are involved. The
   browser never sees the key: it is injected server-side by the MCP proxy.

   Two independent states per card, and the UI has to keep them apart:
     - connected      -> the ACCOUNT holds a connection (shared by every workspace)
     - enabled here   -> THIS workspace has turned it on
   A card can be connected and off, which is the normal state for a workspace that
   did not run the OAuth flow itself. Hence two controls: "Turn off here" edits
   only this workspace, "Delete connection" removes it account-wide.
   A third, read-only fact per connected card is WHICH account the session is
   bound to (an email for Gmail and Google Calendar): read with the grid from
   GET /api/connections, resolved live through POST /api/connections/<id>/account
   when the read had none, and shown as a chip. Never a control, never blocking.

   Two failure modes are first-class states, not errors to hide:
     - no key configured  -> connectors inactive; the key panel is shown
     - no auth config     -> that one toolkit 422s on connect (rare: we create
                             a Composio-managed auth config on first connect)

   Connect opens the provider in a popup. The callback page posts back to its
   opener, but it posts to "*", so the listener below verifies the origin. A
   popup can also be blocked or dismissed silently, so the postMessage is only
   an accelerator: the status poll is what actually decides.

   Every call goes through API_BASE like the rest of the UI (same-origin under
   /space/, the dev fallback otherwise). core/api.js is imported bare, the
   same specifier every other view uses, so the browser holds one instance of
   the fetch layer; the escape, the relative time and the polling wording come
   from core too (core/connections.js is shared with the Inbox). */
import {API_BASE,apiFetch} from '../core/api.js';
import {esc,toast} from '../core/ui.js';
import {pollLine} from '../core/connections.js';
import {accountLabel,accountLine} from '../core/connections.js';
import {mountNativeConnectors} from './native-connectors.js?v=20260914-connectors2';

const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const cap=s=>s.charAt(0).toUpperCase()+s.slice(1);

const BASE=API_BASE+'/api/connectors/composio';
const POLL_ATTEMPTS=150;   /* 150 x 2s = 5 min, the usual provider consent window */
const POLL_INTERVAL=2000;

let root=null;
let toolkits=[];
let openToolkit=null;      /* id of the expanded action drawer, if any */
let toolsCache={};         /* toolkit id -> action rows */
let loading=false;
let listener=null;
let filter='';
let nativeConnectors=null;
let keyState={mode:'inactive',key_source:null,dynamic:false};   /* GET .../backend */
let keyReplacing=false;   /* Replace pressed: show the input over a configured key */
/* Browse-all (dynamic mode): the catalog is paged in on demand, never all at once. */
let browseCursor=null;    /* next_cursor from the last /catalog page */
let browseQuery='';       /* current search text */
let browseCategory='';    /* selected category id, '' = all */
let browseCats=null;      /* [{id,name}] once fetched; null = not yet */
let browseLoading=false;
let browseDebounce=null;
let browseLoaded=false;   /* has the first page been fetched for this mount */
/* Composio returns ~40 categories, many near-duplicate; showing them all makes the
   page scroll forever. Surface a short, high-value set (curated order first), deduped
   by name and capped; everything else stays reachable through search. */
const CAT_PRIORITY=['popular','productivity & project management','collaboration & communication',
  'crm','marketing & social media','sales & customer support','ai & machine learning',
  'analytics & data','scheduling & booking','developer tools'];
const MAX_CATEGORY_CHIPS=10;

/* Polling drawer (spec: connections polling). Same shape as the Actions drawer:
   one open id, one cache. The connections routes are workspace-local files under
   .quirq, not Composio, so these calls carry no session header. */
let openPolling=null;      /* id of the expanded Polling drawer, if any */
let pollCache={};          /* toolkit id -> GET /api/connections/<id> payload; null on failure */
let pollNotes={};          /* toolkit id -> one-line result of the last "Poll now" */
/* The drawer is an uncontrolled form: until Save its state lives only in the
   DOM, and the grid is rebuilt by Refresh, by the Actions toggle of any card
   and by a connect landing. So every grid paint first reads the open drawer's
   form into a draft, the drawer is painted from the draft when one exists,
   and Save or closing the drawer (Hide polling, opening another toolkit's
   drawer, turning the toolkit off, deleting the connection) discards it. The
   paint right after Save reads the server's copy, not the form. */
let pollDraft={};          /* toolkit id -> {enabled, interval_s, collectors} not yet saved */
const INTERVALS=[[300,'5 min'],[900,'15 min'],[1800,'30 min'],[3600,'1 hour'],
  [21600,'6 hours'],[86400,'24 hours']];

/* Account labels (spec: connected account name). Filled once per grid load
   from GET /api/connections, never per card; a connected toolkit turned on
   here that the read left unlabelled is asked once per load through
   POST /api/connections/<id>/account, and a label that arrives is written
   into its card in place. Both calls are workspace-local: no session header. */
let accountCache={};       /* toolkit id -> {account_label, account_checked_at} */
let accountAsked=new Set(); /* ids POSTed this load; reset by every load (Refresh included) */
const accountInFlight=new Set(); /* ids with a POST in flight, kept across loads: one request per toolkit */

export default {
  id:'connectors',label:'Connectors',order:10,
  toolbar:{search:{
    placeholder:'Filter connectors…',
    getValue:()=>filter,
    setValue(value){filter=String(value??'');applyFilter();},
  }},
  async mount(el){
    root=el;
    renderShell();
    nativeConnectors=mountNativeConnectors(root.querySelector('#conn-native-grid'),{onChange:applyFilter});
    bindEvents();
    await refreshAll();
  },
  show(){/* keep an in-flight authorization alive across tab switches */}
};

function renderShell(){
  root.innerHTML=
    '<div class="conn-page">'
      +'<header class="conn-hero">'
        +'<div>'
          +'<h2 id="setup-connectors-title" tabindex="-1">Connectors</h2>'
          +'<p>Tools and apps for this workspace.</p>'
        +'</div>'
        +'<div class="conn-hero-actions">'
          +'<button class="conn-refresh" id="conn-refresh" type="button">Refresh</button>'
        +'</div>'
      +'</header>'
      +'<section class="conn-group" id="conn-workspace-section" aria-labelledby="conn-workspace-title">'
        +'<div class="conn-group-head"><div><h3 id="conn-workspace-title">Workspace integrations</h3>'
          +'<p>Code, design, deployments, and files.</p></div></div>'
        +'<div class="conn-grid" id="conn-native-grid"></div>'
      +'</section>'
      +'<section class="conn-group" id="conn-account-section" aria-labelledby="conn-account-title">'
        +'<div class="conn-group-head"><div><h3 id="conn-account-title">Account apps</h3>'
          +'<p>Connect once to your XO account, then enable per workspace.</p></div>'
          +'<span class="conn-group-badge">Composio</span></div>'
        +'<div class="conn-key" id="conn-key"></div>'
        +'<div class="conn-alert" id="conn-alert" hidden></div>'
        +'<div class="conn-grid" id="conn-grid" aria-label="Composio toolkits">'
          +'<div class="conn-empty">Loading apps&hellip;</div>'
        +'</div>'
      +'</section>'
      +'<section class="conn-group" id="conn-browse-section" aria-labelledby="conn-browse-title" hidden>'
        +'<div class="conn-group-head"><div><h3 id="conn-browse-title">Browse all connectors</h3>'
          +'<p>Search Composio’s full catalog and connect anything you need.</p></div></div>'
        +'<input type="search" id="conn-browse-search" class="conn-browse-search" '
          +'placeholder="Search connectors…" autocomplete="off" spellcheck="false">'
        +'<div class="conn-chips" id="conn-browse-cats" role="group" '
          +'aria-label="Filter by category"></div>'
        +'<div class="conn-grid" id="conn-browse-grid"></div>'
        +'<div class="conn-browse-more" id="conn-browse-more" hidden>'
          +'<button class="conn-secondary" data-browse="more" type="button">Load more</button></div>'
      +'</section>'
      +'<div class="conn-empty" id="conn-no-match" role="status" hidden></div>'
    +'</div>';
}

function bindEvents(){
  root.querySelector('#conn-refresh').addEventListener('click',refreshAll);
  root.querySelector('#conn-grid').addEventListener('click',handleGridAction);
  const keyEl=root.querySelector('#conn-key');
  keyEl.addEventListener('click',ev=>{
    const b=ev.target.closest('button[data-action]');
    if(!b)return;
    if(b.dataset.action==='key-save')saveKey();
    else if(b.dataset.action==='key-remove')removeKey();
    else if(b.dataset.action==='key-replace'){keyReplacing=true;renderKeyPanel();}
    else if(b.dataset.action==='key-cancel'){keyReplacing=false;renderKeyPanel();}
  });
  keyEl.addEventListener('keydown',ev=>{
    if(ev.key==='Enter'&&ev.target.id==='conn-key-input'){ev.preventDefault();saveKey();}
  });
  const search=root.querySelector('#conn-browse-search');
  search.addEventListener('input',ev=>{
    const q=ev.target.value;
    clearTimeout(browseDebounce);
    browseDebounce=setTimeout(()=>browseSearch(q),250);
  });
  const browseGrid=root.querySelector('#conn-browse-grid');
  browseGrid.addEventListener('click',handleBrowseAction);
  root.querySelector('#conn-browse-cats').addEventListener('click',handleCategoryClick);
  root.querySelector('#conn-browse-more').addEventListener('click',ev=>{
    if(ev.target.closest('button[data-browse="more"]'))browseLoadMore();
  });
  if(!listener){
    listener=onAuthMessage;
    addEventListener('message',listener);
  }
}

/* ---------- loading ---------- */

async function refreshAll(){
  /* Workspace integrations do not depend on an XO or Composio session. */
  await Promise.all([nativeConnectors.refresh(),loadAll()]);
}

async function loadAll(){
  if(loading)return;
  loading=true;
  setAlert(null);
  try{
    /* Which mode are we in? A key (env or local file) activates connectors; without
       one they are inactive and the key panel is the only call to action. */
    const backend=await apiFetch(BASE+'/backend');
    keyState=(backend.ok&&backend.data)||{mode:'inactive',key_source:null,dynamic:false};
    renderKeyPanel();
    updateBrowseVisibility();

    /* the account labels ride alongside the listing; awaited before the
       paint so the cards come up labelled, never awaited past a failure */
    accountAsked=new Set();
    const accounts=loadAccounts();

    /* Listing also starts the server's MCP-gateway sweep in the background (only
       when a key is set), so opening this tab does what the old "Reinstall MCP
       gateway" button did: the agent's wiring is never installed by hand. */
    const list=await apiFetch(BASE+'/toolkits');

    if(!list.ok){renderListFailure(list);return;}
    toolkits=(list.data&&list.data.toolkits)||[];
    await accounts;
    renderGrid();
    askAccounts();
  }finally{
    loading=false;
  }
}

const KEY_ICON='<span class="conn-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" '
  +'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
  +'<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2 20 3m-3 3 2 2m-4 0 2 2"/></svg></span>';

function renderKeyPanel(){
  const el=root.querySelector('#conn-key');
  if(!el)return;
  const configured=keyState.mode==='local';
  const fromEnv=keyState.key_source==='env';
  const showInput=!configured||keyReplacing;

  const pill=configured
    ? '<span class="conn-state is-good">Configured</span>'
    : '<span class="conn-state is-pending">Not set</span>';

  const sub=configured
    ? 'Connectors are active. Key held on this machine only'
      +(fromEnv?', from <code>COMPOSIO_BYO_API_KEY</code>.':', in a private file.')
    : 'Add your Composio API key to activate connectors. It is stored only on this '
      +'machine and never sent to XO.';

  let form='';
  if(showInput){
    form='<div class="conn-key-form">'
      +'<input type="password" id="conn-key-input" autocomplete="off" spellcheck="false" '
      +'placeholder="Paste your Composio API key">'
      +'<button type="button" class="conn-primary" data-action="key-save">Save key</button>'
      +(keyReplacing?'<button type="button" class="conn-secondary" data-action="key-cancel">Cancel</button>':'')
      +'</div>';
  }else if(configured&&!fromEnv){
    form='<div class="conn-key-form">'
      +'<button type="button" class="conn-secondary" data-action="key-replace">Replace</button>'
      +'<button type="button" class="conn-secondary is-danger" data-action="key-remove">Remove</button>'
      +'</div>';
  }

  el.className='conn-key'+(configured?' is-set':'');
  el.innerHTML='<div class="conn-key-head">'+KEY_ICON
    +'<div class="conn-key-text"><h4>Composio API key</h4><p>'+sub+'</p></div>'
    +pill+'</div>'+form;

  const input=el.querySelector('#conn-key-input');
  if(input&&showInput)input.focus();
}

async function saveKey(){
  const input=root.querySelector('#conn-key-input');
  const api_key=input?input.value.trim():'';
  if(!api_key){setAlert('error','No key entered','Paste your Composio API key first.');return;}
  const res=await apiFetch(BASE+'/api-key',{method:'PUT',body:{api_key}});
  if(!res.ok){
    setAlert('error','Could not save the key',
      res.status===422?'Composio rejected this API key.':esc(res.error||'Try again.'));
    return;
  }
  setAlert(null);
  keyReplacing=false;
  toast('Composio API key saved');
  await refreshAll();
}

async function removeKey(){
  const res=await apiFetch(BASE+'/api-key',{method:'DELETE'});
  if(!res.ok){setAlert('error','Could not remove the key',esc(res.error||'Try again.'));return;}
  toast('Composio API key removed');
  await refreshAll();
}

/* ---------- browse all (dynamic mode) ---------- */

/* Show the catalog browser only in dynamic mode with a key; load its first page
   once. The catalog is paged on demand, never the whole ~1500-toolkit list. */
function updateBrowseVisibility(){
  const section=root.querySelector('#conn-browse-section');
  if(!section)return;
  const on=keyState.mode==='local'&&keyState.dynamic===true;
  section.hidden=!on;
  if(on&&!browseLoaded){browseLoaded=true;loadBrowseCats();browseSearch('');}
}

/* Category chips: one bounded /catalog/categories call (server-side TTL cache), then
   an "All" chip plus one per category. Failure is silent; search still works. */
async function loadBrowseCats(){
  if(browseCats!==null)return;
  const res=await apiFetch(BASE+'/catalog/categories');
  browseCats=(res.ok&&res.data&&Array.isArray(res.data.categories))?res.data.categories:[];
  renderCategoryChips();
}

/* Dedupe by name, order curated names first, cap the rest. Stable sort keeps the
   upstream order among non-curated ones. */
function topCategories(){
  const seen=new Set();
  const uniq=[];
  for(const c of browseCats||[]){
    const name=String(c.name||c.id||'').trim();
    const key=name.toLowerCase();
    if(!key||seen.has(key))continue;
    seen.add(key);
    uniq.push({id:c.id,name,rank:CAT_PRIORITY.indexOf(key)});
  }
  uniq.sort((a,b)=>(a.rank<0?CAT_PRIORITY.length:a.rank)-(b.rank<0?CAT_PRIORITY.length:b.rank));
  return uniq.slice(0,MAX_CATEGORY_CHIPS);
}

function renderCategoryChips(){
  const row=root.querySelector('#conn-browse-cats');
  if(!row)return;
  const cats=topCategories();
  if(cats.length===0){row.innerHTML='';return;}
  const chip=(id,name)=>'<button class="conn-chip" type="button" data-cat="'+esc(id)
    +'" aria-pressed="'+(browseCategory===id?'true':'false')+'">'+esc(name)+'</button>';
  row.innerHTML=chip('','All')+cats.map(c=>chip(c.id,c.name)).join('');
}

function handleCategoryClick(event){
  const btn=event.target.closest('button[data-cat]');
  if(!btn)return;
  const id=btn.dataset.cat||'';
  if(id===browseCategory)return;
  browseCategory=id;
  renderCategoryChips();
  browseSearch(browseQuery);
}

async function browseSearch(query){
  browseQuery=String(query||'').trim();
  browseCursor=null;
  await loadBrowsePage(false);
}

async function browseLoadMore(){
  if(browseCursor)await loadBrowsePage(true);
}

async function loadBrowsePage(append){
  if(browseLoading)return;
  browseLoading=true;
  const grid=root.querySelector('#conn-browse-grid');
  if(!append)grid.innerHTML='<div class="conn-empty">Searching&hellip;</div>';
  try{
    let path=BASE+'/catalog?limit=24';
    if(browseQuery)path+='&search='+encodeURIComponent(browseQuery);
    if(browseCategory)path+='&category='+encodeURIComponent(browseCategory);
    if(append&&browseCursor)path+='&cursor='+encodeURIComponent(browseCursor);
    const res=await apiFetch(path);
    if(!res.ok||!res.data){
      grid.innerHTML='<div class="conn-empty is-error">'+esc(res.error||'Could not load the catalog.')+'</div>';
      return;
    }
    browseCursor=res.data.next_cursor||null;
    renderBrowse(res.data.items||[],append);
  }finally{
    browseLoading=false;
    const more=root.querySelector('#conn-browse-more');
    if(more)more.hidden=!browseCursor;
  }
}

function renderBrowse(items,append){
  const grid=root.querySelector('#conn-browse-grid');
  if(!append&&items.length===0){
    const what=browseQuery?'&ldquo;'+esc(browseQuery)+'&rdquo;':'this filter';
    grid.innerHTML='<div class="conn-empty">No connectors match '+what+'.</div>';
    return;
  }
  const html=items.map(browseCardHTML).join('');
  if(append)grid.insertAdjacentHTML('beforeend',html);
  else grid.innerHTML=html;
}

function browseCardHTML(t){
  const logo=t.logo
    ? '<img class="conn-icon" src="'+esc(t.logo)+'" alt="" loading="lazy" width="36" height="36">'
    : '<span class="conn-icon" aria-hidden="true">'+esc((t.name||t.slug||'?').slice(0,1).toUpperCase())+'</span>';
  const tag=t.managed_auth?'':'<span class="conn-fact">API key</span>';
  return '<article class="conn-card" data-browse-slug="'+esc(t.slug)+'">'
    +'<div class="conn-card-head"><div class="conn-card-heading">'+logo
      +'<div class="conn-card-id"><h3>'+esc(t.name||t.slug)+'</h3>'
      +'<span>'+esc((t.categories&&t.categories[0]&&t.categories[0].name)||'')+'</span></div></div></div>'
    +'<div class="conn-card-body"><div class="conn-facts">'
      +'<span class="conn-fact">'+(Number(t.tools_count)||0)+' tools</span>'+tag+'</div>'
      +'<div class="conn-card-error" id="berr-'+esc(t.slug)+'" hidden></div>'
      +'<div class="conn-browse-form" id="bform-'+esc(t.slug)+'"></div></div>'
    +'<div class="conn-card-acts">'
      +'<button class="conn-primary" data-browse="connect" type="button">Connect</button></div>'
    +'</article>';
}

function handleBrowseAction(event){
  const card=event.target.closest('[data-browse-slug]');
  if(!card)return;
  const slug=card.dataset.browseSlug;
  const btn=event.target.closest('button[data-browse]');
  if(btn&&btn.dataset.browse==='connect')connectCatalog(slug);
  else if(btn&&btn.dataset.browse==='connect-custom')submitCustomAuth(slug);
}

function browseError(slug,msg){
  const el=root.querySelector('#berr-'+CSS.escape(slug));
  if(!el)return;
  if(!msg){el.hidden=true;el.textContent='';return;}
  el.textContent=msg;el.hidden=false;
}

async function connectCatalog(slug){
  browseError(slug,'');
  const res=await apiFetch(BASE+'/'+encodeURIComponent(slug)+'/auth-fields');
  if(!res.ok||!res.data){browseError(slug,res.error||'Could not read this connector.');return;}
  if(res.data.managed_auth){
    connect(slug);   /* one-click: same popup+poll flow as a featured card */
    return;
  }
  renderCustomAuthForm(slug,res.data);
}

function renderCustomAuthForm(slug,detail){
  const scheme=(detail.auth_schemes&&detail.auth_schemes[0])||'API_KEY';
  const fields=(detail.fields&&detail.fields.required)||[];
  const form=root.querySelector('#bform-'+CSS.escape(slug));
  if(!form)return;
  form.dataset.scheme=scheme;
  form.innerHTML=fields.map(f=>
    '<label class="conn-browse-field">'+esc(f.display_name||f.name)
    +'<input data-field="'+esc(f.name)+'" type="'+(f.is_secret?'password':'text')+'" '
    +'autocomplete="off"'+(f.required?' required':'')+'></label>').join('')
    +'<button class="conn-primary" data-browse="connect-custom" type="button">Save &amp; connect</button>';
}

async function submitCustomAuth(slug){
  browseError(slug,'');
  const form=root.querySelector('#bform-'+CSS.escape(slug));
  if(!form)return;
  const scheme=form.dataset.scheme||'API_KEY';
  const credentials={};
  form.querySelectorAll('input[data-field]').forEach(i=>{credentials[i.dataset.field]=i.value.trim();});
  const res=await apiFetch(BASE+'/'+encodeURIComponent(slug)+'/connect',{
    method:'POST',body:{auth_scheme:scheme,credentials},
  });
  if(!res.ok){browseError(slug,connectErrorText(res,slug));return;}
  if(res.data&&res.data.auth_url){
    const popup=window.open(res.data.auth_url,'_blank','noopener,width=560,height=760');
    if(!popup)browseError(slug,'Allow the popup to finish authorizing.');
    await pollUntilConnected(slug,res.data.connection_request_id,popup);
  }else{
    toast(labelFor(slug)+' connected');
    await refreshAll();
  }
}

/* The /toolkits route is the only source of the toolkit list, so when it fails
   there are no tiles to draw. It returns 200 even with no key (tiles read
   NEEDS_KEY), so a failure here is offline or a genuine server fault. */
function renderListFailure(res){
  let note;
  if(res.offline){
    setAlert('error','xo-space is unreachable','The server is down or restarting.');
    note='Cannot reach the server.';
  }else if(res.status===500){
    setAlert('error','Could not load account apps',
      'Check the xo-space server log for the cause, then refresh.');
    note='Account apps are temporarily unavailable.';
  }else{
    setAlert('error','Could not list connectors',esc(res.error||''));
    note=res.error||'Unavailable.';
  }
  paintGrid(()=>'<div class="conn-empty is-error">'+esc(note)+'</div>');
}

/* ---------- rendering ---------- */

function isConnected(t){return String(t.status||'').toUpperCase()==='ACTIVE';}
function schemeOf(toolkitId){
  const t=toolkits.find(x=>x.id===toolkitId);
  return (t&&Array.isArray(t.schemes)&&t.schemes[0])||'OAUTH2';
}
/* what a person is asked for when they press Connect */
const SCHEME_LABEL={OAUTH2:'OAuth sign-in',API_KEY:'API key or bot token',BEARER_TOKEN:'access token',BASIC:'username and password'};
const schemeLabel=s=>SCHEME_LABEL[String(s||'').toUpperCase()]||String(s||'');
function isEnabledHere(t){return !!t.workspace_enabled;}

/* Local artwork keeps the directory recognizable without third-party image
   requests. Unknown server-provided apps still receive a useful letter mark. */
const APP_ART={
  gmail:['#ed8b84','<path d="M4 7h16v12H4z"/><path d="m4 7 8 6 8-6"/>','Email and inbox'],
  googlecalendar:['#85acf2','<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M8 3v4m8-4v4M4 10h16M8 14h2m4 0h2m-8 4h2"/>','Events and calendars'],
  notion:['#d7d4ce','<path d="M6 19V5l12 14V5"/>','Notes, pages, and databases'],
  googlesheets:['#7dc69b','<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M5 9h14M5 15h14M12 9v12"/>','Spreadsheets and data'],
  googledocs:['#85acf2','<path d="M6 3h9l4 4v14H6zM15 3v5h4M9 12h7m-7 4h7"/>','Documents and text'],
  googleslides:['#e9c871','<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 9h8v7H8z"/>','Presentations and slides'],
  googlemeet:['#7dc69b','<rect x="3" y="6" width="12" height="12" rx="2"/><path d="m15 10 6-4v12l-6-4"/>','Meetings and recordings'],
  figma:['#bf9cea','<path d="M12 4H8a4 4 0 0 0 0 8h4zm0 0h4a4 4 0 0 1 0 8h-4zm0 8H8a4 4 0 0 0 0 8 4 4 0 0 0 4-4z"/><circle cx="16" cy="16" r="4"/>','Design files and comments'],
  slack:['#cd9fc6','<path d="M9 3v14M15 7v14M3 15h14M7 9h14"/>','Channels and team messages'],
  telegram:['#81b9dd','<path d="m3 11 18-7-5 17-5-7-8-3zm8 3L21 4"/>','Bot messages and chats'],
};
function appIcon(t){
  const art=APP_ART[t.id];
  return'<span class="conn-icon" aria-hidden="true"'+(art?' style="color:'+art[0]+'"':'')+'>'
    +(art?'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'+art[1]+'</svg>'
      :esc((t.display_name||t.id||'?').slice(0,1).toUpperCase()))+'</span>';
}

/* Connections are account-wide; reach is not. A toolkit connected on the account but
   not enabled here is the normal state for a workspace that did not run the OAuth
   flow, so it gets its own label rather than reading as broken. */
function statusOf(t){
  if(!isConnected(t))return{text:'Not connected',cls:''};
  if(!isEnabledHere(t))return{text:'Off in this workspace',cls:'is-idle'};
  return{text:'On in this workspace',cls:'is-good'};
}

/* Every write to the grid goes through here. The open Polling drawer's
   unsaved form is read into its draft FIRST, and only then is build() run:
   renderCard paints the drawer from that draft, so building before the
   snapshot would paint the previous snapshot and file the fresh edits away
   for the paint after (a form that flips between edited and saved values
   on every repaint). The one paint that must not read the form is the one
   right after Save: the form in the DOM is the pre-save one and the
   server's copy is the truth, so savePolling passes snapshot:false. */
function paintGrid(build,{snapshot=true}={}){
  if(snapshot)snapshotPollDraft();
  root.querySelector('#conn-grid').innerHTML=build();
  applyFilter();
}
function renderGrid(opts){
  if(!toolkits.length){
    paintGrid(()=>'<div class="conn-empty">No toolkits are registered on this server.</div>',opts);
    return;
  }
  paintGrid(()=>toolkits.map(renderCard).join(''),opts);
}

/* Filter in place: replacing cards while someone authorizes, saves a poll
   draft or toggles an action would detach its controls and lose live state. */
function applyFilter(){
  if(!root)return;
  const q=filter.trim().toLowerCase();
  const cards=root.querySelectorAll('.conn-card[data-toolkit]');
  let shown=0;
  for(const card of cards){
    const t=toolkits.find(t=>t.id===card.dataset.toolkit);
    const fields=t?[t.id,t.slug,t.display_name,t.description,APP_ART[t.id]?.[2],
      isConnected(t)?accountLabel(accountCache[t.id]):'']:[];
    card.hidden=!!q&&!fields.some(value=>String(value??'').toLowerCase().includes(q));
    if(!card.hidden)shown++;
  }
  const native=nativeConnectors?.setFilter(filter)||{total:0,shown:0};
  root.querySelector('#conn-workspace-section').hidden=!!q&&native.shown===0;
  root.querySelector('#conn-account-section').hidden=!!q&&shown===0;
  const note=root.querySelector('#conn-no-match');
  note.hidden=!(cards.length+native.total)||shown+native.shown>0;
  note.textContent=note.hidden?'':'No connectors match “'+filter.trim()+'”. Clear the search to show all connectors.';
}

function renderCard(t){
  const connected=isConnected(t);
  const enabled=isEnabledHere(t);
  const status=statusOf(t);
  const open=openToolkit===t.id;
  const polling=openPolling===t.id;
  const acct=accountLabel(accountCache[t.id]);
  return'<article class="conn-card'+(connected&&enabled?' is-on':'')+'" data-toolkit="'+esc(t.id)+'">'
    +'<div class="conn-card-head">'
      +'<div class="conn-card-heading">'+appIcon(t)
        +'<div class="conn-card-id"><h3>'+esc(t.display_name||t.id)+'</h3>'
          +'<span>'+esc((t.schemes||['OAUTH2']).map(schemeLabel).join(', '))+'</span>'
        +'</div>'
      +'</div>'
      +'<i class="conn-state '+status.cls+'">'+esc(status.text)+'</i>'
    +'</div>'
    +'<div class="conn-card-body">'
      +'<p class="conn-card-description">'+esc(t.description||APP_ART[t.id]?.[2]||'Account app integration')+'</p>'
      +'<div class="conn-facts">'
        +(t.account_count>1?'<span class="conn-fact">'+t.account_count+' accounts</span>':'')
        /* which account the session is bound to; only a connection has one */
        +(connected&&acct
          ?'<span class="conn-fact conn-account" title="the account this workspace uses">'+esc(acct)+'</span>'
          :'')
      +'</div>'
      +(connected&&!enabled
        ?'<p class="conn-card-note">Enable it to use this account in this workspace.</p>'
        :'')
      +(!connected&&String(schemeOf(t.id)).toUpperCase()!=='OAUTH2'
        ?'<p class="conn-card-note">Connect opens a page that asks for the '+esc(schemeLabel(schemeOf(t.id)))
          +(t.id==='telegram'?' (the bot token BotFather gave you)':'')+'.</p>'
        :'')
      +'<div class="conn-card-error" id="err-'+esc(t.id)+'" role="alert" hidden></div>'
    +'</div>'
    +'<div class="conn-card-acts">'
      +(!connected
        ?'<button class="conn-primary" data-action="connect">Connect</button>'
        :(enabled
          ?'<button class="conn-secondary" data-action="unlink">Turn off here</button>'
          :'<button class="conn-primary" data-action="enable">Turn on here</button>'))
      /* Deleting is account-wide, so it is kept visually apart from the
         workspace-local toggle above and confirmed before it runs. */
      +(connected
        ?'<button class="conn-secondary is-danger" data-action="disconnect">'
          +'Delete connection&hellip;</button>'
        :'')
      +(connected&&enabled&&t.supports_action_prefs
        ?'<button class="conn-secondary" data-action="actions">'
          +(open?'Hide actions':'Actions')+'</button>'
        :'')
      /* Polling: connected and on here, or already open (the auto-opened drawer
         after a connect needs a way to close before the toolkit is on here). */
      +(connected&&(enabled||polling)
        ?'<button class="conn-secondary" data-action="polling">'
          +(polling?'Hide polling':'Polling')+'</button>'
        :'')
    +'</div>'
    +(open?renderActions(t.id):'')
    /* The drawer needs a connection, not "enabled here": a fresh connect opens
       it before the workspace has turned the toolkit on, and it says so. */
    +(polling&&connected?renderPolling(t,enabled):'')
    +'</article>';
}

/* ---------- polling ---------- */

function renderPolling(t,enabled){
  const c=pollCache[t.id];
  const wrap=inner=>'<div class="conn-poll" id="poll-'+esc(t.id)+'">'+inner+'</div>';
  if(c===undefined)return wrap('<div class="conn-empty">Loading polling settings&hellip;</div>');
  if(c===null)return wrap('<div class="conn-empty is-error">Could not load polling settings.</div>');
  const available=(Array.isArray(c.available_collectors)?c.available_collectors:[])
    .filter(a=>a&&typeof a==='object'&&typeof a.id==='string');
  if(!available.length)return wrap('<div class="conn-empty">No collectors available for this toolkit yet.</div>');
  /* unsaved edits (the draft) win; else an unconfigured connection starts
     from the catalog defaults and a configured one shows exactly what
     config.json says */
  const draft=pollDraft[t.id];
  const chosen=new Set(draft?draft.collectors
    :c.configured
      ?(Array.isArray(c.collectors)?c.collectors:[])
      :available.filter(a=>a.default).map(a=>a.id));
  const interval=draft?draft.interval_s:(Number(c.interval_s)||900);
  const collect=draft?draft.enabled:!!c.enabled;
  /* the drawer's own read carries the label; the card's cache covers a
     lookup that landed after the drawer loaded */
  const acct=accountLine(c)||accountLine(accountCache[t.id]);
  const options=INTERVALS.map(([s,label])=>
      '<option value="'+s+'"'+(s===interval?' selected':'')+'>'+label+'</option>').join('')
    /* a hand-edited interval outside the menu is kept, not silently rounded */
    +(INTERVALS.some(([s])=>s===interval)?''
      :'<option value="'+interval+'" selected>'+interval+' s (from config.json)</option>');
  return wrap(
    (enabled?'':'<p class="conn-poll-note">Turn it on here first: polling reads through this '
      +'workspace&rsquo;s connection.</p>')
    +'<p class="conn-poll-note">What the poller collects into the Inbox, and how often. '
      +'Nothing is saved until you press Save.</p>'
    +'<label class="conn-poll-row"><input type="checkbox" data-poll="enabled"'
      +(collect?' checked':'')+'> Collect into Inbox</label>'
    +'<label class="conn-poll-row">Every <select data-poll="interval">'+options+'</select></label>'
    +(acct?'<p class="conn-poll-note conn-poll-account">Polling '+esc(acct)+'</p>':'')
    +available.map(a=>
      '<label class="conn-poll-row"><input type="checkbox" data-poll="collector" value="'+esc(a.id)+'"'
        +(chosen.has(a.id)?' checked':'')+'> '+esc(a.label||a.id)+'</label>').join('')
    +pollStatus(t.id,c)
    +'<div class="conn-poll-acts">'
      +'<button class="conn-primary" data-action="poll-save">Save</button>'
      +'<button class="conn-secondary" data-action="poll-now"'
        +(c.configured?'':' disabled title="Save first"')+'>Poll now</button>'
    +'</div>');
}

/* one line: the last "Poll now" result (if any), then the last error in the
   error style, else the last poll time, else "Never polled"; the wording is
   core/connections.js's pollLine, the same line the Inbox shows */
function pollStatus(toolkitId,c){
  const note=pollNotes[toolkitId]?esc(pollNotes[toolkitId])+' &middot; ':'';
  const line=pollLine(c);
  if(line.error)return'<div class="conn-poll-status is-error">'+note+esc(line.error)+'</div>';
  const total=Number(c.events_total)||0;
  const when=esc(cap(line.text))
    +(c.last_poll_at&&total?' &middot; '+total+' collected so far':'');
  return'<div class="conn-poll-status">'+note+when+'</div>';
}

async function togglePolling(toolkitId){
  if(openPolling===toolkitId){
    openPolling=null;
    delete pollDraft[toolkitId]; /* closing the drawer discards unsaved edits */
    renderGrid();
    return;
  }
  closeOtherPolling(toolkitId);
  openPolling=toolkitId;
  renderGrid();
  if(pollCache[toolkitId]===undefined)await loadPolling(toolkitId);
  if(openPolling===toolkitId)renderGrid();
}

/* One drawer at a time: opening one closes whichever other was open, and
   that close discards the other's unsaved edits, the same as pressing its
   Hide polling would. Called before openPolling is reassigned. */
function closeOtherPolling(toolkitId){
  if(openPolling!==null&&openPolling!==toolkitId)delete pollDraft[openPolling];
}

async function loadPolling(toolkitId){
  const res=await apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId));
  pollCache[toolkitId]=res.ok&&res.data?res.data:null;
  return res;
}

/* the open drawer's form, read before a paint replaces it; a drawer still
   loading has no form and keeps whatever draft it had */
function snapshotPollDraft(){
  if(openPolling===null)return;
  const form=readPollForm(openPolling);
  if(form)pollDraft[openPolling]=form;
}

function readPollForm(toolkitId){
  const drawer=root.querySelector('#poll-'+CSS.escape(toolkitId));
  if(!drawer)return null;
  const enabled=drawer.querySelector('input[data-poll="enabled"]');
  const interval=drawer.querySelector('select[data-poll="interval"]');
  if(!enabled||!interval)return null;
  return{
    enabled:enabled.checked,
    interval_s:Number(interval.value),
    collectors:[...drawer.querySelectorAll('input[data-poll="collector"]:checked')].map(i=>i.value),
  };
}

async function savePolling(toolkitId,button){
  const body=readPollForm(toolkitId);
  if(!body)return;
  cardError(toolkitId,'');
  setBusy(button,true);
  try{
    const res=await apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId),{method:'PUT',body});
    if(!res.ok||!res.data){cardError(toolkitId,res.error||'Could not save polling settings.');return;}
    pollCache[toolkitId]=res.data;
    delete pollDraft[toolkitId]; /* saved: the server's copy is the form now */
    delete pollNotes[toolkitId];
    toast(labelFor(toolkitId)+' polling saved');
    /* painted from the server's copy (res.data), not from the form just
       submitted: that form is still in the DOM, and a snapshot would file it
       as a draft again and mask whatever the server normalised */
    if(openPolling===toolkitId)renderGrid({snapshot:false});
  }finally{
    setBusy(button,false);
  }
}

async function pollNow(toolkitId,button){
  cardError(toolkitId,'');
  setBusy(button,true);
  try{
    const res=await apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkitId)+'/poll',{method:'POST'});
    if(!res.ok||!res.data){cardError(toolkitId,res.error||'Poll failed.');return;}
    const r=res.data;
    pollNotes[toolkitId]=r.skipped?'Poll skipped ('+String(r.skipped)+')'
      :r.error?'Poll failed'
      :'Polled just now: '+(Number(r.new_events)||0)+' new';
    /* the server is the truth for last_poll_at and last_error: re-read, repaint */
    await loadPolling(toolkitId);
    if(openPolling===toolkitId)renderGrid();
  }finally{
    setBusy(button,false);
  }
}

/* ---------- accounts ---------- */

/* One read per grid load, never per card: the list route carries every
   toolkit's account_label, so the cards paint with whatever the server
   already knows. A failed read keeps the labels of the previous load. */
async function loadAccounts(){
  const path=API_BASE+'/api/connections';
  const res=await apiFetch(path);
  const rows=res.ok&&res.data&&Array.isArray(res.data.connections)?res.data.connections:null;
  if(!rows)return;
  const next={};
  for(const c of rows){
    if(!c||typeof c!=='object'||typeof c.toolkit!=='string')continue;
    next[c.toolkit]={account_label:accountLabel(c)||null,account_checked_at:c.account_checked_at||null};
  }
  accountCache=next;
}

/* A connected toolkit turned on here that the read left unlabelled gets one
   live lookup per load; the server answers from its cache for a minute, so
   a Refresh right after costs no provider call. Not awaited by loadAll: a
   slow provider must not hold the Refresh button. */
function askAccounts(){
  for(const t of toolkits){
    if(!isConnected(t)||!isEnabledHere(t)||accountLabel(accountCache[t.id]))continue;
    if(accountAsked.has(t.id)||accountInFlight.has(t.id))continue;
    accountAsked.add(t.id);
    resolveAccount(t.id);
  }
}

async function resolveAccount(toolkitId){
  accountInFlight.add(toolkitId);
  try{
    const path=API_BASE+'/api/connections/'+encodeURIComponent(toolkitId)+'/account';
    const res=await apiFetch(path,{method:'POST'});
    /* no retry: an error with no label (a provider fault, a toolkit with no
       lookup yet) leaves the card exactly as it is */
    const label=res.ok&&res.data?accountLabel(res.data):'';
    if(!label)return;
    accountCache[toolkitId]={account_label:label,account_checked_at:res.data.account_checked_at||null};
    paintAccount(toolkitId,label);
    applyFilter();
  }finally{
    accountInFlight.delete(toolkitId);
  }
}

/* The label lands in place, never through a grid repaint: a lookup answers
   at any moment after the load, and rebuilding the grid then would hide a
   card error a failed Save, Poll now or scope change just showed, and
   re-enable the button of a connect or delete still in flight (its later
   setBusy would reach a detached node). The next full paint reads
   accountCache and draws the same chip and note. Nothing to do when the
   card is gone or its toolkit is no longer connected (deleted meanwhile). */
function paintAccount(toolkitId,label){
  const toolkit=toolkits.find(t=>t.id===toolkitId);
  if(!toolkit||!isConnected(toolkit))return;
  const card=root.querySelector('.conn-card[data-toolkit="'+CSS.escape(toolkitId)+'"]');
  if(!card)return;
  const facts=card.querySelector('.conn-facts');
  if(facts&&!facts.querySelector('.conn-account'))
    facts.insertAdjacentHTML('beforeend',
      '<span class="conn-fact conn-account" title="the account this workspace uses">'+esc(label)+'</span>');
  /* an open drawer gets its "Polling as" note under the interval row, the
     spot renderPolling gives it */
  const drawer=card.querySelector('.conn-poll');
  if(!drawer||drawer.querySelector('.conn-poll-account'))return;
  const interval=drawer.querySelector('select[data-poll="interval"]');
  const row=interval&&interval.closest('label');
  if(row)row.insertAdjacentHTML('afterend',
    '<p class="conn-poll-note conn-poll-account">Polling '+esc(accountLine(accountCache[toolkitId]))+'</p>');
}

function renderActions(toolkitId){
  const rows=toolsCache[toolkitId];
  if(rows===undefined)return'<div class="conn-actions"><div class="conn-empty">Loading actions&hellip;</div></div>';
  if(rows===null)return'<div class="conn-actions"><div class="conn-empty is-error">Could not load actions.</div></div>';
  if(!rows.length)return'<div class="conn-actions"><div class="conn-empty">No actions available.</div></div>';
  return'<div class="conn-actions">'
    +'<p class="conn-actions-note">Turn an action off to keep it out of the '
      +'agent&rsquo;s toolset. Changes apply to your next turn.</p>'
    +rows.map(a=>
      '<label class="conn-action">'
        +'<input type="checkbox" data-action="toggle" data-slug="'+esc(a.slug)+'"'
          +(a.enabled?' checked':'')+'>'
        +'<span class="conn-action-name">'+esc(a.name||a.slug)+'</span>'
        +(a.category?'<span class="conn-tag is-'+esc(a.category)+'">'+esc(a.category)+'</span>':'')
      +'</label>').join('')
    +'</div>';
}

function setAlert(kind,title,detail){
  const el=root.querySelector('#conn-alert');
  if(!kind){el.hidden=true;el.innerHTML='';return;}
  el.className='conn-alert is-'+kind;
  el.hidden=false;
  el.innerHTML='<span aria-hidden="true">&#9670;</span><div><b>'+esc(title)+'</b>'
    +(detail?'<p>'+detail+'</p>':'')+'</div>';
}

function cardError(toolkitId,message){
  const el=root.querySelector('#err-'+CSS.escape(toolkitId));
  if(!el)return;
  if(!message){el.hidden=true;el.textContent='';return;}
  el.textContent=message;
  el.hidden=false;
}

function setBusy(button,busy){
  if(!button)return;
  button.disabled=busy;
  button.classList.toggle('is-busy',busy);
}

/* ---------- actions ---------- */

function handleGridAction(event){
  const input=event.target.closest('input[data-action="toggle"]');
  if(input){
    const card=input.closest('[data-toolkit]');
    toggleAction(card.dataset.toolkit,input);
    return;
  }
  const button=event.target.closest('button[data-action]');
  if(!button)return;
  const card=button.closest('[data-toolkit]');
  if(!card)return;
  const id=card.dataset.toolkit;
  if(button.dataset.action==='connect')connect(id,button);
  else if(button.dataset.action==='enable')setScope(id,true,button);
  else if(button.dataset.action==='unlink')setScope(id,false,button);
  else if(button.dataset.action==='disconnect')disconnect(id,button);
  else if(button.dataset.action==='actions')toggleDrawer(id);
  else if(button.dataset.action==='polling')togglePolling(id);
  else if(button.dataset.action==='poll-save')savePolling(id,button);
  else if(button.dataset.action==='poll-now')pollNow(id,button);
}

async function connect(toolkitId,button){
  cardError(toolkitId,'');
  setBusy(button,true);
  /* Opened before the await: a popup opened later is not tied to the click and
     is blocked by default in most browsers. */
  const popup=window.open('','composio-auth','width=560,height=760');
  try{
    /* The toolkit says how it authenticates (OAUTH2, or API_KEY for a bot token);
       the swarm's hosted page handles either, so the popup flow is the same. */
    const res=await apiFetch(BASE+'/'+encodeURIComponent(toolkitId)+'/connect',{
      method:'POST',body:{auth_scheme:schemeOf(toolkitId)},
    });
    if(!res.ok||!res.data||!res.data.auth_url){
      if(popup)popup.close();
      cardError(toolkitId,connectErrorText(res,toolkitId));
      return;
    }
    if(popup)popup.location=res.data.auth_url;
    else window.open(res.data.auth_url,'_blank','noopener');
    await pollUntilConnected(toolkitId,res.data.connection_request_id,popup);
  }finally{
    setBusy(button,false);
  }
}

function connectErrorText(res,toolkitId){
  if(res.status===422){
    /* /connect answers 422 for every unconfigured-server case, so match the
       detail before falling through to the auth-config wording. The literal
       COMPOSIO_CALLBACK_URL is load-bearing on the server side, like
       COMPOSIO_API_KEY above. */
    if(String(res.error||'').includes('COMPOSIO_CALLBACK_URL')){
      return'This server has no OAuth callback URL configured. Set '
        +'COMPOSIO_CALLBACK_URL to this deployment’s public callback URL '
        +'and register that origin as an allowed callback on the Composio auth '
        +'configs in the dashboard.';
    }
    return'This toolkit could not be set up automatically. Create an auth config '
      +'for it in your Composio dashboard, then try again.';
  }
  if(res.offline)return'xo-space is unreachable.';
  return res.error||'Could not start authorization.';
}

/* The popup's postMessage is an accelerator; this poll is the decision. It also
   covers a blocked popup, a closed tab, and a consent finished in another
   window. */
async function pollUntilConnected(toolkitId,requestId,popup){
  if(!requestId){cardError(toolkitId,'The server returned no request id.');return;}
  const path=BASE+'/'+encodeURIComponent(toolkitId)+'/status?connection_request_id='
    +encodeURIComponent(requestId);
  for(let attempt=0;attempt<POLL_ATTEMPTS;attempt+=1){
    await delay(POLL_INTERVAL);
    const res=await apiFetch(path);
    const status=String((res.data&&res.data.status)||'').toUpperCase();
    if(status==='ACTIVE'){
      if(popup&&!popup.closed)popup.close();
      toast(labelFor(toolkitId)+' connected');
      /* Open the Polling drawer for the toolkit that just connected, so the
         interval and the data to collect get picked right away. Set BEFORE
         loadAll(): it re-renders the grid, and a drawer flagged afterwards
         would be lost. Nothing is persisted until Save. */
      closeOtherPolling(toolkitId);
      openPolling=toolkitId;
      delete pollCache[toolkitId];
      await loadAll();
      await loadPolling(toolkitId);
      if(openPolling===toolkitId&&root.querySelector('.conn-card[data-toolkit]'))renderGrid();
      return;
    }
    if(status==='FAILED'){
      cardError(toolkitId,(res.data&&res.data.error)||'Authorization failed.');
      return;
    }
    if(popup&&popup.closed&&attempt>2){
      cardError(toolkitId,'The authorization window closed before it finished.');
      return;
    }
  }
  cardError(toolkitId,'Timed out waiting for authorization. Try again.');
}

/* Turning a toolkit on or off for THIS workspace only. Nothing is deleted, and no
   other workspace is affected: the whole reason connections became account-wide. */
async function setScope(toolkitId,enabled,button){
  const toolkit=toolkits.find(t=>t.id===toolkitId);
  if(!toolkit)return;
  cardError(toolkitId,'');
  setBusy(button,true);
  try{
    const path=BASE+'/'+encodeURIComponent(toolkitId)
      +(enabled?'/scope':'/accounts/'+encodeURIComponent(toolkit.connected_account_id||'')+'/unlink');
    const res=enabled
      ? await apiFetch(path,{method:'PUT',
          body:{enabled:true,
                connected_account_ids:[toolkit.connected_account_id].filter(Boolean)}})
      : await apiFetch(path,{method:'POST'});
    if(!res.ok){cardError(toolkitId,res.error||'Could not save that change.');return;}
    toast(labelFor(toolkitId)+(enabled?' on in this workspace':' off in this workspace'));
    if(!enabled&&openToolkit===toolkitId)openToolkit=null;
    if(!enabled&&openPolling===toolkitId)openPolling=null;
    if(!enabled)delete pollDraft[toolkitId]; /* the drawer closes with the toolkit */
    await loadAll();
  }finally{
    setBusy(button,false);
  }
}

async function disconnect(toolkitId,button){
  const toolkit=toolkits.find(t=>t.id===toolkitId);
  if(!toolkit||!toolkit.connected_account_id)return;
  /* Account-wide and irreversible, so the confirm has to say so: this is not the
     workspace-local "Turn off here" above. It deletes the connected account at
     Composio; it does not revoke the grant at the provider. */
  if(!confirm('Delete the '+labelFor(toolkitId)+' connection?\n\n'
    +'This removes it from EVERY workspace in your XO account, not just this one. '
    +'To stop using it here only, choose "Turn off here" instead.\n\n'
    +'You may also want to remove access in your '+labelFor(toolkitId)
    +' account settings.'))return;
  cardError(toolkitId,'');
  setBusy(button,true);
  try{
    const res=await apiFetch(BASE+'/'+encodeURIComponent(toolkitId)+'/disconnect',{
      method:'POST',
      body:{connected_account_id:toolkit.connected_account_id},
    });
    if(!res.ok){cardError(toolkitId,res.error||'Delete failed.');return;}
    toast(labelFor(toolkitId)+' connection deleted');
    if(openToolkit===toolkitId)openToolkit=null;
    if(openPolling===toolkitId)openPolling=null;
    delete toolsCache[toolkitId];
    delete pollCache[toolkitId];
    delete pollDraft[toolkitId];
    delete pollNotes[toolkitId];
    await loadAll();
  }finally{
    setBusy(button,false);
  }
}

async function toggleDrawer(toolkitId){
  if(openToolkit===toolkitId){openToolkit=null;renderGrid();return;}
  openToolkit=toolkitId;
  if(toolsCache[toolkitId]===undefined){
    renderGrid();
    const res=await apiFetch(BASE+'/'+encodeURIComponent(toolkitId)+'/tools');
    toolsCache[toolkitId]=res.ok&&res.data?(res.data.tools||[]):null;
  }
  renderGrid();
}

async function toggleAction(toolkitId,input){
  const slug=input.dataset.slug;
  const enabled=input.checked;
  input.disabled=true;
  const res=await apiFetch(BASE+'/'+encodeURIComponent(toolkitId)+'/prefs',{
    method:'PUT',body:{actions:{[slug]:enabled}},
  });
  input.disabled=false;
  if(!res.ok){
    input.checked=!enabled; /* the server is the truth; put the box back */
    cardError(toolkitId,res.error||'Could not save that preference.');
    return;
  }
  const rows=toolsCache[toolkitId];
  if(Array.isArray(rows)){
    const row=rows.find(r=>r.slug===slug);
    if(row)row.enabled=enabled;
  }
}

function labelFor(toolkitId){
  const t=toolkits.find(x=>x.id===toolkitId);
  return t?(t.display_name||t.id):toolkitId;
}

/* The callback page posts to "*", so the origin check here is what makes this
   listener safe: any page could otherwise postMessage a forged completion. */
function onAuthMessage(event){
  if(event.origin!==location.origin)return;
  const data=event.data;
  if(!data||data.connector!=='composio')return;
  if(data.type==='connector-auth-complete'){
    loadAll();
  }else if(data.type==='connector-auth-error'){
    cardError(data.toolkit,String(data.error||'Authorization failed.'));
  }
}
