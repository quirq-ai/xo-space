/* Inline project sharing. Forms stay mounted in their rows; a shared lock
   prevents simultaneous submissions for one project from List and Setup. */
import {API_BASE,apiFetch,failText} from './api.js';

const pending=new Map(),subscribers=new Map();
let nextId=0;
export const isProjectSharing=id=>pending.has(id);
function notify(id){for(const update of subscribers.get(id)||[])update();}

export function createProjectShare({projectId,label=projectId,onDraftChange=()=>{},onBusyChange=()=>{}}){
  const element=document.createElement('form');
  const uid='project-share-'+(++nextId);
  element.id=uid;element.className='project-share';element.hidden=true;element.noValidate=true;
  element.dataset.projectId=projectId;
  element.innerHTML='<label for="'+uid+'-recipient">Space ID</label>'
    +'<div class="project-share-controls"><input id="'+uid+'-recipient" name="workspace_id" type="text" maxlength="100" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Recipient Space ID" required>'
    +'<button type="submit" data-share-submit>Share</button><button type="button" data-share-cancel>Cancel</button></div>'
    +'<p id="'+uid+'-result" data-share-result role="status" aria-live="polite" hidden></p>';
  const input=element.querySelector('input'),submit=element.querySelector('[data-share-submit]');
  const cancel=element.querySelector('[data-share-cancel]'),result=element.querySelector('[data-share-result]');
  input.setAttribute('aria-describedby',uid+'-result');
  let trigger=null,disabled=false,disposed=false,lastShared='',lastBusy=false;
  function hasDraft(){return !element.hidden&&(!lastShared||input.value.trim()!==lastShared);}
  function draftChanged(){onDraftChange(hasDraft());}
  function setLabel(value){element.setAttribute('aria-label','Share '+String(value||projectId));}
  function message(text,kind=''){
    result.textContent=text;result.hidden=!text;result.dataset.state=kind;
  }
  function paint(){
    const busy=isProjectSharing(projectId);
    input.disabled=disabled||busy;
    submit.disabled=disabled||busy||!!lastShared&&input.value.trim()===lastShared;
    cancel.disabled=busy;submit.textContent=busy?'Sharing…':'Share';
    element.setAttribute('aria-busy',String(busy));
    trigger?.setAttribute('aria-expanded',String(!element.hidden));
    if(lastBusy!==busy){lastBusy=busy;onBusyChange(busy);}
  }
  function setTrigger(button){
    trigger=button;trigger.setAttribute('aria-controls',uid);paint();
  }
  function open(button){
    if(button)setTrigger(button);
    element.hidden=false;paint();draftChanged();
    if(!input.disabled)input.focus({preventScroll:true});
    element.scrollIntoView({block:'nearest'});
  }
  function close(){
    if(isProjectSharing(projectId))return;
    element.hidden=true;input.value='';lastShared='';input.removeAttribute('aria-invalid');
    message('');paint();draftChanged();
    if(trigger?.isConnected&&trigger.getClientRects().length)trigger.focus({preventScroll:true});
  }
  input.addEventListener('input',()=>{
    input.removeAttribute('aria-invalid');message('');paint();draftChanged();
  });
  cancel.addEventListener('click',close);
  element.addEventListener('submit',async event=>{
    event.preventDefault();
    if(disposed||disabled||isProjectSharing(projectId)||element.hidden)return;
    const recipient=input.value.trim();
    if(!/^\S{1,100}$/.test(recipient)){
      message('Enter a Space ID without spaces (up to 100 characters).','error');
      input.setAttribute('aria-invalid','true');input.focus();return;
    }
    if(lastShared===recipient)return;
    const focused=document.activeElement;
    const request={recipient};pending.set(projectId,request);
    message('');notify(projectId);
    let response;
    try{
      response=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(projectId)+'/share',{
        method:'POST',body:{workspace_id:recipient},
      });
    }catch(error){response={ok:false,error:error?.message||'Could not share this project.'};}
    const confirmed=response.ok&&response.data?.ok===true;
    if(!disposed){
      if(confirmed){lastShared=recipient;message('Shared with '+recipient+'.','success');}
      else message(response.ok?'Sharing could not be confirmed. Check project access before trying again.':failText(response),'error');
    }
    if(pending.get(projectId)===request)pending.delete(projectId);
    notify(projectId);
    if(confirmed)dispatchEvent(new CustomEvent('space:project-access-changed',{detail:{project_id:projectId,action:'access'}}));
    if(!disposed){
      draftChanged();
      if(element.isConnected&&element.getClientRects().length&&[input,submit,cancel].includes(focused)
        &&(document.activeElement===focused||document.activeElement===document.body)){
        (submit.disabled?input:focused).focus({preventScroll:true});
      }
    }
  });
  if(!subscribers.has(projectId))subscribers.set(projectId,new Set());
  subscribers.get(projectId).add(paint);setLabel(label);paint();
  return {element,open,close,hasDraft,setTrigger,setLabel,
    setDisabled(value){disabled=Boolean(value);paint();},
    destroy(){disposed=true;subscribers.get(projectId)?.delete(paint);
      if(!subscribers.get(projectId)?.size)subscribers.delete(projectId);element.remove();},
  };
}
