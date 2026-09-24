import {DEFAULT_THEME,loadTheme,saveTheme} from '../core/theme.js?v=20260922-theme4';

export function mountTheme(el,onDraftChange=()=>{}){
  el.innerHTML=`<div class="setup-card-head"><h3>Theme</h3><i id="theme-status" role="status">Loading…</i></div>
    <form id="theme-form">
      <fieldset disabled>
        <label for="theme-select">Workspace theme</label>
        <p class="setup-theme-intro">Choose the colors and typography that feel right for you.</p>
        <select id="theme-select" name="workspace-theme" aria-describedby="theme-description">
          <option value="space">Grove — default</option>
          <option value="quirq">Neon</option>
          <option value="midnight">Midnight</option>
          <option value="graphite">Graphite</option>
          <option value="linen">Linen — light</option>
        </select>
        <div class="theme-palette" data-preview="space"><span aria-hidden="true"><i></i><i></i><i></i></span><p id="theme-description"></p></div>
      </fieldset>
      <p class="setup-theme-note">Your workspace name and logo stay the same. Save to apply immediately.</p>
      <div id="theme-error" class="setup-form-error" role="alert" hidden></div>
      <div class="setup-actions"><button id="theme-save" class="setup-primary" type="submit" disabled>Save theme</button><button id="theme-retry" class="setup-secondary" type="button" hidden>Try again</button></div>
    </form>`;
  const form=el.querySelector('form'),fieldset=el.querySelector('fieldset');
  const select=el.querySelector('select'),save=el.querySelector('#theme-save');
  const retry=el.querySelector('#theme-retry'),error=el.querySelector('#theme-error'),status=el.querySelector('#theme-status');
  let saved=DEFAULT_THEME,loaded=false,busy=false,refreshing=false,revision=0,saveRevision=0,lastDirty=false;
  const selection=()=>select.value||DEFAULT_THEME;
  const descriptions={space:'Moss green and warm neutrals.',quirq:'Soft magenta, violet and amber on charcoal.',midnight:'Periwinkle blue and silver on near-black.',graphite:'Quiet black and grey. Clear, simple and focused.',linen:'Warm white, soft stone and burnt orange.'};
  const dirty=()=>loaded&&selection()!==saved;
  function showError(message=''){error.textContent=message;error.hidden=!message;}
  function render(){
    el.querySelector('.theme-palette').dataset.preview=selection();
    el.querySelector('#theme-description').textContent=descriptions[selection()];
    fieldset.disabled=busy||!loaded;
    save.disabled=busy||!loaded||!dirty();
    save.textContent=busy?'Saving…':'Save theme';
    form.setAttribute('aria-busy',String(busy||refreshing));
    status.textContent=busy?'Saving…':!loaded?(refreshing?'Loading…':'Unavailable'):dirty()?'Unsaved changes':'Saved';
    status.classList.toggle('is-pending',dirty());status.classList.toggle('is-good',loaded&&!dirty());
    if(dirty()!==lastDirty){lastDirty=dirty();onDraftChange(lastDirty);}
  }
  async function refresh(){
    if(busy||refreshing)return;
    const mine=revision,writesAtStart=saveRevision,hadDraft=dirty();
    refreshing=true;retry.hidden=true;render();
    const res=await loadTheme();
    refreshing=false;
    if(writesAtStart!==saveRevision){render();return;}
    if(!res.ok){showError(res.error||'Could not load themes. Try again.');retry.hidden=false;render();return;}
    const keepSelection=hadDraft||mine!==revision;
    saved=res.data.theme;loaded=true;
    if(!keepSelection)select.value=saved;
    showError();render();
  }
  form.addEventListener('change',()=>{++revision;showError();render();});
  retry.addEventListener('click',refresh);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!loaded||!dirty())return;
    ++revision;++saveRevision;busy=true;retry.hidden=true;showError();render();
    const res=await saveTheme(selection());busy=false;
    if(res.ok){saved=res.data.theme;select.value=saved;}
    else showError(res.error||'Could not save the theme. Try again.');
    render();
  });
  render();return{refresh};
}
