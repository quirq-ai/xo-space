/* Actions for page specs (services/schema/page.schema.json, "action").

   An action is one of three things:
     call     "METHOD /path": {field} fills from the row, {page.x} from the
              page payload (URI-encoded); body from the action's body (its
              string values are templates too) or a form; confirm asks
              first; then: refresh | toast | open:<route>
     open     a page route to switch to (a template too)
     link     an http(s) URL template, opened in a new tab with noopener;
              anything else is refused, never opened
   A button is disabled while its call is in flight; a failure is a toast
   with failText. Nothing here touches innerHTML: the renderer owns the
   markup, this module owns what a click does. */
import {API_BASE,apiFetch,failText} from './api.js';
import {toast} from './ui.js';
import {evaluate,fill,truthy} from './expr.js';

const CALL=/^(GET|POST|PUT|PATCH|DELETE)\s+(\/\S*)$/;
const HTTP=/^https?:\/\//i;

/* Whether the action shows for this row: its "when" is absent or truthy. */
export function actionVisible(action,payload,row){
  return action.when===undefined||truthy(evaluate(action.when,payload,row));
}

/* Split "POST /api/x/{id}" into method and the filled path; null when the
   template is not a call. */
export function parseCall(template,payload,row){
  const m=CALL.exec(String(template??'').trim());
  if(!m)return null;
  return{method:m[1],path:fill(m[2],payload,row,true)};
}

/* A body whose string values are templates: {"note": "{title}"} fills. */
function fillBody(body,payload,row){
  if(body===null||body===undefined)return undefined;
  if(typeof body==='string')return fill(body,payload,row);
  if(Array.isArray(body))return body.map(v=>fillBody(v,payload,row));
  if(typeof body==='object'){
    const out={};
    for(const[k,v]of Object.entries(body))out[k]=fillBody(v,payload,row);
    return out;
  }
  return body;
}

/* A safe external URL, or null. Only http and https ever open. */
export function safeLink(template,payload,row){
  if(!template)return null;
  const url=fill(template,payload,row,false);
  return HTTP.test(url)?url:null;
}

/* Run one action. ctx = {refresh, switchTo}; body (optional) overrides the
   action's own (a form's values). Resolves to the call's result, or
   {ok:true} for open and link, {ok:false} when refused or cancelled. */
export async function runAction(action,{payload,row,ctx,button,body}={}){
  if(action.link){
    const url=safeLink(action.link,payload,row);
    if(!url){toast('That link is not an http(s) address.');return{ok:false};}
    window.open(url,'_blank','noopener');
    return{ok:true};
  }
  if(action.open&&!action.call){
    await ctx.switchTo(fill(action.open,payload,row));
    return{ok:true};
  }
  const call=parseCall(action.call,payload,row);
  if(!call){toast('This action has nothing to do.');return{ok:false};}
  if(action.confirm&&!window.confirm(fill(action.confirm,payload,row)))return{ok:false};
  if(button){button.disabled=true;button.setAttribute('aria-busy','true');}
  try{
    const sent=body!==undefined?body:fillBody(action.body,payload,row);
    const res=await apiFetch(API_BASE+call.path,{method:call.method,body:sent});
    if(!res.ok){toast(failText(res));return res;}
    dispatchEvent(new CustomEvent('space:wrote',{detail:{method:call.method,path:call.path}}));
    const then=String(action.then||'');
    if(then==='refresh')await ctx.refresh?.();
    else if(then==='toast')toast(action.label+': done');
    else if(then.startsWith('open:'))await ctx.switchTo(fill(then.slice(5),payload,row));
    return res;
  }finally{
    if(button&&button.isConnected){button.disabled=false;button.removeAttribute('aria-busy');}
  }
}
