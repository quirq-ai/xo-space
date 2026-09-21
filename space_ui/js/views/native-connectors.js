/* Workspace connectors use their existing local APIs, independently of the XO
   session used by the account-app catalog. Cards are built once and never
   rebuilt: refreshing status and filtering must not replace a credential
   field or an active login.

   The grid holds one tile per app; the card is the popup's contents. Because
   the card is the node that carries the live state, it is MOVED into the
   shared popup on open and taken back out on close, rather than re-rendered
   there: a half-typed token, a device code and an in-flight poll all survive
   opening and closing the popup. Its listeners sit on the card itself for
   the same reason, so they travel with it. */
import {API_BASE,apiFetch} from '../core/api.js';
import {esc} from '../core/ui.js';

const BASE=API_BASE+'/api/connectors/';
const ICONS={
  github:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 .75a11.25 11.25 0 0 0-3.56 21.92c.56.1.77-.24.77-.54v-2.09c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.65-1.25-1.65-1.02-.7.08-.69.08-.69 1.13.08 1.72 1.16 1.72 1.16 1 1.72 2.64 1.22 3.28.93.1-.73.39-1.22.71-1.5-2.5-.28-5.13-1.25-5.13-5.57 0-1.23.44-2.23 1.16-3.02-.12-.28-.5-1.43.11-2.98 0 0 .95-.3 3.09 1.16a10.76 10.76 0 0 1 5.62 0c2.15-1.45 3.09-1.16 3.09-1.16.61 1.55.23 2.7.11 2.98.72.79 1.16 1.79 1.16 3.02 0 4.33-2.63 5.29-5.14 5.57.4.35.76 1.04.76 2.1v3.07c0 .3.2.65.78.54A11.25 11.25 0 0 0 12 .75Z"/></svg>',
  gdrive:'<svg viewBox="0 0 24 24"><path fill="#34a853" d="M8 2 0 16l4 7 8-14Z"/><path fill="#fbbc04" d="M8 2h8l8 14h-8Z"/><path fill="#4285f4" d="M0 16h24l-4 7H4Z"/></svg>',
  onedrive:'<svg viewBox="0 0 24 24"><path fill="#1765c1" d="M6 17a5 5 0 1 1 2-9 6 6 0 0 1 11 3 3 3 0 0 1 0 6Z"/><path fill="#42a5ed" d="M5 19a4 4 0 1 1 3-7 5 5 0 0 1 9 1 3 3 0 1 1 2 6Z"/></svg>',
};
const APPS=[
  {id:'github',name:'GitHub',icon:'GH',description:'Repositories, issues and project sync.'},
  {id:'magicpath',name:'MagicPath',icon:'M',description:'Design and build with the MagicPath skill and CLI.'},
  {id:'vercel',name:'Vercel',icon:'▲',description:'Projects, deployments and hosting.'},
  {id:'gdrive',name:'Google Drive',icon:'D',description:'Connect file storage through rclone.',drive:true},
  {id:'onedrive',name:'OneDrive',icon:'O',description:'Connect Microsoft file storage through rclone.',drive:true},
];
const string=value=>typeof value==='string'?value.trim().slice(0,300):'';
const button=(action,label,primary=false)=>'<button type="button" class="conn-btn '+(primary?'conn-primary':'conn-secondary')+'" data-native-action="'+action+'">'+label+'</button>';
const field=(id,name,label,type='password',extra='')=>'<label class="conn-native-field" for="native-'+id+'-'+name+'"><span>'+label+'</span><input id="native-'+id+'-'+name+'" name="'+name+'" type="'+type+'" autocomplete="off" spellcheck="false" '+extra+'></label>';

/* The directory entry. A button, so Enter and Space open the popup with no
   key handling here; paint() keeps its pill and detail line in step with the
   card's. It carries no control of its own, so a status refresh can never
   disturb what someone is typing. */
function tileMarkup(app){
  return '<button type="button" class="conn-tile" data-native-tile="'+app.id+'" aria-haspopup="dialog">'
    +'<span class="conn-card-heading"><span class="conn-icon" aria-hidden="true">'+(ICONS[app.id]||app.icon)+'</span>'
      +'<span class="conn-card-id"><span class="conn-tile-name">'+app.name+'</span></span></span>'
    +'<span class="conn-card-status"><i class="conn-state">Checking&hellip;</i></span>'
    +'<span class="conn-tile-desc">'+app.description+'</span>'
    +'<span class="conn-tile-detail"></span></button>';
}

function cardMarkup(app){
  const id=app.id;
  const token=id==='github'||id==='vercel';
  return '<article class="conn-card native-conn-card" data-native-connector="'+id+'" aria-labelledby="native-'+id+'-title">'
    +'<header class="conn-card-head"><div class="conn-card-heading"><span class="conn-icon" aria-hidden="true">'+(ICONS[id]||app.icon)+'</span><div class="conn-card-id"><h3 id="native-'+id+'-title">'+app.name+'</h3></div></div><span class="conn-state">Checking…</span></header>'
    +'<div class="conn-card-body"><p class="conn-card-description">'+app.description+'</p><p class="conn-native-status" aria-live="polite"></p>'
    +(app.drive?'<ul class="conn-native-remotes"></ul>':'')
    +'</div>'
    +'<div class="conn-card-acts">'+button('open',app.drive?'Add account':'Connect',true)
      +(id==='magicpath'?button('setup','Install skill &amp; CLI'):'')
      +(!app.drive?button('disconnect','Disconnect'):'')+'</div>'
    +'<div class="conn-native-form" id="native-'+id+'-form" hidden>'
      +(token?'<form data-native-form="token">'+field(id,'token',id==='github'?'Personal access token':'API token','password','required maxlength="4096"')+'<button class="conn-primary" type="submit">Save token</button></form>'+button('browser','Sign in with '+app.name):'')
      +(id==='magicpath'?'<p class="conn-native-note">Sign in, then paste the authorization code from MagicPath.</p>'+button('browser','Open MagicPath sign-in'):'')
      +(app.drive?'<form data-native-form="remote">'+field(id,'name','Account name','text','required pattern="[a-z0-9_-]{1,32}" maxlength="32" placeholder="my-drive"')+'<p class="conn-native-note">Use lowercase letters, numbers, - or _.</p><button class="conn-primary" type="submit">Start sign-in</button></form>':'')
      +'<div class="conn-native-auth" hidden><p class="conn-native-note" data-native-note></p><a class="conn-secondary" data-native-link target="_blank" rel="noopener noreferrer" hidden>Continue sign-in ↗</a><p data-native-code hidden></p></div>'
      +'<form data-native-form="code" hidden>'+field(id,'code',id==='magicpath'?'Authorization code':'Redirect URL','password','required maxlength="16384"')+'<button class="conn-primary" type="submit">Complete sign-in</button></form>'
      +'<div class="conn-card-acts">'+button('check','Check connection')+button('cancel','Cancel sign-in')+button('close','Close')+'</div>'
    +'</div><p class="conn-card-error" role="alert" hidden></p></article>';
}

/* Never render provider errors: CLI output and callback errors can contain a
   pasted token/code. Only known categorical failures become user-facing text. */
function failure(res,action='complete this request'){
  if(res.offline)return'Workspace is unreachable. Try again when it is back online.';
  if(res.status===409)return'Another sign-in is running. Finish or cancel it, then try again.';
  if(res.status===401||res.status===403)return'Authorization was rejected. Sign in again.';
  if(res.status===404)return'This connector or sign-in is unavailable. Refresh and try again.';
  if(res.status===501)return'This connector is not available on this server.';
  return'Could not '+action+'. Check the connection and try again.';
}

function loginURL(value){
  if(typeof value!=='string')return'';
  try{const url=new URL(value);return url.protocol==='https:'&&!url.username&&!url.password?url.href:'';}catch{return'';}
}

export function mountNativeConnectors(el,{onChange=()=>{},modal}={}){
  el.innerHTML=APPS.map(tileMarkup).join('');
  let filter='';
  /* The card is created detached and kept that way: it lives in the popup
     while that app is open and nowhere at all otherwise, so nothing ever
     replaces it. */
  const build=markup=>{const holder=document.createElement('div');holder.innerHTML=markup;return holder.firstElementChild;};
  const states=new Map(APPS.map(app=>[app.id,{app,card:build(cardMarkup(app)),
    tile:el.querySelector('[data-native-tile="'+app.id+'"]'),
    revision:0,busy:false,pending:null,status:null,statusError:false,timer:null,polling:false,refreshing:null,refreshAgain:false}]));
  const find=(state,selector)=>state.card.querySelector(selector);
  const action=(state,name)=>find(state,'[data-native-action="'+name+'"]');
  const error=(state,message)=>{const node=find(state,'.conn-card-error');node.textContent=message;node.hidden=!message;};
  const notice=(state,message)=>{find(state,'[data-native-note]').textContent=message;};
  const count=()=>({total:APPS.length,shown:[...states.values()].filter(state=>!state.tile.hidden).length});

  function applyFilter(){
    for(const state of states.values()){
      const search=[state.app.name,state.app.description,find(state,'.conn-native-status').textContent,
        find(state,'.conn-state').textContent,find(state,'.conn-native-remotes')?.textContent||''].join(' ').toLowerCase();
      state.tile.hidden=!!filter&&!search.includes(filter);
    }
    return count();
  }

  function paint(state){
    const {app,status:data,card}=state;
    let connected=false,label='Not connected',detail='';
    if(state.statusError){label='Unavailable';detail=app.drive?'Check that rclone is installed and available.':'Could not check connection. Refresh to try again.';}
    else if(!data){label='Checking…';}
    else if(app.drive){
      const remotes=Array.isArray(data.remotes)?data.remotes:[];
      const ready=remotes.filter(remote=>remote.complete===true);
      connected=ready.length>0;label=connected?'Configured':remotes.length?'Needs attention':'Not configured';
      detail=ready.length?ready.length+' configured account'+(ready.length===1?'':'s'):'';
      find(state,'.conn-native-remotes').innerHTML=remotes.filter(remote=>string(remote.name)).map(remote=>
        '<li class="conn-native-remote"><span>'+esc(string(remote.name))+' <small>'+(remote.complete===true?'Configured':'Incomplete')+'</small></span><button type="button" class="conn-secondary" data-native-action="remove" data-remote="'+esc(string(remote.name))+'">Remove</button></li>').join('');
    }else if(app.id==='magicpath'){
      connected=data.logged_in===true;label=connected?'Connected':data.cli_installed?'Sign-in not verified':'Install required';
      detail=connected?string(data.user?.email)||string(data.user?.name)||'Signed in':
        data.cli_installed?(data.skill_installed?'Skill and CLI installed':'CLI installed · Skill missing'):'Install the skill and CLI to get started.';
    }else{
      connected=data.status==='connected';label=connected?'Connected':data.status==='failed'?'Unavailable':'Not connected';
      detail=connected?string(data.username)||string(data.email)||string(data.name)||'Account connected':'';
      if(app.id==='github'&&detail&&string(data.username))detail='@'+detail;
    }
    find(state,'.conn-state').textContent=state.pending?'Sign-in pending':label;
    find(state,'.conn-state').classList.toggle('is-connected',connected);
    card.classList.toggle('is-connected',connected);
    find(state,'.conn-native-status').textContent=detail;
    /* the tile repeats what the popup's head says, so the directory reads
       correctly whether or not the popup is open */
    const pill=state.tile.querySelector('.conn-state');
    pill.textContent=state.pending?'Sign-in pending':label;
    pill.classList.toggle('is-connected',connected);
    state.tile.classList.toggle('is-on',connected);
    state.tile.querySelector('.conn-tile-detail').textContent=detail;
    action(state,'open').textContent=app.drive?'Add account':connected?'Manage':'Connect';
    if(!app.drive)action(state,'disconnect').hidden=!connected;
    if(app.id==='magicpath')action(state,'setup').hidden=!!(data?.cli_installed&&data?.skill_installed);
    action(state,'cancel').hidden=!state.pending;
    if(app.id==='vercel')action(state,'cancel').textContent='Stop waiting';
    for(const input of card.querySelectorAll('button,input'))input.disabled=state.busy;
    if(!app.drive)action(state,'disconnect').disabled=state.busy||!!state.pending;
    if(app.id==='magicpath')action(state,'setup').disabled=state.busy||!!state.pending;
    const browserButton=action(state,'browser');
    if(browserButton){
      browserButton.disabled=state.busy||!!state.pending||app.id==='magicpath'&&!data?.cli_installed||app.id==='vercel'&&data?.status!=='needs_auth';
      if(app.id==='vercel')browserButton.textContent=connected?'Disconnect before browser sign-in':
        data?.status==='needs_auth'?'Sign in with Vercel':'Check connection before browser sign-in';
    }
    const remoteForm=find(state,'[data-native-form="remote"]');
    if(remoteForm)remoteForm.hidden=!!state.pending;
    const tokenForm=find(state,'[data-native-form="token"]');
    if(tokenForm)tokenForm.hidden=!!state.pending;
    applyFilter();onChange(count());
  }

  async function refreshOne(state){
    if(state.busy)return;
    /* A status read already in flight predates a just-finished token save.
       Wait for it before starting the fresh read; apiFetch shares GETs. */
    if(state.refreshing){state.refreshAgain=true;await state.refreshing;return;}
    state.refreshing=(async()=>{
      do{
        state.refreshAgain=false;
        const revision=++state.revision;
        const res=await apiFetch(BASE+state.app.id+(state.app.drive?'/remotes':'/status'));
        if(state.revision!==revision||state.busy)continue;
        const valid=res.ok&&res.data&&typeof res.data==='object'&&(state.app.drive?Array.isArray(res.data.remotes):
          state.app.id==='magicpath'?typeof res.data.logged_in==='boolean':['connected','needs_auth','failed'].includes(res.data.status));
        state.status=valid?res.data:null;state.statusError=!valid;
        if(state.pending?.kind==='magicpath'&&!state.pending.initialConnected&&state.status?.logged_in===true)completed(state);
        paint(state);
      }while(state.refreshAgain&&!state.busy);
    })();
    try{await state.refreshing;}finally{state.refreshing=null;}
  }

  function showForm(state){
    find(state,'.conn-native-form').hidden=false;action(state,'open').setAttribute('aria-expanded','true');
  }
  function authLink(state,url){
    const link=find(state,'[data-native-link]');
    const safe=loginURL(url);link.hidden=!safe;
    if(safe)link.href=safe;else link.removeAttribute('href');
    find(state,'.conn-native-auth').hidden=false;
    return !!safe;
  }
  function stopPending(state){
    clearTimeout(state.timer);state.timer=null;state.pending=null;
    find(state,'.conn-native-auth').hidden=true;
    find(state,'[data-native-form="code"]').hidden=true;
    find(state,'input[name="code"]').value='';
    find(state,'[data-native-code]').textContent='';
    find(state,'[data-native-link]').removeAttribute('href');
  }
  function completed(state){
    error(state,'');
    stopPending(state);
    find(state,'input[name="token"]')&&(find(state,'input[name="token"]').value='');
    find(state,'input[name="name"]')&&(find(state,'input[name="name"]').value='');
    find(state,'.conn-native-form').hidden=true;action(state,'open').setAttribute('aria-expanded','false');
  }

  async function run(state,task){
    if(state.busy)return;
    state.busy=true;++state.revision;error(state,'');paint(state);
    try{await task();}finally{state.busy=false;paint(state);}
  }

  async function poll(state){
    const pending=state.pending;
    if(!pending||state.polling||state.busy)return;
    state.polling=true;
    try{
      const res=pending.kind==='github'
        ?await apiFetch(BASE+'github/cli/poll',{method:'POST',body:{session_id:pending.id}})
        :pending.kind==='drive'?await apiFetch(BASE+state.app.id+'/sessions/'+encodeURIComponent(pending.id))
          :await apiFetch(BASE+'vercel/status');
      if(state.pending!==pending)return;
      if(state.busy){state.timer=setTimeout(()=>poll(state),2000);return;}
      if(!res.ok){error(state,failure(res,'check sign-in'));return;}
      const data=res.data||{};
      if(data.status==='connected'||data.status==='completed'){
        completed(state);await refreshOne(state);return;
      }
      if(data.status==='failed'||data.status==='cancelled'){
        stopPending(state);error(state,'Sign-in did not complete. Start again.');paint(state);return;
      }
      if(data.auth_url){
        if(!authLink(state,data.auth_url)){error(state,'The server returned an invalid sign-in link. Cancel and try again.');return;}
        notice(state,pending.submitted?'Finishing sign-in…':'Approve access in the browser, then paste the full redirect URL here.');
        find(state,'[data-native-form="code"]').hidden=!!pending.submitted;
      }
      if(Date.now()-pending.started>300000){notice(state,'Still waiting. Finish sign-in, then choose Check connection.');return;}
      state.timer=setTimeout(()=>poll(state),2000);
    }finally{state.polling=false;}
  }

  async function browser(state){
    /* Vercel's status route has no authorization-attempt id. Reusing an
       already-connected account would falsely finish a new OAuth attempt
       before the user had approved it. Token replacement stays available. */
    if(state.app.id==='vercel'&&state.status?.status!=='needs_auth')return;
    showForm(state);
    await run(state,async()=>{
      const id=state.app.id;
      const path=id==='github'?'github/cli/start':id==='magicpath'?'magicpath/login':'vercel/oauth/start';
      const res=await apiFetch(BASE+path,id==='vercel'?{}:{method:'POST',body:{}});
      if(!res.ok){error(state,failure(res,'start sign-in'));return;}
      const data=res.data||{};
      if(!authLink(state,data.verification_uri||data.login_url||data.auth_url)){
        error(state,'The server returned an invalid sign-in link. Try again.');return;
      }
      if(id==='github'&&!string(data.session_id)){error(state,'The server returned no sign-in session. Try again.');return;}
      state.pending={kind:id,id:string(data.session_id),started:Date.now(),
        initialConnected:id==='magicpath'?state.status?.logged_in===true:state.status?.status==='connected'};
      if(id==='github'){
        const code=find(state,'[data-native-code]');code.textContent='Device code: '+string(data.user_code);code.hidden=false;
        notice(state,'Open GitHub and enter this device code.');
      }else{
        notice(state,id==='magicpath'?'Approve access, then paste the authorization code here.':'Approve access. If the redirect cannot load, paste its full URL here.');
        find(state,'[data-native-form="code"]').hidden=false;
      }
    });
    if(state.pending&&state.app.id!=='magicpath')poll(state);
  }

  async function submit(state,form){
    const kind=form.dataset.nativeForm;
    if(!form.reportValidity())return;
    await run(state,async()=>{
      const id=state.app.id;
      let path,body;
      if(kind==='token'){path=id+'/token';body={token:find(state,'input[name="token"]').value.trim()};}
      else if(kind==='remote'){path=id+'/remotes';body={name:find(state,'input[name="name"]').value.trim()};}
      else{
        const value=find(state,'input[name="code"]').value.trim();
        path=id==='magicpath'?id+'/login':id==='vercel'?id+'/oauth/exchange':id+'/sessions/'+encodeURIComponent(state.pending?.id||'')+'/submit';
        body=id==='vercel'?{callback_url:value}:{code:value};
      }
      if(Object.values(body).some(value=>!value)){error(state,'Enter a value before continuing.');return;}
      const res=await apiFetch(BASE+path,{method:'POST',body});
      if(!res.ok){error(state,failure(res,kind==='token'?'save the token':'complete sign-in'));return;}
      const data=res.data||{};
      if(kind==='remote'){
        if(!string(data.session_id)){error(state,'The server returned no sign-in session. Try again.');return;}
        state.pending={kind:'drive',id:string(data.session_id),started:Date.now()};
        find(state,'.conn-native-auth').hidden=false;notice(state,'Preparing the sign-in link…');
      }else if(state.app.drive){
        if(state.pending)state.pending.submitted=true;
        find(state,'input[name="code"]').value='';notice(state,'Finishing sign-in…');
        find(state,'[data-native-form="code"]').hidden=true;
      }else if(data.status==='connected'||id==='magicpath'&&data.ok===true){completed(state);}
      else{error(state,'The connection was not confirmed. Check the credential and try again.');return;}
    });
    if(state.pending&&state.app.drive)poll(state);
    else if(!state.pending)await refreshOne(state);
  }

  async function handle(state,name,button){
    if(name==='open'){showForm(state);return;}
    if(name==='close'){find(state,'.conn-native-form').hidden=true;action(state,'open').setAttribute('aria-expanded','false');return;}
    if(name==='browser'){await browser(state);return;}
    if(name==='check'){if(state.pending&&state.pending.kind!=='magicpath')await poll(state);else await refreshOne(state);return;}
    if(name==='cancel'){
      await run(state,async()=>{
        const pending=state.pending;if(!pending)return;
        let res={ok:true};
        if(pending.kind==='github')res=await apiFetch(BASE+'github/cli/cancel',{method:'POST',body:{session_id:pending.id}});
        else if(pending.kind==='drive')res=await apiFetch(BASE+state.app.id+'/sessions/'+encodeURIComponent(pending.id)+'/cancel',{method:'POST'});
        if(!res.ok){error(state,failure(res,'cancel sign-in'));return;}
        stopPending(state);
      });return;
    }
    if(name==='setup'){
      await run(state,async()=>{
        const res=await apiFetch(BASE+'magicpath/setup',{method:'POST'});
        if(!res.ok||res.data?.ok!==true)error(state,'Installation did not finish. Check that Node.js and npm are available, then try again.');
      });await refreshOne(state);return;
    }
    if(name==='disconnect'||name==='remove'){
      const remote=button.dataset.remote;
      if(!confirm(name==='remove'?'Remove '+remote+' from this workspace? Files in the account stay in place.':'Disconnect '+state.app.name+' from this workspace?'))return;
      await run(state,async()=>{
        const path=state.app.id+(name==='remove'?'/remotes/'+encodeURIComponent(remote):state.app.id==='magicpath'?'/logout':'/disconnect');
        const res=await apiFetch(BASE+path,{method:name==='remove'?'DELETE':'POST'});
        /* The drive DELETE contract is 204; shared apiFetch expects JSON. */
        if(!res.ok&&res.status!==204){error(state,failure(res,'disconnect'));return;}
        completed(state);
      });await refreshOne(state);
    }
  }

  /* A tile carries nothing but the app it stands for: pressing one hands the
     card to the popup. Closing takes the card back out; it keeps its state
     either way, so reopening resumes exactly where the person left off. */
  el.addEventListener('click',event=>{
    const tile=event.target.closest('[data-native-tile]');
    if(!tile)return;
    const state=states.get(tile.dataset.nativeTile);
    if(!state||!modal)return;
    modal.show(tile,()=>state.card.remove());
    modal.body().appendChild(state.card);
  });
  for(const state of states.values()){
    /* on the card, not on the grid: the card is what moves */
    state.card.addEventListener('click',event=>{
      const button=event.target.closest('button[data-native-action]');
      if(!button||button.disabled)return;
      handle(state,button.dataset.nativeAction,button);
    });
    state.card.addEventListener('submit',event=>{
      const form=event.target.closest('form[data-native-form]');if(!form)return;
      event.preventDefault();submit(state,form);
    });
    action(state,'open').setAttribute('aria-controls','native-'+state.app.id+'-form');
    action(state,'open').setAttribute('aria-expanded','false');paint(state);
  }
  return{
    async refresh(){await Promise.all([...states.values()].map(refreshOne));return count();},
    setFilter(query){filter=String(query||'').trim().toLowerCase();return applyFilter();},
  };
}
