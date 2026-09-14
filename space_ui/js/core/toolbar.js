import {PRIMARY_TABS,PROJECT_PAGES,AGENT_PAGES,INBOX_PAGES,EXPLORER_PAGES} from './navigation.js?v=20260914-unified1';
import {SETUP_SECTIONS} from './setup-sections.js?v=20260914-setuproutes1';

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
  let current=null,descriptor=null,search=null,pageQuery='',menuSignature=null;
  const pages=[...PROJECT_PAGES,...AGENT_PAGES,...INBOX_PAGES,...SETUP_SECTIONS.map(page=>({...page,parent:'setup'})),...EXPLORER_PAGES];
  const pageMenu=document.createElement('nav');pageMenu.className='ac page-finder';pageMenu.id='page-finder';
  pageMenu.setAttribute('aria-label','Matching pages');localSearch.appendChild(pageMenu);
  const pageSearch={placeholder:'Find a page…',label:'Find a page',getValue:()=>pageQuery,setValue:value=>{pageQuery=value;},navigation:true};
  function renderPages(){
    const signature=search?.navigation?pageQuery:null;
    if(signature===menuSignature)return;menuSignature=signature;
    pageMenu.replaceChildren();
    if(!search?.navigation||!pageQuery.trim()){pageMenu.classList.remove('is-open');return;}
    const terms=pageQuery.toLowerCase().trim().split(/\s+/);
    for(const page of pages){
      const section=PRIMARY_TABS.find(tab=>tab.id===page.parent)?.label||'Setup';
      const label=section+' · '+page.label;
      if(!terms.every(term=>label.toLowerCase().includes(term)))continue;
      const link=document.createElement('a');link.href='#/'+page.route;link.textContent=label;
      link.addEventListener('click',()=>{pageQuery='';pageMenu.classList.remove('is-open');});pageMenu.appendChild(link);
    }
    if(!pageMenu.children.length){const empty=document.createElement('span');empty.className='empty';empty.textContent='No matching pages';pageMenu.appendChild(empty);}
    pageMenu.classList.add('is-open');
  }
  const closeGraphMenus=()=>{
    for(const id of ['rootdd','qac','root-ac'])document.getElementById(id)?.classList.remove('is-open');
  };
  function render(){
    let config=null;
    try{config=typeof descriptor==='function'?descriptor():descriptor;}
    catch(err){console.error('Toolbar configuration failed:',err);}
    const graph=!!config?.graph;
    search=graph?null:config?.search&&typeof config.search.setValue==='function'?config.search:pageSearch;
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
    renderPages();
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
    if(event.isComposing)return;
    if(search?.navigation&&['ArrowDown','Enter'].includes(event.key)){
      const link=pageMenu.querySelector('a');if(link){event.preventDefault();if(event.key==='Enter')link.click();else{pageMenu.classList.add('is-open');link.focus();}}return;
    }
    if(event.key!=='Escape'||event.isComposing)return;
    event.preventDefault();event.stopPropagation();
    if(input.value)change('');else input.blur();
  });
  clear.addEventListener('click',()=>{change('');input.focus();});
  addEventListener('space:view',event=>{
    current=event.detail?.id||null;pageQuery='';
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
  pageMenu.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();pageMenu.classList.remove('is-open');input.focus();}
    if(['ArrowDown','ArrowUp'].includes(event.key)){
      const links=[...pageMenu.querySelectorAll('a')],at=links.indexOf(document.activeElement);
      if(at>=0){event.preventDefault();links[(at+(event.key==='ArrowDown'?1:links.length-1))%links.length]?.focus();}
    }
  });
  document.addEventListener('click',event=>{if(!localSearch.contains(event.target))pageMenu.classList.remove('is-open');});
  render();
}
