/* One fetch layer for the whole UI.
   - API_BASE: served under /space/ means THIS origin is the API — true on
     localhost and equally true behind a remote proxy (e.g. a Coder workspace
     URL), so talk same-origin and inherit the page's routing + auth. The
     127.0.0.1:5002 fallback exists only for standalone UI dev, where the
     page is opened from some other static server.
   - Every request forwards the page's query string (e.g. Coder's
     ?coder_session_token=…) so each fetch authenticates on its own.
   - Never throws. Returns {ok, status, data, error, offline, notImplemented}:
       offline        — the request never reached the server (fetch TypeError:
                        down/restarting/proxy). Must not be blamed on the API.
       notImplemented — HTTP 501: the active agent lacks the capability. A
                        normal state for callers to render, not an error.
       error          — the API's own explanation when it sent one (a JSON
                        body with detail.message / detail), else "http NNN".
   - Concurrent GETs for the same path share one in-flight request
     (single-flight); sequential calls always hit the network fresh. */
import {singleFlight} from './store.js';

export const API_BASE=location.pathname.startsWith('/space')?'':'http://127.0.0.1:5002';

/* merge the page's query string into a path that may already carry one —
   naive concatenation would produce "…?limit=30?token=…" */
export function withPageQuery(path){
  const ps=location.search.replace(/^\?/,'');
  if(!ps)return path;
  return path+(path.includes('?')?'&':'?')+ps;
}

async function doFetch(path,method,body,headers){
  try{
    const opts={method,cache:'no-store'};
    const h={...(headers||{})};
    if(body!==undefined){
      h['Content-Type']='application/json';
      opts.body=JSON.stringify(body);
    }
    if(Object.keys(h).length)opts.headers=h;
    const r=await fetch(withPageQuery(path),opts);
    if(!r.ok){
      let message='http '+r.status;
      try{
        const j=await r.json();
        if(j.detail&&j.detail.message)message=j.detail.message;
        else if(typeof j.detail==='string')message=j.detail;
      }catch(e){}
      return{ok:false,status:r.status,data:null,offline:false,notImplemented:r.status===501,error:message};
    }
    let data=null;
    try{data=await r.json();}
    catch(err){return{ok:false,status:r.status,data:null,offline:false,notImplemented:false,error:err.message};}
    return{ok:true,status:r.status,data,offline:false,notImplemented:false,error:null};
  }catch(err){
    return{ok:false,status:0,data:null,offline:err instanceof TypeError,notImplemented:false,error:err.message};
  }
}

/* `headers` is opt-in and merged last, so it can also override Content-Type.
   Only the connector routes need it today (they carry X-XO-Session); every
   other caller omits it and sends exactly the headers it always did. */
export function apiFetch(path,{method='GET',body,headers}={}){
  if(method==='GET'){
    /* The single-flight key must include the headers: two GETs for the same
       path under different identities are different requests, and sharing one
       in-flight promise would serve one caller the other's answer. */
    const key='GET '+path+(headers?' '+JSON.stringify(headers):'');
    return singleFlight(key,()=>doFetch(path,'GET',undefined,headers));
  }
  return doFetch(path,method,body,headers); /* writes are never deduped — disable the button instead */
}
