/* XO session identity for the connector routes.

   Everything else in this UI is same-origin and unauthenticated: the page is
   served from the API's own origin and each fetch forwards the page query
   string. The Composio routes are the exception: they act as an XO account and
   401 without a session, so they need a header.

   The id selects nothing. This backend has exactly one account id, resolved from
   the credential the backend holds; the session is only proof that the tab was
   vouched for by a backend that is signed in.

   GET /xo-auth/session/self asks XO to mint an opaque session id against the
   credential the backend already holds (XO_API_KEY, or a consumed browser login);
   the backend proxies, it does not mint. The raw XO token never reaches the
   browser; only the opaque id does, and it lives in a module variable rather than
   browser storage, so it dies with the tab. The backend's own record of the id is
   in-memory too: persisting it would just outlive the entry it names.

   The id is minted once and shared: concurrent callers await the same promise.

   core/api.js is imported bare, the same specifier every other importer uses,
   so this module shares the ONE api.js instance (its cache stamp lives in
   index.html's import map, not on the import). The mint goes through API_BASE
   like every other call, so it also reaches the API when the page is opened
   from a separate static server. */
import {API_BASE,apiFetch} from './api.js';

let sessionId=null;
let inflight=null;
let lastError=null;

/* Headers for a connector request. Empty when there is no session: the call
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
  inflight=apiFetch(API_BASE+'/xo-auth/session/self').then(res=>{
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
