/* Shared UI helpers: the toast, the escape and relative-time helpers every
   view was carrying its own copy of, and the pill strip the filter rows
   share. Everything but toast() is a pure string builder: callers own the
   DOM and decide where the markup lands. Imported bare everywhere; no cache
   stamp is needed because the /space mount sends Cache-Control: no-cache,
   so a browser revalidates this file on every load. */

let toastT=null;
export function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg;t.classList.add('is-on');
  clearTimeout(toastT);
  toastT=setTimeout(()=>t.classList.remove('is-on'),1900);
}

/* HTML-escape one untrusted value on its way into innerHTML or a
   double-quoted attribute; null and undefined become ''. */
export const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* Relative time for a row: '' for a missing or unparseable stamp, "just
   now" under a minute, then m / h / d, and past 30 days the calendar date,
   which a person reads faster than "94d ago". */
export function rel(iso){
  if(!iso)return'';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'';
  if(s<60)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  if(s<86400*30)return Math.floor(s/86400)+'d ago';
  return new Date(iso).toLocaleDateString(undefined,{dateStyle:'medium'});
}

/* One pill strip: items as [[key,label],...] become buttons carrying
   data-<attr>=key, the active one wearing is-on and aria-pressed="true",
   inside a role=group with the given accessible label. The wrapper class
   defaults to pills-<attr>; a view that already styles its strip passes its
   own. Keys and labels are escaped, so a key that came from data is safe in
   the attribute. Views listen for clicks on button[data-<attr>]. */
export function pills(items,activeKey,attr,ariaLabel,cls){
  return'<div class="'+esc(cls||'pills-'+attr)+'" role="group" aria-label="'+esc(ariaLabel)+'">'
    +items.map(([k,label])=>'<button type="button" data-'+attr+'="'+esc(k)+'"'
      +(k===activeKey?' class="is-on" aria-pressed="true"':' aria-pressed="false"')
      +'>'+esc(label)+'</button>').join('')
  +'</div>';
}
