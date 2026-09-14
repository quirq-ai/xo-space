/* The shell owns the input; views own their queries and filtering behavior.
   Graph autocomplete keeps its own input so its permanent listeners never
   receive text intended for another page. */
export function initToolbar(){
  const topbar=document.querySelector('.topbar');
  const controls=document.getElementById('toolbar-controls');
  const graphSearch=document.getElementById('graph-search');
  const localSearch=document.getElementById('view-search-wrap');
  const input=document.getElementById('view-search');
  const clear=document.getElementById('view-search-clear');
  const hint=document.getElementById('view-search-hint');
  const graphInput=document.getElementById('q');
  const trigger=document.getElementById('cmdk-trigger');
  const triggerKbd=document.getElementById('cmdk-trigger-kbd');
  if(!topbar||!controls||!graphSearch||!localSearch||!input||!clear)return;
  /* The compact Cmd+K trigger is the navbar's default search affordance; it
     opens the command palette, which owns navigation and page search. */
  if(trigger){
    trigger.addEventListener('click',()=>dispatchEvent(new CustomEvent('space:open-command-palette')));
    if(triggerKbd){
      const mac=/Mac|iPhone|iPad|iPod/i.test(navigator.platform||navigator.userAgent||'');
      triggerKbd.textContent=mac?'\u2318K':'Ctrl K';
    }
  }
  let current=null,descriptor=null,search=null;
  const closeGraphMenus=()=>{
    document.getElementById('qac')?.classList.remove('is-open');
  };
  function render(){
    let config=null;
    try{config=typeof descriptor==='function'?descriptor():descriptor;}
    catch(err){console.error('Toolbar configuration failed:',err);}
    const graph=!!config?.graph;
    search=!graph&&config?.search&&typeof config.search.setValue==='function'?config.search:null;
    const mode=graph?'graph':search?'search':'none';
    topbar.dataset.toolbar=mode;
    const value=search?String(search.getValue?.()??''):'';
    /* The inline field is now a detail view for an ACTIVE page filter: the
       compact Cmd+K trigger is the default entry, so the field only shows once
       the current page has a non-empty query (set and cleared through the
       palette's "search this page"), and an empty page reads as just the
       trigger. */
    const showLocal=!!search&&value!=='';
    graphSearch.hidden=!graph;
    localSearch.hidden=!showLocal;
    controls.hidden=!graph&&!showLocal;
    if(!graph)closeGraphMenus();
    const meta=document.getElementById('fmeta');
    if(meta)meta.hidden=!graph;
    const disabled=!!config?.disabled;
    if(graphInput)graphInput.disabled=disabled;
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
    if(focused&&(
      (!graph&&graphSearch.contains(focused))||
      (controls.hidden&&controls.contains(focused))||
      (!search&&localSearch.contains(focused))
    ))focused.blur();
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
    closeGraphMenus();
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
    if(topbar.dataset.toolbar==='graph'){
      if(graphInput&&!graphInput.disabled&&!controls.hidden){event.preventDefault();graphInput.focus();}
      return;
    }
    /* On a page that has a search, `/` opens the palette (that page's search
       lives inside it now). Pages with no search leave `/` alone. */
    if(search){event.preventDefault();dispatchEvent(new CustomEvent('space:open-command-palette'));}
  });
  render();
}
