/* Project creation and local removal. The server is the authority for every
   removal check; a missing or stale sharing response never enables deletion. */
import {apiFetch,failText} from '../core/api.js';
import {esc,toast} from '../core/ui.js';
import {createProjectShare,isProjectSharing} from '../core/project-share.js?v=20260914-inboxshare1';

const base='/api/xo-projects';
const path=id=>base+'/'+encodeURIComponent(id);
const text=value=>typeof value==='string'?value.trim():'';
const PROJECT_ID=/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;

export function mountProjects(el,{onChange=()=>{},onDraftChange=()=>{},onStatusChange=()=>{}}={}){
  let items=[],catalogRevision=0,catalogLoading=false,catalogQueued=false,creating=false;
  let selected=null,detail=null,detailRevision=0,detailLoading=false,detailQueued=false;
  let busy=false,lastAction='',revokeConfirm=null,folderEdited=false;
  const projectRows=new Map();
  el.classList.add('setup-projects');
  el.innerHTML=`<div class="setup-card-head"><h3 id="setup-projects-title" tabindex="-1">Local projects <span id="setup-project-count"></span></h3>
      <div class="setup-project-tools"><button class="setup-secondary" type="button" id="setup-project-refresh">Refresh</button><button class="setup-primary" type="button" id="setup-project-add">Add project</button></div></div>
    <p id="setup-project-notice" class="setup-project-notice" role="status" hidden></p>
    <form id="setup-project-form" class="setup-project-form" hidden novalidate>
      <h4>Add a project</h4><p>Clone a Git repository into your projects folder.</p>
      <label for="setup-project-repository">Repository URL</label><input id="setup-project-repository" type="text" autocomplete="off" spellcheck="false" placeholder="https://github.com/owner/project.git" required>
      <label for="setup-project-id">Folder name</label><input id="setup-project-id" autocomplete="off" spellcheck="false" placeholder="my-project" maxlength="120" required>
      <div class="setup-form-error" id="setup-project-add-error" role="alert" hidden></div>
      <div class="setup-actions"><button class="setup-primary" id="setup-project-create" type="submit">Clone project</button><button class="setup-secondary" id="setup-project-cancel" type="button">Cancel</button></div>
    </form>
    <div id="setup-project-list" class="setup-project-list"><p id="setup-project-list-status" class="setup-empty" role="status">Loading projects…</p></div>
    <section id="setup-project-removal" class="setup-project-removal" aria-labelledby="setup-project-removal-title" hidden>
      <header><div><h4 id="setup-project-removal-title" tabindex="-1">Remove project</h4><code id="setup-project-removal-id"></code></div><button class="setup-secondary" id="setup-project-close" type="button">Cancel</button></header>
      <p>Delete this project folder and all its files from this Space. Remote repositories and backups remain.</p>
      <div id="setup-project-access" aria-live="polite"></div>
      <div class="setup-form-error" id="setup-project-remove-error" role="alert" hidden></div>
      <form id="setup-project-remove-form" novalidate><label for="setup-project-confirm">Type <code id="setup-project-confirm-label"></code> to confirm</label><input id="setup-project-confirm" autocomplete="off" spellcheck="false">
        <div class="setup-actions"><button class="setup-project-delete" id="setup-project-delete" type="submit" disabled>Delete local project</button><button class="setup-secondary" id="setup-project-recheck" type="button">Check access again</button></div></form>
    </section>`;
  const $=selector=>el.querySelector(selector);
  const form=$('#setup-project-form'),idInput=$('#setup-project-id'),repositoryInput=$('#setup-project-repository');
  const removal=$('#setup-project-removal'),confirmInput=$('#setup-project-confirm');
  const list=$('#setup-project-list'),listStatus=$('#setup-project-list-status');
  function error(selector,message){const node=$(selector);node.textContent=message||'';node.hidden=!message;}
  function draftChanged(){onDraftChange(hasDraft());}
  function hasDraft(){return !form.hidden||Boolean(selected)||[...projectRows.values()].some(row=>row.share.hasDraft());}
  function changed(id,action){onChange({project_id:id,action});}
  function validDetail(data,id){
    return data&&data.project_id===id&&typeof data.can_remove==='boolean'
      &&Array.isArray(data.blockers)&&Array.isArray(data.members)&&Array.isArray(data.peers)
      &&data.members.every(member=>text(member?.workspace_id)&&typeof member.can_revoke==='boolean')
      &&data.peers.every(peer=>text(peer?.user_id));
  }
  function canRemove(){return !busy&&!isProjectSharing(selected)&&!detailLoading&&detail?.can_remove===true&&detail.blockers.length===0
    &&detail.peers.length===0&&!detail.members.some(member=>member.can_revoke);}
  function updateControls(){
    $('#setup-project-delete').disabled=!canRemove()||confirmInput.value!==selected;
    $('#setup-project-delete').textContent=busy&&lastAction==='delete'?'Deleting…':'Delete local project';
    $('#setup-project-close').disabled=busy;
    $('#setup-project-recheck').disabled=busy||detailLoading;
    confirmInput.disabled=busy;
    for(const [id,row] of projectRows){
      row.removeButton.disabled=busy||isProjectSharing(id);
      row.shareButton.disabled=busy||creating;
      row.share.setDisabled(busy||creating);
    }
  }
  function paintList(message=''){
    $('#setup-project-count').textContent=catalogLoading?'':String(items.length);
    const focused=list.contains(document.activeElement)?document.activeElement:null,scrollTop=list.scrollTop;
    listStatus.textContent=message||(!items.length?'No projects in this Space yet.':'');
    listStatus.hidden=!listStatus.textContent;listStatus.classList.toggle('is-error',Boolean(message));
    const ids=new Set(items.map(item=>item.id));
    for(const [id,row] of projectRows)if(!ids.has(id)){row.share.destroy();row.element.remove();projectRows.delete(id);}
    let position=listStatus.nextElementSibling;
    for(const item of items){
      let row=projectRows.get(item.id);
      if(!row){
        const element=document.createElement('div');element.className='setup-project-row';element.dataset.projectId=item.id;
        element.innerHTML='<div class="setup-project-summary"></div><div class="setup-project-row-actions">'
          +'<button class="setup-secondary" type="button" data-project-share="'+esc(item.id)+'">Share</button>'
          +'<button class="setup-secondary" type="button" data-project-remove="'+esc(item.id)+'">Remove</button></div>';
        const share=createProjectShare({projectId:item.id,onDraftChange:draftChanged,
          onBusyChange:()=>sharingChanged(item.id)});
        row={element,share,summary:element.querySelector('.setup-project-summary'),
          shareButton:element.querySelector('[data-project-share]'),removeButton:element.querySelector('[data-project-remove]')};
        share.setTrigger(row.shareButton);element.appendChild(share.element);projectRows.set(item.id,row);
      }
      const html='<b>'+esc(item.display_name||item.id)+'</b><code>'+esc(item.id)+'</code>'
        +(text(item.description)?'<p>'+esc(item.description)+'</p>':'');
      if(row.summary.innerHTML!==html)row.summary.innerHTML=html;
      row.share.setLabel(item.display_name||item.id);
      row.shareButton.setAttribute('aria-label','Share '+(item.display_name||item.id));
      if(row.element!==position)list.insertBefore(row.element,position);
      position=row.element.nextElementSibling;
    }
    list.scrollTop=scrollTop;
    if(focused?.isConnected&&focused.getClientRects().length)focused.focus({preventScroll:true});
    updateControls();
  }
  function sharingChanged(id){
    if(selected===id){
      detail=null;detailRevision++;detailLoading=false;revokeConfirm=null;
      if(isProjectSharing(id))paintAccess({message:'Sharing is in progress. Access will be checked again when it finishes.'});
      else refreshDetail();
    }
    updateControls();
  }
  async function refreshCatalog(){
    const mine=++catalogRevision;
    if(catalogLoading){catalogQueued=true;return;}
    catalogLoading=true;$('#setup-project-refresh').disabled=true;
    onStatusChange({status:'loading',count:items.length});
    const res=await apiFetch(base);
    catalogLoading=false;
    if(mine===catalogRevision){
      if(res.ok&&Array.isArray(res.data?.items)&&res.data.items.every(item=>text(item?.id))){
        items=res.data.items;paintList();onStatusChange({status:'ready',count:items.length});
      }else{
        paintList(res.ok?'Could not read the project list.':failText(res));
        onStatusChange({status:'error',count:items.length});
      }
    }
    $('#setup-project-refresh').disabled=false;
    if(catalogQueued){catalogQueued=false;await refreshCatalog();}
  }
  function paintAccess({loading=false,message=''}={}){
    const target=$('#setup-project-access');
    if(loading){target.innerHTML='<p class="setup-project-access-note" role="status">Checking project access…</p>';updateControls();return;}
    if(!detail){target.innerHTML='<p class="setup-project-access-note is-error">'+esc(message||'Access could not be verified. Try checking again.')+'</p>';updateControls();return;}
    const members=detail.members;
    const rows=members.map(member=>{
      const ws=text(member.workspace_id),confirm=revokeConfirm?.kind==='workspace'&&revokeConfirm.id===ws;
      return '<div class="setup-project-member"><div><b>'+esc(text(member.label)||ws)+'</b>'
        +(text(member.label)?'<code>'+esc(ws)+'</code>':'')+'<small>'+esc(text(member.role)||'Collaborator')
        +(member.is_self?' · You':'')+(member.status==='revoked'?' · Revoked':'')+'</small></div>'
        +(member.can_revoke===true&&ws?(confirm
          ?'<div class="setup-project-revoke-confirm" role="group" aria-label="Confirm revoke access"><span>Revoke access for this user?</span><div><button type="button" class="setup-project-danger" data-project-revoke="'+esc(ws)+'"'+(busy?' disabled':'')+'>Confirm revoke</button><button type="button" class="setup-secondary" data-project-revoke-cancel'+(busy?' disabled':'')+'>Cancel</button></div></div>'
          :'<button type="button" class="setup-secondary" data-project-revoke-start="'+esc(ws)+'"'+(busy?' disabled':'')+'>Revoke access</button>'):'')+'</div>';
    }).join('');
    const peers=detail.peers.map(peer=>{
      const id=text(peer.user_id),confirm=revokeConfirm?.kind==='peer'&&revokeConfirm.id===id;
      return '<div class="setup-project-member"><div><b>'+esc(text(peer.label)||id)+'</b>'
        +(text(peer.label)?'<code>'+esc(id)+'</code>':'')+'<small>Local roster · '+esc(text(peer.role)||'member')+'</small></div>'
        +(confirm?'<div class="setup-project-revoke-confirm" role="group" aria-label="Confirm remove collaborator"><span>Remove this user from the local roster?</span><div><button type="button" class="setup-project-danger" data-project-peer="'+esc(id)+'"'+(busy?' disabled':'')+'>Confirm removal</button><button type="button" class="setup-secondary" data-project-revoke-cancel'+(busy?' disabled':'')+'>Cancel</button></div></div>'
          :'<button type="button" class="setup-secondary" data-project-peer-start="'+esc(id)+'"'+(busy?' disabled':'')+'>Remove collaborator</button>')+'</div>';
    }).join('');
    target.innerHTML=(members.length||detail.peers.length?'<h5>Project access</h5>'+rows+peers:'')
      +(detail.blockers.length?'<div class="setup-project-blockers">'+detail.blockers.map(blocker=>'<p>'+esc(text(blocker.message)||'Project access must be checked before removal.')+'</p>').join('')+'</div>':'')
      +(canRemove()?'<p class="setup-project-access-note is-good">Access checked. This local project can be removed.</p>':'');
    updateControls();
  }
  async function refreshDetail(){
    if(!selected)return;
    const id=selected,mine=++detailRevision;
    detail=null;revokeConfirm=null;detailLoading=true;paintAccess({loading:true});
    // A refreshed selection can share an in-flight GET. Queue a fresh request
    // after it settles so a result from before a revoke never enables deletion.
    if(refreshDetail.pending){detailQueued=true;return;}
    refreshDetail.pending=true;
    const res=await apiFetch(path(id)+'/removal');
    refreshDetail.pending=false;
    if(mine===detailRevision&&selected===id){
      detailLoading=false;
      if(res.ok&&validDetail(res.data,id)){detail=res.data;paintAccess();}
      else paintAccess({message:res.ok?'Access could not be verified. Try checking again.':failText(res)});
    }
    if(detailQueued){detailQueued=false;await refreshDetail();}
  }
  function openRemoval(id){
    if(busy||isProjectSharing(id))return;
    selected=id;detail=null;revokeConfirm=null;confirmInput.value='';
    $('#setup-project-removal-id').textContent=id;
    $('#setup-project-confirm-label').textContent=id;
    error('#setup-project-remove-error','');removal.hidden=false;draftChanged();
    $('#setup-project-removal-title').focus({preventScroll:true});removal.scrollIntoView({block:'nearest',behavior:'smooth'});
    refreshDetail();
  }
  function closeRemoval(){
    if(busy)return;
    selected=null;detail=null;detailRevision++;detailLoading=false;revokeConfirm=null;
    removal.hidden=true;confirmInput.value='';draftChanged();
  }
  async function revokeAccess(target,kind='workspace'){
    if(busy||isProjectSharing(selected)||!selected||revokeConfirm?.kind!==kind||revokeConfirm?.id!==target)return;
    const allowed=kind==='workspace'?detail?.members.some(member=>member.workspace_id===target&&member.can_revoke===true)
      :detail?.peers.some(peer=>peer.user_id===target);
    if(!allowed)return;
    const id=selected;busy=true;lastAction='revoke';paintAccess();
    error('#setup-project-remove-error','');
    const res=kind==='workspace'?await apiFetch(path(id)+'/revoke',{method:'POST',body:{workspace_id:target}})
      :await apiFetch(path(id)+'/peers/'+encodeURIComponent(target),{method:'DELETE'});
    busy=false;revokeConfirm=null;
    if(res.ok){changed(id,'access');toast(kind==='workspace'?'Access revoked':'Collaborator removed');}
    else error('#setup-project-remove-error',failText(res));
    await refreshDetail();
  }
  async function create(event){
    event.preventDefault();if(creating)return;
    const id=idInput.value.trim(),repository=repositoryInput.value.trim();
    if(!repository){error('#setup-project-add-error','Enter a Git repository URL.');repositoryInput.focus();return;}
    if(!PROJECT_ID.test(id)||id==='.'||id==='..'){
      error('#setup-project-add-error','Use letters, numbers, hyphens, underscores or dots for the folder name.');idInput.focus();return;
    }
    creating=true;error('#setup-project-add-error','');updateControls();
    form.querySelectorAll('input,button').forEach(node=>{node.disabled=true;});
    $('#setup-project-create').textContent='Cloning…';
    const res=await apiFetch(base,{method:'POST',body:{project_id:id,repository_url:repository}});
    creating=false;form.querySelectorAll('input,button').forEach(node=>{node.disabled=false;});updateControls();
    $('#setup-project-create').textContent='Clone project';
    if(res.ok&&res.data?.created===true&&res.data?.project_id===id){form.reset();form.hidden=true;folderEdited=false;draftChanged();changed(id,'added');toast('Project cloned');error('#setup-project-notice',text(res.data.warning));await refreshCatalog();}
    else error('#setup-project-add-error',res.ok?'Cloning could not be confirmed. Refresh the project list before retrying.':failText(res));
  }
  async function remove(event){
    event.preventDefault();if(!canRemove()||confirmInput.value!==selected)return;
    const id=selected;busy=true;lastAction='delete';updateControls();
    error('#setup-project-remove-error','');
    const res=await apiFetch(path(id),{method:'DELETE',body:{confirm_project_id:id}});
    busy=false;
    if(res.ok&&res.data?.removed===true&&res.data?.project_id===id){closeRemoval();changed(id,'removed');toast('Local project deleted');await refreshCatalog();}
    else{error('#setup-project-remove-error',res.ok?'Removal could not be confirmed. Refresh the project list before retrying.':failText(res));await refreshDetail();}
    updateControls();
  }
  function openAdd(){
    form.hidden=false;
    draftChanged();
    form.scrollIntoView({block:'nearest'});
    if(creating)return;
    repositoryInput.focus({preventScroll:true});
  }
  $('#setup-project-add').addEventListener('click',openAdd);
  $('#setup-project-cancel').addEventListener('click',()=>{if(creating)return;form.hidden=true;form.reset();folderEdited=false;error('#setup-project-add-error','');draftChanged();});
  $('#setup-project-refresh').addEventListener('click',()=>refresh());
  $('#setup-project-close').addEventListener('click',closeRemoval);
  $('#setup-project-recheck').addEventListener('click',()=>refreshDetail());
  form.addEventListener('submit',create);form.addEventListener('input',draftChanged);
  idInput.addEventListener('input',()=>{folderEdited=true;});
  repositoryInput.addEventListener('input',()=>{
    if(folderEdited)return;
    const repository=repositoryInput.value.trim().replace(/[?#].*$/,'').replace(/\/+$/,'');
    const name=repository.split(/[/:]/).pop()?.replace(/\.git$/i,'')||'';
    idInput.value=PROJECT_ID.test(name)?name:'';
  });
  $('#setup-project-remove-form').addEventListener('submit',remove);
  confirmInput.addEventListener('input',updateControls);
  el.addEventListener('click',event=>{
    const shareButton=event.target.closest('[data-project-share]');
    if(shareButton){if(!busy&&!creating)projectRows.get(shareButton.dataset.projectShare)?.share.open();return;}
    const removeButton=event.target.closest('[data-project-remove]');
    if(removeButton){openRemoval(removeButton.dataset.projectRemove);return;}
    const start=event.target.closest('[data-project-revoke-start]');
    if(start&&!busy){revokeConfirm={kind:'workspace',id:start.dataset.projectRevokeStart};paintAccess();return;}
    const peer=event.target.closest('[data-project-peer-start]');
    if(peer&&!busy){revokeConfirm={kind:'peer',id:peer.dataset.projectPeerStart};paintAccess();return;}
    if(event.target.closest('[data-project-revoke-cancel]')&&!busy){revokeConfirm=null;paintAccess();return;}
    const confirm=event.target.closest('[data-project-revoke]');
    if(confirm)revokeAccess(confirm.dataset.projectRevoke);
    const peerConfirm=event.target.closest('[data-project-peer]');
    if(peerConfirm)revokeAccess(peerConfirm.dataset.projectPeer,'peer');
  });
  async function refresh(){await Promise.all([refreshCatalog(),selected&&!busy?refreshDetail():Promise.resolve()]);}
  return {refresh,hasDraft,openAdd};
}
