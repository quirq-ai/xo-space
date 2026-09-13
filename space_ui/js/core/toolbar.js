/* The shell owns the input; views own their queries and filtering behavior.
   Graph autocomplete keeps its own input so its permanent listeners never
   receive text intended for another page. */
export function initToolbar(){
  const topbar=document.querySelector('.topbar');
  const controls=document.getElementById('toolbar-controls');
  const graphRoot=document.getElementById('graph-root');
  const graphSearch=document.getElementById('graph-search');
  const localSearch=document.getElementById('view-search-wrap');
  const input=document.getElementById('view-search');
  const clear=document.getElementById('view-search-clear');
  const hint=document.getElementById('view-search-hint');
  const graphInput=document.getElementById('q');
  if(!topbar||!controls||!graphRoot||!graphSearch||!localSearch||!input||!clear)return;
  let current=null,descriptor=null,search=null;
  const closeGraphMenus=()=>{
    for(const id of ['rootdd','qac','root-ac'])document.getElementById(id)?.classList.remove('is-open');
  };
  function render(){
    let config=null;
    try{config=typeof descriptor==='function'?descriptor():descriptor;}
    catch(err){console.error('Toolbar configuration failed:',err);}
    const graph=!!config?.graph;
    search=!graph&&config?.search&&typeof config.search.setValue==='function'?config.search:null;
    const mode=graph?'graph':search?'search':'none';
    topbar.dataset.toolbar=mode;
    controls.hidden=mode==='none';
    graphRoot.hidden=!graph;
    graphSearch.hidden=!graph;
    localSearch.hidden=!search;
    if(!graph)closeGraphMenus();
    const meta=document.getElementById('fmeta');
    if(meta)meta.hidden=!graph;
    const disabled=!!config?.disabled;
    if(graphInput)graphInput.disabled=disabled;
    const rootButton=document.getElementById('root-btn');
    if(rootButton)rootButton.disabled=disabled;
    if(search){
      input.placeholder=search.placeholder||'Search this page…';
      input.setAttribute('aria-label',search.label||input.placeholder.replace(/…$/, ''));
      input.disabled=disabled||!!search.disabled;
      const value=String(search.getValue?.()??'');
      if(input.value!==value)input.value=value;
      clear.hidden=!value;
      clear.disabled=input.disabled;
      if(hint)hint.hidden=!!value;
    }
    const focused=document.activeElement;
    if(focused&&controls.contains(focused)&&(
      controls.hidden||(!graph&&(graphRoot.contains(focused)||graphSearch.contains(focused)))||
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
    const target=topbar.dataset.toolbar==='graph'?graphInput:search?input:null;
    if(!target||target.disabled||controls.hidden)return;
    event.preventDefault();target.focus();
  });
  render();
}
