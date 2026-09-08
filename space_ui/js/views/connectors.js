/* Connectors tab — Composio toolkits.

   The eight toolkits are OAuth2-only. Identity is the XO account id resolved from
   an X-XO-Session header, so every call here goes through core/session.js. Nothing
   on this page holds a provider credential; the server keeps the Composio API key
   and injects it into the MCP proxy, so the browser only ever sees status.

   Two independent states per card, and the UI has to keep them apart:
     - connected      -> the ACCOUNT holds a connection (shared by every workspace)
     - enabled here   -> THIS workspace has turned it on
   A card can be connected and off, which is the normal state for a workspace that
   did not run the OAuth flow itself. Hence two controls: "Turn off here" edits
   only this workspace, "Delete connection" removes it account-wide.

   Three failure modes are first-class states, not errors to hide:
     - no XO session      -> the backend holds no credential to identify you
     - COMPOSIO_API_KEY    -> unset, so /toolkits 500s (documented in .env.example)
     - no auth config      -> that one toolkit 422s on connect

   Connect opens the provider in a popup. The callback page posts back to its
   opener, but it posts to "*", so the listener below verifies the origin. A
   popup can also be blocked or dismissed silently, so the postMessage is only
   an accelerator — the status poll is what actually decides. */
/* core/api.js is imported with a stamp here (the other views import it bare):
   this view needs apiFetch's `headers` option, which was added at that stamp,
   and StaticFiles sends no Cache-Control — a browser holding the older bare
   URL would drop the session header and strand this tab on "sign in to XO". */
import {apiFetch} from '../core/api.js?v=20260904-tenancy1';
import {toast} from '../core/ui.js';
import {ensureSession,sessionHeaders,sessionError} from '../core/session.js?v=20260903-connectors1';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));

const BASE='/api/connectors/composio';
const POLL_ATTEMPTS=150;   /* 150 x 2s = 5 min, the usual provider consent window */
const POLL_INTERVAL=2000;

let root=null;
let toolkits=[];
let openToolkit=null;      /* id of the expanded action drawer, if any */
let toolsCache={};         /* toolkit id -> action rows */
let loading=false;
let listener=null;

export default {
  id:'connectors',label:'Connectors',order:10,
  async mount(el){
    root=el;
    renderShell();
    bindEvents();
    await loadAll();
  },
  show(){/* keep an in-flight authorization alive across tab switches */}
};

function renderShell(){
  root.innerHTML=
    '<div class="conn-page">'
      +'<header class="conn-hero">'
        +'<div>'
          +'<div class="conn-kicker">Composio &middot; per-user, per-workspace</div>'
          +'<h1>Connectors</h1>'
          +'<p>Connect the apps your agent can act in. Each connection belongs to '
            +'you in this workspace, and its tools reach the agent over an MCP '
            +'proxy that keeps the Composio key on the server.</p>'
        +'</div>'
        +'<div class="conn-hero-actions">'
          +'<button class="conn-refresh" id="conn-refresh" type="button">Refresh</button>'
        +'</div>'
      +'</header>'
      +'<div class="conn-alert" id="conn-alert" hidden></div>'
      +'<section class="conn-grid" id="conn-grid" aria-label="Composio toolkits">'
        +'<div class="conn-empty">Loading connectors&hellip;</div>'
      +'</section>'
    +'</div>';
}

function bindEvents(){
  root.querySelector('#conn-refresh').addEventListener('click',()=>loadAll());
  root.querySelector('#conn-grid').addEventListener('click',handleGridAction);
  if(!listener){
    listener=onAuthMessage;
    addEventListener('message',listener);
  }
}

/* ---------- loading ---------- */

async function loadAll(){
  if(loading)return;
  loading=true;
  setAlert(null);
  try{
    const session=await ensureSession();
    if(!session){renderSignedOut();return;}

    /* Listing also starts the server's MCP-gateway sweep in the background, so
       opening this tab (or pressing Refresh) does what the old "Reinstall MCP
       gateway" button did — the agent's wiring is never installed by hand. */
    const list=await apiFetch(BASE+'/toolkits',{headers:sessionHeaders()});

    if(!list.ok){renderListFailure(list);return;}
    toolkits=(list.data&&list.data.toolkits)||[];
    renderGrid();
  }finally{
    loading=false;
  }
}

function renderSignedOut(){
  setAlert('pending',
    'Sign in to XO to use connectors',
    (sessionError()||'')+' Connections belong to your XO account, so this page needs an '
      +'identity. Set XO_API_KEY in .env, or sign in from the app, then refresh.');
  root.querySelector('#conn-grid').innerHTML=
    '<div class="conn-empty">No identity &mdash; nothing to show yet.</div>';
}

/* The /toolkits route is the only source of the toolkit list, so when it fails
   there are no tiles to draw. Say precisely which of the two causes it was. */
function renderListFailure(res){
  /* CredentialsUnavailable's message always contains the literal
     "COMPOSIO_API_KEY" (service.py), so matching it names the cause with
     confidence. A bare 500 is *not* proof of one: every server-side fault in
     the route — a missing `composio` package, a Composio outage — is rendered
     by FastAPI as the same plain-text 500 with no detail to match on. Blaming
     credentials for all of them sends the operator off to verify keys that are
     already correct, so an unmatched 500 points at the log instead, where the
     traceback says which it was. */
  const notConfigured=/COMPOSIO_API_KEY/i.test(res.error||'');
  const serverFault=!notConfigured&&res.status===500;
  let note;
  if(res.offline){
    setAlert('error','xo-space is unreachable','The server is down or restarting.');
    note='Cannot reach the server.';
  }else if(res.status===401){
    setAlert('pending','Session expired','Refresh to mint a new session.');
    note='Your session is no longer valid.';
  }else if(notConfigured){
    setAlert('pending','Composio is not configured on this server',
      'Composio credentials come from your XO account, not from this workspace. '
      +'Check that this server is signed in (XO_API_KEY) and that COMPOSIO_API_KEY '
      +'plus one COMPOSIO_AUTH_CONFIG_&lt;TOOLKIT&gt; id per app are set on the XO side '
      +'&mdash; both are created in the Composio dashboard. A self-hosted install '
      +'with its own Composio project can set them locally with '
      +'COMPOSIO_CREDENTIALS_SOURCE=env.');
    note='No connectors to show until the server has a Composio API key.';
  }else if(serverFault){
    setAlert('error','Listing connectors failed on this server',
      'The server errored while listing toolkits and returned no detail, so the '
      +'reason is only in the xo-space server log &mdash; read the traceback there '
      +'first. The usual causes are the <code>composio</code> package missing from '
      +'the venv (install it from requirements.txt) or credentials this server '
      +'cannot fetch (XO_API_KEY plus a reachable CHAT_API_BASE_URL, or '
      +'COMPOSIO_CREDENTIALS_SOURCE=env for a self-hosted install).');
    note='Connectors are unavailable until the server-side error is cleared.';
  }else{
    setAlert('error','Could not list connectors',esc(res.error||''));
    note=res.error||'Unavailable.';
  }
  root.querySelector('#conn-grid').innerHTML=
    '<div class="conn-empty'+(notConfigured?'':' is-error')+'">'+esc(note)+'</div>';
}

/* ---------- rendering ---------- */

function isConnected(t){return String(t.status||'').toUpperCase()==='ACTIVE';}
function isEnabledHere(t){return !!t.workspace_enabled;}

/* Connections are account-wide; reach is not. A toolkit connected on the account but
   not enabled here is the normal state for a workspace that did not run the OAuth
   flow, so it gets its own label rather than reading as broken. */
function statusOf(t){
  if(!isConnected(t))return{text:'Not connected',cls:''};
  if(!isEnabledHere(t))return{text:'Off in this workspace',cls:'is-idle'};
  return{text:'On in this workspace',cls:'is-good'};
}

function renderGrid(){
  const grid=root.querySelector('#conn-grid');
  if(!toolkits.length){
    grid.innerHTML='<div class="conn-empty">No toolkits are registered on this server.</div>';
    return;
  }
  grid.innerHTML=toolkits.map(renderCard).join('');
}

function renderCard(t){
  const connected=isConnected(t);
  const enabled=isEnabledHere(t);
  const status=statusOf(t);
  const open=openToolkit===t.id;
  return'<article class="conn-card'+(connected&&enabled?' is-on':'')+'" data-toolkit="'+esc(t.id)+'">'
    +'<div class="conn-card-head">'
      +'<div class="conn-card-id">'
        +'<span>'+esc(t.slug||'')+'</span>'
        +'<h2>'+esc(t.display_name||t.id)+'</h2>'
      +'</div>'
      +'<i class="'+status.cls+'">'+esc(status.text)+'</i>'
    +'</div>'
    +'<div class="conn-card-body">'
      +'<div class="conn-facts">'
        +'<span class="conn-fact">'+esc((t.schemes||['OAUTH2']).join(', '))+'</span>'
        +(t.supports_action_prefs?'<span class="conn-fact">per-action control</span>':'')
        +(t.account_count>1?'<span class="conn-fact">'+t.account_count+' accounts</span>':'')
      +'</div>'
      +(connected&&!enabled
        ?'<p class="conn-card-note">Connected on your account. Turn it on to let this '
          +'workspace&rsquo;s agent use it.</p>'
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
    +'</div>'
    +(open?renderActions(t.id):'')
    +'</article>';
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
}

async function connect(toolkitId,button){
  cardError(toolkitId,'');
  setBusy(button,true);
  /* Opened before the await: a popup opened later is not tied to the click and
     is blocked by default in most browsers. */
  const popup=window.open('','composio-auth','width=560,height=760');
  try{
    const res=await apiFetch(BASE+'/'+encodeURIComponent(toolkitId)+'/connect',{
      method:'POST',body:{auth_scheme:'OAUTH2'},headers:sessionHeaders(),
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
    return'This toolkit has no auth config on the server. Create one in the '
      +'Composio dashboard and set COMPOSIO_AUTH_CONFIG_'
      +String(toolkitId).toUpperCase()+' where this install reads its Composio '
      +'credentials — your XO account, or locally in self-host mode.';
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
    const res=await apiFetch(path,{headers:sessionHeaders()});
    const status=String((res.data&&res.data.status)||'').toUpperCase();
    if(status==='ACTIVE'){
      if(popup&&!popup.closed)popup.close();
      toast(labelFor(toolkitId)+' connected');
      await loadAll();
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
   other workspace is affected — the whole reason connections became account-wide. */
async function setScope(toolkitId,enabled,button){
  const toolkit=toolkits.find(t=>t.id===toolkitId);
  if(!toolkit)return;
  cardError(toolkitId,'');
  setBusy(button,true);
  try{
    const path=BASE+'/'+encodeURIComponent(toolkitId)
      +(enabled?'/scope':'/accounts/'+encodeURIComponent(toolkit.connected_account_id||'')+'/unlink');
    const res=enabled
      ? await apiFetch(path,{method:'PUT',headers:sessionHeaders(),
          body:{enabled:true,
                connected_account_ids:[toolkit.connected_account_id].filter(Boolean)}})
      : await apiFetch(path,{method:'POST',headers:sessionHeaders()});
    if(!res.ok){cardError(toolkitId,res.error||'Could not save that change.');return;}
    toast(labelFor(toolkitId)+(enabled?' on in this workspace':' off in this workspace'));
    if(!enabled&&openToolkit===toolkitId)openToolkit=null;
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
      headers:sessionHeaders(),
    });
    if(!res.ok){cardError(toolkitId,res.error||'Delete failed.');return;}
    toast(labelFor(toolkitId)+' connection deleted');
    if(openToolkit===toolkitId)openToolkit=null;
    delete toolsCache[toolkitId];
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
    const res=await apiFetch(BASE+'/'+encodeURIComponent(toolkitId)+'/tools',
      {headers:sessionHeaders()});
    toolsCache[toolkitId]=res.ok&&res.data?(res.data.tools||[]):null;
  }
  renderGrid();
}

async function toggleAction(toolkitId,input){
  const slug=input.dataset.slug;
  const enabled=input.checked;
  input.disabled=true;
  const res=await apiFetch(BASE+'/'+encodeURIComponent(toolkitId)+'/prefs',{
    method:'PUT',body:{actions:{[slug]:enabled}},headers:sessionHeaders(),
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
