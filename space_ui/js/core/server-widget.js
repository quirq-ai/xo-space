/* Server status probe: GET /space/server/status. Setup's restart flow polls it to
   decide when the server is back. There is no status pill: every tab already says
   "xo-space is unreachable" when a request cannot reach the server. */
import {API_BASE,apiFetch} from './api.js';

export function pollServer(){
  return apiFetch(API_BASE+'/space/server/status');
}
