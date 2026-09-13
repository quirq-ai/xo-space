/* Pure formatters over one entry of GET /api/connections (a polled
   toolkit): its cadence, its collector labels, and its last-poll-or-error
   line. Shared by the Inbox connections section and the Connectors polling
   drawer so the same payload never reads two ways. No DOM, no fetch, no
   escaping: every string returned here still goes through esc() in the
   view that paints it. */
import {rel} from './ui.js';

/* 'every 15 min', or 'every 2 h' on a whole number of hours; a missing or
   sub-minute interval reads as one minute rather than zero */
export function every(s){
  s=Number(s)||0;
  if(s>0&&s%3600===0)return'every '+(s/3600)+' h';
  return'every '+Math.max(1,Math.round(s/60))+' min';
}

/* The chosen collectors by their catalog labels (ids stand in for labels
   the catalog does not carry), 'no collectors' when none are chosen. */
export function collectorLabels(c){
  const byId=new Map((Array.isArray(c.available_collectors)?c.available_collectors:[])
    .filter(a=>a&&typeof a==='object').map(a=>[a.id,a.label||a.id]));
  const ids=Array.isArray(c.collectors)?c.collectors:[];
  return ids.length?ids.map(id=>byId.get(id)||id).join(', '):'no collectors';
}

/* {error} when the last poll failed, else {text}: 'last poll 3m ago' or
   'never polled'. The view sets the tone (capitalisation, the error style). */
export function pollLine(c){
  if(c.last_error)return{error:String(c.last_error)};
  return{text:c.last_poll_at?'last poll '+rel(c.last_poll_at):'never polled'};
}
