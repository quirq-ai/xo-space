/* XO session identity for the connector routes.

   Everything else in this UI is same-origin and unauthenticated: the page is
   served from the API's own origin and each fetch forwards the page query
   string. The Composio routes are the exception — they resolve a per-user,
   workspace-scoped principal and 401 without one, so they need a header.

   GET /xo-auth/session/self mints an opaque session id from the credential the
   backend already holds (XO_API_KEY, or a consumed browser login). The raw XO
   token never reaches the browser; only the opaque id does, and it lives in a
   module variable rather than browser storage, so it dies with the tab. The
   server-side session table is in-memory too — persisting the id would just
   outlive the entry it names.

   The id is minted once and shared: concurrent callers await the same promise. */
/* Same stamp as the connectors view, so both share ONE api.js module instance
   (a differing URL would give each its own, splitting singleFlight's map). */
import {apiFetch} from './api.js?v=20260903-connectors1';

let sessionId=null;
let inflight=null;
let lastError=null;

/* Headers for a connector request. Empty when there is no session — the call
   then 401s with the API's own explanation, which is what the view renders. */
export function sessionHeaders(){
  return sessionId?{'X-XO-Session':sessionId}:{};
}

export function hasSession(){return !!sessionId;}

/* Why the last mint failed, for the signed-out empty state. */
export function sessionError(){return lastError;}

export function ensureSession({force=false}={}){
  if(force){sessionId=null;inflight=null;}
  if(sessionId)return Promise.resolve(sessionId);
  if(inflight)return inflight;
  inflight=apiFetch('/xo-auth/session/self').then(res=>{
    inflight=null;
    if(res.ok&&res.data&&res.data.session_id){
      sessionId=res.data.session_id;
      lastError=null;
      return sessionId;
    }
    lastError=res.offline
      ?'xo-space is unreachable.'
      :(res.error||'This server holds no XO credential.');
    return null;
  });
  return inflight;
}
