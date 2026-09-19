/* Footer server pill: polls /space/server/status; when the API is offline it
   offers the start command. No stop control — killing the server from its own
   UI was a footgun, especially behind a shared proxy. Independent of every
   view. */
import {API_BASE,apiFetch} from './api.js';
import {setSlottedInterval} from './store.js';

let updateServer=()=>{};
const stateListeners=new Set();

/* Subscribe to the pill's own reading of the server: fn(true) when the
   server comes (back) up, fn(false) when it goes away, called only on a
   change. The shell retries /api/ui on the next "up". Returns an
   unsubscribe function. */
export function onServerState(fn){
  stateListeners.add(fn);
  return()=>stateListeners.delete(fn);
}

/* Setup uses the same probe while restarting, so the pill and the reload
   decision reflect the same response. */
export async function pollServer(){
  const r=await apiFetch(API_BASE+'/space/server/status');
  updateServer(r.ok);
  return r;
}

export function initServerWidget(){
  const srvPip=document.getElementById('srv-pip');
  const srvText=document.getElementById('srv-text');
  const srvBtn=document.getElementById('srv-btn');
  const srvPop=document.getElementById('srvpop');
  let srvOn=null;
  updateServer=setSrv;
  function setSrv(on){
    if(srvOn===on)return;
    srvOn=on;
    srvPip.className='pip '+(on?'on':'off');
    srvText.textContent='xo-space · '+(on?'online':'offline');
    srvBtn.hidden=on; /* button exists only to show the start command */
    srvBtn.textContent='Start…';
    if(on)srvPop.classList.remove('is-open');
    for(const fn of stateListeners){
      try{fn(on);}catch(err){console.error('Server state listener failed:',err);}
    }
  }
  srvBtn.addEventListener('click',()=>{
    srvPop.classList.toggle('is-open');
  });
  document.getElementById('srv-copy').addEventListener('click',()=>{
    navigator.clipboard.writeText('cd xo-space && ./cowork-api.sh start').then(()=>{
      document.getElementById('srv-copy').textContent='Copied';
      setTimeout(()=>document.getElementById('srv-copy').textContent='Copy command',1400);
    });
  });
  pollServer();
  setSlottedInterval('server-status',pollServer,5000);
}
