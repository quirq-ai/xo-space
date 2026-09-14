/* Project creation and local removal. The server is the authority for every
   removal check; a missing or stale sharing response never enables deletion. */
import {API_BASE,apiFetch,failText} from '../core/api.js';
import {esc,toast} from '../core/ui.js';
import {createProjectShare,isProjectSharing} from '../core/project-share.js?v=20260914-manage1';
import {createProjectIssues} from '../core/project-issues.js?v=20260914-details1';

const base=API_BASE+'/api/xo-projects';
const path=id=>base+'/'+encodeURIComponent(id);
const text=value=>typeof value==='string'?value.trim():'';
const PROJECT_ID=/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;
let nextDetailsId=0;

/* Only verified GitHub origins become browser URLs. Credentials, fragments,
   query strings, path traversal and other hosts never reach the clipboard. */
export function githubBrowserUrl(remote){
  const raw=text(remote);if(!raw||/[\s\\%]/.test(raw)||/(?:^|\/)\.{1,2}(?:\/|$)/.test(raw))return null;
  let pathname;
  const ssh=raw.match(/^git@github\.com:([^?#]+)$/i);
  if(ssh)pathname=ssh[1];
  else try{
    const url=new URL(raw);
    if(url.hostname!=='github.com'||url.password||url.search||url.hash
      ||!['https:','ssh:'].includes(url.protocol)
      ||(url.protocol==='https:'&&(url.username||url.port))
      ||(url.protocol==='ssh:'&&((url.username&&url.username!=='git')||(url.port&&url.port!=='22'))))return null;
    pathname=url.pathname.replace(/^\//,'');
  }catch{return null;}
  const slug=pathname.replace(/\/$/,'').replace(/\.git$/i,'');
  if(!/^[a-zA-Z0-9_-]+\/[a-zA-Z0-9_.-]+$/.test(slug)||['.','..'].includes(slug.split('/')[1]))return null;
  return 'https://github.com/'+slug;
}

export function mountProjectManagement(el,{onChange=()=>{},onDraftChange=()=>{},onStatusChange=()=>{},onViewActivity=()=>{}}={}){
  let items=[],catalogRevision=0,catalogLoading=false,catalogQueued=false,creating=false;
  let selected=null,detail=null,detailRevision=0,detailLoading=false,detailQueued=false;
  let busy=false,lastAction='',revokeConfirm=null,folderEdited=false;
  const projectRows=new Map();
  el.classList.add('manage-projects');
  el.innerHTML=`<header class="manage-project-head"><h1 id="manage-projects-title" tabindex="-1">Manage projects <span id="manage-project-count"></span></h1>
      <div class="manage-project-tools"><button class="setup-primary" type="button" id="manage-project-add">Add project</button></div></header>
    <p id="manage-project-notice" class="manage-project-notice" role="status" hidden></p>
    <form id="manage-project-form" class="manage-project-form" hidden novalidate>
      <h2>Add a project</h2><p>Clone a Git repository into your projects folder.</p>
      <label for="manage-project-repository">Repository URL</label><input id="manage-project-repository" type="text" autocomplete="off" spellcheck="false" placeholder="https://github.com/owner/project.git" required>
      <label for="manage-project-id">Folder name</label><input id="manage-project-id" autocomplete="off" spellcheck="false" placeholder="my-project" maxlength="120" required>
      <div class="setup-form-error" id="manage-project-add-error" role="alert" hidden></div>
      <div class="setup-actions"><button class="setup-primary" id="manage-project-create" type="submit">Clone project</button><button class="setup-secondary" id="manage-project-cancel" type="button">Cancel</button></div>
    </form>
    <div id="manage-project-list" class="manage-project-list"><p id="manage-project-list-status" class="setup-empty" role="status">Loading projects…</p></div>
    <section id="manage-project-removal" class="manage-project-removal" aria-labelledby="manage-project-removal-title" hidden>
      <header><div><h2 id="manage-project-removal-title" tabindex="-1">Remove project</h2><code id="manage-project-removal-id"></code></div><button class="setup-secondary" id="manage-project-close" type="button">Cancel</button></header>
      <p>Delete this project folder and all its files from this Space. Remote repositories and backups remain.</p>
      <div id="manage-project-access" aria-live="polite"></div>
      <div class="setup-form-error" id="manage-project-remove-error" role="alert" hidden></div>
      <form id="manage-project-remove-form" novalidate><label for="manage-project-confirm">Type <code id="manage-project-confirm-label"></code> to confirm</label><input id="manage-project-confirm" autocomplete="off" spellcheck="false">
        <div class="setup-actions"><button class="manage-project-delete" id="manage-project-delete" type="submit" disabled>Delete local project</button><button class="setup-secondary" id="manage-project-recheck" type="button">Check access again</button></div></form>
    </section>`;
  const $=selector=>el.querySelector(selector);
  const form=$('#manage-project-form'),idInput=$('#manage-project-id'),repositoryInput=$('#manage-project-repository');
  const removal=$('#manage-project-removal'),confirmInput=$('#manage-project-confirm');
  const list=$('#manage-project-list'),listStatus=$('#manage-project-list-status');
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
    $('#manage-project-delete').disabled=!canRemove()||confirmInput.value!==selected;
    $('#manage-project-delete').textContent=busy&&lastAction==='delete'?'Deleting…':'Delete local project';
    $('#manage-project-close').disabled=busy;
    $('#manage-project-recheck').disabled=busy||detailLoading;
    confirmInput.disabled=busy;
    for(const [id,row] of projectRows){
      row.removeButton.disabled=busy||isProjectSharing(id);
      row.shareButton.disabled=busy||creating;
      row.share.setDisabled(busy||creating);
    }
  }
  function paintList(message=''){
    $('#manage-project-count').textContent=catalogLoading?'':String(items.length);
    const focused=list.contains(document.activeElement)?document.activeElement:null,scrollTop=list.scrollTop;
    listStatus.textContent=message||(!items.length?'No projects in this Space yet.':'');
    listStatus.hidden=!listStatus.textContent;listStatus.classList.toggle('is-error',Boolean(message));
    const ids=new Set(items.map(item=>item.id));
    for(const [id,row] of projectRows)if(!ids.has(id)){
      row.share.destroy();row.issues.destroy();row.metadataController?.abort();row.element.remove();projectRows.delete(id);
    }
    let position=listStatus.nextElementSibling;
    for(const item of items){
      let row=projectRows.get(item.id);
      if(!row){
        const element=document.createElement('div');element.className='manage-project-row';element.dataset.projectId=item.id;
        const detailsId='manage-project-details-'+(++nextDetailsId);
        element.innerHTML='<button type="button" class="manage-project-toggle" data-project-toggle="'+esc(item.id)+'" aria-expanded="false" aria-controls="'+detailsId+'">'
          +'<span class="manage-project-chevron" aria-hidden="true">›</span><span class="manage-project-summary"></span></button><div class="manage-project-row-actions">'
          +'<button class="setup-secondary" type="button" data-project-copy="'+esc(item.id)+'">Copy GitHub URL</button>'
          +'<button class="setup-secondary" type="button" data-project-share="'+esc(item.id)+'">Share</button>'
          +'<button class="setup-secondary" type="button" data-project-remove="'+esc(item.id)+'">Remove</button></div>'
          +'<p class="manage-project-copy-result" role="status" hidden></p>'
          +'<div class="manage-project-details" id="'+detailsId+'" hidden>'
          +'<div class="manage-project-overview"><p class="manage-project-description" hidden></p><dl class="manage-project-metadata"></dl>'
          +'<button class="setup-secondary" type="button" data-project-activity="'+esc(item.id)+'">View activity</button></div></div>';
        const share=createProjectShare({projectId:item.id,onDraftChange:draftChanged,
          onBusyChange:()=>sharingChanged(item.id)});
        row={element,share,expanded:false,item,metadata:null,metadataLoaded:false,metadataPending:null,metadataError:'',
          copyButton:element.querySelector('[data-project-copy]'),copyResult:element.querySelector('.manage-project-copy-result'),
          toggle:element.querySelector('[data-project-toggle]'),details:element.querySelector('.manage-project-details'),
          metadataNode:element.querySelector('.manage-project-metadata'),description:element.querySelector('.manage-project-description'),
          summary:element.querySelector('.manage-project-summary'),
          shareButton:element.querySelector('[data-project-share]'),removeButton:element.querySelector('[data-project-remove]')};
        row.issues=createProjectIssues({projectId:item.id});row.details.appendChild(row.issues.element);
        share.setTrigger(row.shareButton);element.insertBefore(share.element,row.details);projectRows.set(item.id,row);
      }
      row.item=item;
      const html='<b>'+esc(item.display_name||item.id)+'</b><code>'+esc(item.id)+'</code>';
      if(row.summary.innerHTML!==html)row.summary.innerHTML=html;
      row.description.textContent=text(item.description);row.description.hidden=!row.description.textContent;
      paintMetadata(row);
      row.share.setLabel(item.display_name||item.id);
      row.shareButton.setAttribute('aria-label','Share '+(item.display_name||item.id));
      row.copyButton.setAttribute('aria-label','Copy GitHub URL for '+(item.display_name||item.id));
      if(row.element!==position)list.insertBefore(row.element,position);
      position=row.element.nextElementSibling;
    }
    list.scrollTop=scrollTop;
    if(focused?.isConnected&&focused.getClientRects().length)focused.focus({preventScroll:true});
    updateControls();
  }
  function paintMetadata(row){
    const item=row.item,meta=row.metadata;
    const created=text(item.created_at),date=created&&Number.isFinite(Date.parse(created))?new Date(created).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'Not recorded';
    const url=githubBrowserUrl(meta?.git?.remote_url);
    const fields=[['Project ID','<code>'+esc(item.id)+'</code>'],[item.unscaffolded===true?'Folder modified':'Created',esc(date)],
      ['Space metadata',item.unscaffolded===true?'Not present':item.unscaffolded===false?'Present':'Not recorded']];
    if(text(meta?.pid))fields.push(['Project UUID','<code>'+esc(meta.pid)+'</code>']);
    if(text(meta?.owner_user_id))fields.push(['Owner',esc(meta.owner_user_id)]);
    if(text(meta?.git?.default_branch))fields.push(['Default branch','<code>'+esc(meta.git.default_branch)+'</code>']);
    fields.push(['GitHub',url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(url.replace('https://github.com/',''))+'</a>'
      :esc(row.metadataPending?'Loading…':row.metadataError||(row.metadataLoaded?'No GitHub remote recorded.':'Not loaded'))]);
    const html=fields.map(([label,value])=>'<div><dt>'+label+'</dt><dd>'+value+'</dd></div>').join('');
    if(row.metadataNode.innerHTML!==html)row.metadataNode.innerHTML=html;
  }
  function loadMetadata(row,{refresh=false}={}){
    if(row.metadataPending)return row.metadataPending;
    if(row.metadataLoaded&&!refresh)return Promise.resolve(row.metadata);
    const controller=new AbortController();row.metadataController=controller;
    let timer;
    const timeout=new Promise(resolve=>{timer=setTimeout(()=>{controller.abort();resolve({ok:false,error:'Project details took too long to load. Try again.'});},12000);});
    row.metadataPending=(async()=>{
      try{
        const response=await Promise.race([apiFetch(path(row.item.id)+'/file?relative_path=.xo%2Fproject.json',{signal:controller.signal}),timeout]);
        if(projectRows.get(row.item.id)!==row)return null;
        if(response.status===404){row.metadata=null;row.metadataLoaded=true;row.metadataError='';return null;}
        if(!response.ok){row.metadataError=failText(response);return null;}
        if(response.data?.project_id!==row.item.id||response.data?.relative_path!=='.xo/project.json'||response.data.truncated===true)throw new Error('invalid metadata');
        const metadata=JSON.parse(response.data.content);
        if(!metadata||typeof metadata!=='object'||Array.isArray(metadata))throw new Error('invalid metadata');
        row.metadata=metadata;row.metadataLoaded=true;row.metadataError='';return metadata;
      }catch{
        if(projectRows.get(row.item.id)===row)row.metadataError='Project details could not be read. Try again.';
        return null;
      }finally{
        clearTimeout(timer);row.metadataPending=null;row.metadataController=null;
        if(projectRows.get(row.item.id)===row)paintMetadata(row);
      }
    })();
    paintMetadata(row);return row.metadataPending;
  }
  function toggleProject(id){
    const row=projectRows.get(id);if(!row)return;
    row.expanded=!row.expanded;row.toggle.setAttribute('aria-expanded',String(row.expanded));row.details.hidden=!row.expanded;
    if(row.expanded){loadMetadata(row);row.issues.load();}
  }
  async function copyGithub(id){
    const row=projectRows.get(id);if(!row||row.copying)return;
    row.copying=true;row.copyButton.disabled=true;row.copyButton.textContent='Copying…';
    row.copyResult.hidden=true;row.copyResult.classList.remove('is-error');
    try{
      const metadata=await loadMetadata(row,{refresh:true});
      if(projectRows.get(id)!==row)return;
      const url=githubBrowserUrl(metadata?.git?.remote_url);
      if(row.metadataError)throw new Error(row.metadataError);
      if(!url)throw new Error('No GitHub URL is recorded for this project.');
      try{await navigator.clipboard.writeText(url);}catch{throw new Error('Could not copy the URL. Open the project details to use its GitHub link.');}
      row.copyResult.textContent='GitHub URL copied.';
    }catch(error){row.copyResult.textContent=error.message;row.copyResult.classList.add('is-error');}
    finally{row.copying=false;row.copyButton.disabled=false;row.copyButton.textContent='Copy GitHub URL';row.copyResult.hidden=false;}
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
    catalogLoading=true;
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
    if(catalogQueued){catalogQueued=false;await refreshCatalog();}
  }
  function paintAccess({loading=false,message=''}={}){
    const target=$('#manage-project-access');
    if(loading){target.innerHTML='<p class="manage-project-access-note" role="status">Checking project access…</p>';updateControls();return;}
    if(!detail){target.innerHTML='<p class="manage-project-access-note is-error">'+esc(message||'Access could not be verified. Try checking again.')+'</p>';updateControls();return;}
    const members=detail.members;
    const rows=members.map(member=>{
      const ws=text(member.workspace_id),confirm=revokeConfirm?.kind==='workspace'&&revokeConfirm.id===ws;
      return '<div class="manage-project-member"><div><b>'+esc(text(member.label)||ws)+'</b>'
        +(text(member.label)?'<code>'+esc(ws)+'</code>':'')+'<small>'+esc(text(member.role)||'Collaborator')
        +(member.is_self?' · You':'')+(member.status==='revoked'?' · Revoked':'')+'</small></div>'
        +(member.can_revoke===true&&ws?(confirm
          ?'<div class="manage-project-revoke-confirm" role="group" aria-label="Confirm revoke access"><span>Revoke access for this user?</span><div><button type="button" class="manage-project-danger" data-project-revoke="'+esc(ws)+'"'+(busy?' disabled':'')+'>Confirm revoke</button><button type="button" class="setup-secondary" data-project-revoke-cancel'+(busy?' disabled':'')+'>Cancel</button></div></div>'
          :'<button type="button" class="setup-secondary" data-project-revoke-start="'+esc(ws)+'"'+(busy?' disabled':'')+'>Revoke access</button>'):'')+'</div>';
    }).join('');
    const peers=detail.peers.map(peer=>{
      const id=text(peer.user_id),confirm=revokeConfirm?.kind==='peer'&&revokeConfirm.id===id;
      return '<div class="manage-project-member"><div><b>'+esc(text(peer.label)||id)+'</b>'
        +(text(peer.label)?'<code>'+esc(id)+'</code>':'')+'<small>Local roster · '+esc(text(peer.role)||'member')+'</small></div>'
        +(confirm?'<div class="manage-project-revoke-confirm" role="group" aria-label="Confirm remove collaborator"><span>Remove this user from the local roster?</span><div><button type="button" class="manage-project-danger" data-project-peer="'+esc(id)+'"'+(busy?' disabled':'')+'>Confirm removal</button><button type="button" class="setup-secondary" data-project-revoke-cancel'+(busy?' disabled':'')+'>Cancel</button></div></div>'
          :'<button type="button" class="setup-secondary" data-project-peer-start="'+esc(id)+'"'+(busy?' disabled':'')+'>Remove collaborator</button>')+'</div>';
    }).join('');
    target.innerHTML=(members.length||detail.peers.length?'<h3>Project access</h3>'+rows+peers:'')
      +(detail.blockers.length?'<div class="manage-project-blockers">'+detail.blockers.map(blocker=>'<p>'+esc(text(blocker.message)||'Project access must be checked before removal.')+'</p>').join('')+'</div>':'')
      +(canRemove()?'<p class="manage-project-access-note is-good">Access checked. This local project can be removed.</p>':'');
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
    $('#manage-project-removal-id').textContent=id;
    $('#manage-project-confirm-label').textContent=id;
    error('#manage-project-remove-error','');removal.hidden=false;draftChanged();
    $('#manage-project-removal-title').focus({preventScroll:true});removal.scrollIntoView({block:'nearest',behavior:'smooth'});
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
    error('#manage-project-remove-error','');
    const res=kind==='workspace'?await apiFetch(path(id)+'/revoke',{method:'POST',body:{workspace_id:target}})
      :await apiFetch(path(id)+'/peers/'+encodeURIComponent(target),{method:'DELETE'});
    busy=false;revokeConfirm=null;
    if(res.ok){changed(id,'access');toast(kind==='workspace'?'Access revoked':'Collaborator removed');}
    else error('#manage-project-remove-error',failText(res));
    await refreshDetail();
  }
  async function create(event){
    event.preventDefault();if(creating)return;
    const id=idInput.value.trim(),repository=repositoryInput.value.trim();
    if(!repository){error('#manage-project-add-error','Enter a Git repository URL.');repositoryInput.focus();return;}
    if(!PROJECT_ID.test(id)||id==='.'||id==='..'){
      error('#manage-project-add-error','Use letters, numbers, hyphens, underscores or dots for the folder name.');idInput.focus();return;
    }
    creating=true;error('#manage-project-add-error','');updateControls();
    form.querySelectorAll('input,button').forEach(node=>{node.disabled=true;});
    $('#manage-project-create').textContent='Cloning…';
    const res=await apiFetch(base,{method:'POST',body:{project_id:id,repository_url:repository}});
    creating=false;form.querySelectorAll('input,button').forEach(node=>{node.disabled=false;});updateControls();
    $('#manage-project-create').textContent='Clone project';
    if(res.ok&&res.data?.created===true&&res.data?.project_id===id){form.reset();form.hidden=true;folderEdited=false;draftChanged();changed(id,'added');toast('Project cloned');error('#manage-project-notice',text(res.data.warning));await refreshCatalog();}
    else error('#manage-project-add-error',res.ok?'Cloning could not be confirmed. Refresh the project list before retrying.':failText(res));
  }
  async function remove(event){
    event.preventDefault();if(!canRemove()||confirmInput.value!==selected)return;
    const id=selected;busy=true;lastAction='delete';updateControls();
    error('#manage-project-remove-error','');
    const res=await apiFetch(path(id),{method:'DELETE',body:{confirm_project_id:id}});
    busy=false;
    if(res.ok&&res.data?.removed===true&&res.data?.project_id===id){closeRemoval();changed(id,'removed');toast('Local project deleted');await refreshCatalog();}
    else{error('#manage-project-remove-error',res.ok?'Removal could not be confirmed. Refresh the project list before retrying.':failText(res));await refreshDetail();}
    updateControls();
  }
  function openAdd(){
    form.hidden=false;
    draftChanged();
    form.scrollIntoView({block:'nearest'});
    if(creating)return;
    repositoryInput.focus({preventScroll:true});
  }
  $('#manage-project-add').addEventListener('click',openAdd);
  $('#manage-project-cancel').addEventListener('click',()=>{if(creating)return;form.hidden=true;form.reset();folderEdited=false;error('#manage-project-add-error','');draftChanged();});
  $('#manage-project-close').addEventListener('click',closeRemoval);
  $('#manage-project-recheck').addEventListener('click',()=>refreshDetail());
  form.addEventListener('submit',create);form.addEventListener('input',draftChanged);
  idInput.addEventListener('input',()=>{folderEdited=true;});
  repositoryInput.addEventListener('input',()=>{
    if(folderEdited)return;
    const repository=repositoryInput.value.trim().replace(/[?#].*$/,'').replace(/\/+$/,'');
    const name=repository.split(/[/:]/).pop()?.replace(/\.git$/i,'')||'';
    idInput.value=PROJECT_ID.test(name)?name:'';
  });
  $('#manage-project-remove-form').addEventListener('submit',remove);
  confirmInput.addEventListener('input',updateControls);
  el.addEventListener('click',event=>{
    const toggle=event.target.closest('[data-project-toggle]');
    if(toggle){toggleProject(toggle.dataset.projectToggle);return;}
    const copy=event.target.closest('[data-project-copy]');
    if(copy){copyGithub(copy.dataset.projectCopy);return;}
    const activity=event.target.closest('[data-project-activity]');
    if(activity){onViewActivity(activity.dataset.projectActivity);return;}
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
  async function refresh(){
    await Promise.all([refreshCatalog(),selected&&!busy?refreshDetail():Promise.resolve()]);
    await Promise.all([...projectRows.values()].filter(row=>row.expanded).flatMap(row=>[
      loadMetadata(row,{refresh:true}),row.issues.load({refresh:true}),
    ]));
  }
  return {refresh,hasDraft,openAdd};
}
