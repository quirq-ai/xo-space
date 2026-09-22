import {DEFAULT_THEME,loadTheme,saveTheme} from '../core/theme.js?v=20260922-theme1';

export function mountTheme(el,onDraftChange=()=>{}){
  el.innerHTML=`<div class="setup-card-head"><h3>Theme</h3><i id="theme-status" role="status">Loading…</i></div>
    <form id="theme-form">
      <fieldset disabled><legend>Choose your workspace theme</legend>
        <p class="setup-theme-intro">Set the colors and typography across your workspace.</p>
        <div class="setup-theme-options">
          <label class="setup-theme-option" for="theme-space">
            <span class="theme-sample theme-sample-space" aria-hidden="true"><span class="theme-sample-bar"><b>Space</b><i></i><i></i></span><span class="theme-sample-body"><b>Your workspace</b><span></span><span></span><em>Explore projects</em></span></span>
            <span class="theme-option-title"><input id="theme-space" type="radio" name="workspace-theme" value="space" checked><b>Space</b><small>Default</small></span>
            <span class="theme-option-description">Green accents · Inter</span>
          </label>
          <label class="setup-theme-option" for="theme-quirq">
            <span class="theme-sample theme-sample-quirq" aria-hidden="true"><span class="theme-sample-bar"><b>Quirq</b><i></i><i></i></span><span class="theme-sample-body"><b>Your workspace</b><span></span><span></span><em>Explore projects</em></span></span>
            <span class="theme-option-title"><input id="theme-quirq" type="radio" name="workspace-theme" value="quirq"><b>Quirq</b></span>
            <span class="theme-option-description">Monochrome &amp; spectrum · Inter, Poppins &amp; JetBrains Mono</span>
          </label>
        </div>
      </fieldset>
      <p class="setup-theme-note">Your workspace name and logo stay the same. Save to apply immediately.</p>
      <div id="theme-error" class="setup-form-error" role="alert" hidden></div>
      <div class="setup-actions"><button id="theme-save" class="setup-primary" type="submit" disabled>Save theme</button><button id="theme-retry" class="setup-secondary" type="button" hidden>Try again</button></div>
    </form>`;
  const form=el.querySelector('form'),fieldset=el.querySelector('fieldset');
  const radios=[...el.querySelectorAll('input')],save=el.querySelector('#theme-save');
  const retry=el.querySelector('#theme-retry'),error=el.querySelector('#theme-error'),status=el.querySelector('#theme-status');
  let saved=DEFAULT_THEME,loaded=false,busy=false,refreshing=false,revision=0,saveRevision=0,lastDirty=false;
  const selection=()=>radios.find(radio=>radio.checked)?.value||DEFAULT_THEME;
  const dirty=()=>loaded&&selection()!==saved;
  function showError(message=''){error.textContent=message;error.hidden=!message;}
  function render(){
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
    if(!keepSelection)radios.forEach(radio=>{radio.checked=radio.value===saved;});
    showError();render();
  }
  form.addEventListener('change',()=>{++revision;showError();render();});
  retry.addEventListener('click',refresh);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!loaded||!dirty())return;
    ++revision;++saveRevision;busy=true;retry.hidden=true;showError();render();
    const res=await saveTheme(selection());busy=false;
    if(res.ok){saved=res.data.theme;radios.forEach(radio=>{radio.checked=radio.value===saved;});}
    else showError(res.error||'Could not save the theme. Try again.');
    render();
  });
  render();return{refresh};
}
