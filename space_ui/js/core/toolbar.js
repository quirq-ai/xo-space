/* The shell owns the input; views own their queries and filtering behavior.
   The Cmd+K trigger lives with Wiki and GitHub. Page search is a detail
   field for an active filter; Graph no longer keeps a second map field. */
export function initToolbar(){
  const topbar=document.querySelector('.topbar');
  const controls=document.getElementById('toolbar-controls');
  const localSearch=document.getElementById('view-search-wrap');
  const input=document.getElementById('view-search');
  const clear=document.getElementById('view-search-clear');
  const hint=document.getElementById('view-search-hint');
  const trigger=document.getElementById('cmdk-trigger');
  const triggerKbd=document.getElementById('cmdk-trigger-kbd');
  if(!topbar||!controls||!localSearch||!input||!clear||!trigger)return;
  trigger.addEventListener('click',()=>dispatchEvent(new CustomEvent('space:open-command-palette')));
  if(triggerKbd){
    const mac=/Mac|iPhone|iPad|iPod/i.test(navigator.platform||navigator.userAgent||'');
    const chord=mac?'\u2318K':'Ctrl K';
    triggerKbd.textContent=chord;
    trigger.title='Press '+chord+' to search';
    trigger.setAttribute('aria-label','Open search. Press '+chord+'.');
  }
  let current=null,descriptor=null,search=null;
  function render(){
    let config=null;
    try{config=typeof descriptor==='function'?descriptor():descriptor;}
    catch(err){console.error('Toolbar configuration failed:',err);}
    const graph=!!config?.graph;
    search=!graph&&config?.search&&typeof config.search.setValue==='function'?config.search:null;
    const mode=graph?'graph':search?'search':'none';
    topbar.dataset.toolbar=mode;
    const value=search?String(search.getValue?.()??''):'';
    /* The inline field is a detail view for an ACTIVE page filter. The Cmd+K
       trigger sits with Wiki/GitHub, so this slot only mounts once the
       current page has a non-empty query. */
    const showLocal=!!search&&value!=='';
    localSearch.hidden=!showLocal;
    controls.hidden=!showLocal;
    const meta=document.getElementById('fmeta');
    if(meta)meta.hidden=!graph;
    const disabled=!!config?.disabled;
    if(search){
      input.placeholder=search.placeholder||'Search this page…';
      input.setAttribute('aria-label',search.label||input.placeholder.replace(/…$/, ''));
      input.disabled=disabled||!!search.disabled;
      if(input.value!==value)input.value=value;
      clear.hidden=!value;
      clear.disabled=input.disabled;
      if(hint)hint.hidden=!!value;
    }
    const focused=document.activeElement;
    if(focused&&((localSearch.hidden&&localSearch.contains(focused))||(!search&&localSearch.contains(focused))))
      focused.blur();
  }
  function change(value){
    if(!search||input.disabled)return;
    try{search.setValue(value);}
    catch(err){console.error('Page search failed:',err);}
    render();
  }
  input.addEventListener('input',()=>change(input.value));
  input.addEventListener('keydown',event=>{
    if(event.key!=='Escape'||event.isComposing)return;
    event.preventDefault();event.stopPropagation();
    if(input.value)change('');else input.blur();
  });
  clear.addEventListener('click',()=>{change('');input.focus();});
  addEventListener('space:view',event=>{
    current=event.detail?.id||null;
    descriptor=event.detail?.toolbar||null;
    render();
  });
  addEventListener('space:toolbar',event=>{
    if(event.detail?.id!==current)return;
    descriptor=event.detail.toolbar||null;
    render();
  });
  addEventListener('keydown',event=>{
    const focused=document.activeElement;
    if(event.defaultPrevented||event.isComposing||event.ctrlKey||event.metaKey||event.altKey||
      /INPUT|TEXTAREA|SELECT/.test(focused?.tagName||'')||focused?.isContentEditable)return;
    if(event.key!=='/')return;
    /* Graph and searchable pages open the palette. Pages with no search leave
       `/` alone. */
    if(topbar.dataset.toolbar==='graph'||search){
      event.preventDefault();
      dispatchEvent(new CustomEvent('space:open-command-palette'));
    }
  });
  render();
}
