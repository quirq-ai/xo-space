import {DEFAULT_BRANDING,brandingLogoURL,defaultBrandMark,loadBranding,saveBranding} from '../core/branding.js?v=20260921-branding1';

const MAX_LOGO_BYTES=2*1024*1024;
const LOGO_TYPES=new Set(['image/png','image/jpeg','image/webp']);

export function mountBranding(el,onDraftChange=()=>{}){
  el.innerHTML=`<div class="setup-card-head"><h3>Branding</h3><i id="branding-status" role="status">Loading…</i></div>
    <form id="branding-form" novalidate>
      <p class="setup-branding-intro">Make this workspace your own with a name and logo.</p>
      <div class="setup-branding-fields">
        <div class="setup-branding-controls">
          <label for="branding-name">Workspace name</label>
          <input id="branding-name" value="Space" maxlength="80" autocomplete="off" required aria-describedby="branding-name-help" disabled>
          <small id="branding-name-help">Shown in the header and browser tab. Up to 80 characters.</small>
          <label for="branding-logo">Workspace logo</label>
          <input id="branding-logo" type="file" accept="image/png,image/jpeg,image/webp" aria-describedby="branding-logo-help branding-logo-file" disabled>
          <small id="branding-logo-help">PNG, JPG or WebP. Up to 2 MB and 4096 × 4096 pixels.</small>
          <small id="branding-logo-file"></small>
          <button class="setup-secondary" id="branding-remove-logo" type="button" hidden>Remove logo</button>
        </div>
        <div class="setup-branding-preview"><span>Preview</span><div><span data-branding-preview-logo></span><b data-branding-preview-name>Space</b></div><small>Your workspace header</small></div>
      </div>
      <div class="setup-form-error" id="branding-error" role="alert" hidden></div>
      <div class="setup-actions"><button class="setup-primary" id="branding-save" type="submit" disabled>Save branding</button><button class="setup-secondary" id="branding-reset" type="button" disabled>Reset to defaults</button><button class="setup-secondary" id="branding-retry" type="button" hidden>Try again</button></div>
    </form>`;
  const form=el.querySelector('#branding-form'),name=el.querySelector('#branding-name');
  const upload=el.querySelector('#branding-logo'),save=el.querySelector('#branding-save');
  const remove=el.querySelector('#branding-remove-logo'),reset=el.querySelector('#branding-reset');
  const retry=el.querySelector('#branding-retry'),error=el.querySelector('#branding-error');
  const status=el.querySelector('#branding-status'),fileLabel=el.querySelector('#branding-logo-file');
  const previewName=el.querySelector('[data-branding-preview-name]'),previewLogo=el.querySelector('[data-branding-preview-logo]');
  let saved={...DEFAULT_BRANDING},loaded=false,busy=false,checking=false,refreshing=false;
  let selectedFile=null,previewURL=null,removeLogo=false,revision=0,lastDirty=false,renderedLogo;
  const dirty=()=>name.value!==saved.name||Boolean(selectedFile)||Boolean(removeLogo&&saved.logo_url);
  const logoURL=()=>previewURL||(removeLogo?null:brandingLogoURL(saved.logo_url));
  function showError(message=''){
    error.textContent=message;error.hidden=!message;
  }
  function clearFile(resetInput=true){
    if(previewURL)URL.revokeObjectURL(previewURL);
    previewURL=null;selectedFile=null;if(resetInput)upload.value='';
  }
  function showDefaultLogo(){
    const mark=defaultBrandMark();
    if(mark.style)mark.style.display='';
    previewLogo.replaceChildren(mark);
  }
  function render(){
    const locked=busy||checking||!loaded;
    name.disabled=locked;upload.disabled=locked;reset.disabled=locked;
    save.disabled=locked||!dirty();remove.disabled=locked;
    save.textContent=busy?'Saving…':'Save branding';
    form.setAttribute('aria-busy',String(busy||checking||refreshing));
    status.textContent=busy?'Saving…':checking?'Checking image…':!loaded?(refreshing?'Loading…':'Unavailable'):dirty()?'Unsaved changes':'Saved';
    status.classList.toggle('is-pending',loaded&&dirty());
    status.classList.toggle('is-good',loaded&&!dirty());
    previewName.textContent=name.value.trim()||'Space';
    const url=logoURL();
    if(url!==renderedLogo){
      renderedLogo=url;previewLogo.replaceChildren();
      if(url){
        const img=document.createElement('img');img.src=url;img.alt='Logo preview';
        img.addEventListener('error',()=>{if(previewLogo.contains(img))showDefaultLogo();});
        previewLogo.append(img);
      }else showDefaultLogo();
    }
    remove.hidden=!url;
    fileLabel.textContent=selectedFile?selectedFile.name:url?'Custom logo':removeLogo&&saved.logo_url?'Default logo will be restored when you save.':'Default logo';
    const hasDraft=loaded&&dirty();
    if(hasDraft!==lastDirty){lastDirty=hasDraft;onDraftChange(hasDraft);}
  }
  async function refresh(){
    // Refresh never discards a draft, a selected upload, or an in-flight save.
    if(busy||checking||refreshing)return;
    const mine=revision;
    refreshing=true;retry.hidden=true;render();
    const res=await loadBranding();
    refreshing=false;
    if(mine!==revision){render();return;}
    if(!res.ok){showError(res.error||'Could not load branding. Try again.');retry.hidden=false;render();return;}
    const hadDraft=loaded&&dirty();
    saved=res.data;loaded=true;
    if(!hadDraft){name.value=saved.name;clearFile();removeLogo=false;}
    showError();render();
  }
  name.addEventListener('input',()=>{++revision;showError();render();});
  upload.addEventListener('change',async()=>{
    const file=upload.files?.[0];
    if(!file)return;
    showError();
    if(!LOGO_TYPES.has(file.type)||file.size===0||file.size>MAX_LOGO_BYTES){
      upload.value='';showError('Choose a PNG, JPG or WebP image up to 2 MB.');return;
    }
    ++revision;checking=true;render();
    const url=URL.createObjectURL(file),img=new Image();
    try{
      img.src=url;await img.decode();
      if(img.naturalWidth>4096||img.naturalHeight>4096)throw new Error('Choose an image no larger than 4096 × 4096 pixels.');
      clearFile(false);selectedFile=file;previewURL=url;removeLogo=false;
    }catch(err){
      URL.revokeObjectURL(url);upload.value='';
      showError(err.message.startsWith('Choose')?err.message:'This image could not be read. Choose a valid PNG, JPG or WebP.');
    }finally{checking=false;render();}
  });
  remove.addEventListener('click',()=>{++revision;clearFile();removeLogo=true;showError();render();});
  reset.addEventListener('click',()=>{++revision;name.value='Space';clearFile();removeLogo=true;showError();render();});
  retry.addEventListener('click',refresh);
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if(busy||checking||!loaded)return;
    const value=name.value.trim();
    if(!value||Array.from(value).length>80||/[\u0000-\u001f\u007f]/.test(value)){
      showError('Enter a workspace name between 1 and 80 characters, without control characters.');name.focus();return;
    }
    if(!dirty())return;
    ++revision;busy=true;showError();retry.hidden=true;render();
    const body=new FormData();body.set('name',value);
    if(selectedFile)body.set('logo',selectedFile);
    else if(removeLogo)body.set('remove_logo','true');
    const res=await saveBranding(body);
    busy=false;
    if(res.ok){saved=res.data;name.value=saved.name;clearFile();removeLogo=false;}
    else showError(res.error||'Could not save branding. Your changes are still here.');
    render();
  });
  render();
  return{refresh};
}
