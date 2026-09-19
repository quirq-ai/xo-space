/* Section navigation is shell chrome. Native links keep history, deep links
   and opening a page in another tab available without importing the router.

   The links of a tab come from the registry's page list (the 'space:pages'
   event registry.js dispatches with every view's id, route, label, order,
   parent and flags) merged over the legacy tables in navigation.js: a
   legacy entry keeps its place, a registered view with the same route
   takes it over (a spec page replacing a hand-written one), a view the
   tables do not know is added, and the result is ordered by order then
   label. Projects keeps its Data entry, which stands for List, Graph and
   Tree. A view with secondary:false is never listed (a detail page); one
   with sectionNav:false hides this bar while it is active (Setup draws its
   own section list). */
import {PRIMARY_TABS,PROJECT_PAGES,PROJECT_SECTIONS,DATA_VIEWS,AGENT_PAGES,INBOX_PAGES} from './navigation.js';
import {toast} from './ui.js';

const LEGACY={projects:PROJECT_SECTIONS,agents:AGENT_PAGES,inbox:INBOX_PAGES};
const LEGACY_PAGES=new Map([...PROJECT_PAGES,...AGENT_PAGES,...INBOX_PAGES].map(page=>[page.id,page]));
const DATA_IDS=new Set(DATA_VIEWS.map(page=>page.id));
const UNTITLED_PAGES=new Set(['dashboard','graph','tree','sharing']);
const pageActions=new Map();
let refreshActions=()=>{};
let registered=[];

/* The entries of one tab, in display order. Exported for the tests. */
export function pagesFor(tab,views=registered){
  const entries=new Map();
  /* A table entry stays only while a registered view answers its route
     (Data stands for List, Graph and Tree); a page whose module switched
     off has no listed view left, so it leaves. Before the registry has
     announced anything the tables stand alone. */
  const listed=views.filter(v=>v&&v.secondary!==false);
  const answered=page=>!listed.length||listed.some(v=>v.route===page.route||(page.id==='data'&&DATA_IDS.has(v.id)));
  (LEGACY[tab]||[]).forEach((page,i)=>{
    if(answered(page))entries.set(page.route,{id:page.id,route:page.route,label:page.label,order:(i+1)*10,legacy:true});
  });
  for(const view of views){
    if(!view||view.parent!==tab||view.secondary===false)continue;
    if(tab==='projects'&&DATA_IDS.has(view.id))continue; /* the Data entry stands for these */
    const existing=entries.get(view.route);
    if(existing)entries.set(view.route,{...existing,id:view.id,label:view.label||existing.label,order:view.order||existing.order});
    else entries.set(view.route,{id:view.id,route:view.route,label:view.label||view.id,order:view.order||0});
  }
  return[...entries.values()].sort((a,b)=>(a.order-b.order)||String(a.label).localeCompare(String(b.label)));
}
const entriesKey=entries=>entries.map(e=>e.id+'='+e.route+'='+e.label).join('|');

/* Views retain their own nodes, listeners and state; the shell places only
   the active page's actions beside the section-wide controls. */
export function setSectionActions(pageId,node){
  if(node)pageActions.set(pageId,node);else pageActions.delete(pageId);
  refreshActions();
}

export function initSectionNav({refreshCurrentView}){
  const nav=document.getElementById('section-nav');
  const stage=document.getElementById('stage');
  const graphRoot=document.getElementById('graph-root');
  if(!nav||!stage||nav.dataset.initialized)return;
  nav.dataset.initialized='true';
  let parent=null,active=null,height=-1,frame=0,refreshable=false,rendered='',lastDetail=null;
  let lastData=DATA_VIEWS[0];

  function refreshState({busy=false,available=false}={}){
    const button=nav.querySelector('[data-page-refresh]');
    if(!button)return;
    refreshable=available;
    button.hidden=parent!=='projects'&&!available;
    button.disabled=busy||!available;
    button.setAttribute('aria-busy',String(busy));
    button.textContent=busy?'Refreshing…':'Refresh';
    measure();
  }

  function measure(){
    const next=nav.hidden?0:Math.ceil(nav.getBoundingClientRect().height);
    if(next===height)return;
    height=next;
    stage.style.setProperty('--section-nav-height',next+'px');
    document.documentElement.style.setProperty('--section-nav-inset',next+'px');
    cancelAnimationFrame(frame);
    frame=requestAnimationFrame(()=>dispatchEvent(new Event('resize')));
  }

  function revealCurrent(){
    const links=nav.querySelector('.section-nav-links');
    const scope=links?.querySelector('[data-section-page][aria-current="page"]');
    if(!links||!scope)return;
    const bounds=links.getBoundingClientRect(),selected=scope.getBoundingClientRect();
    if(selected.right>bounds.right)links.scrollLeft+=selected.right-bounds.right;
    else if(selected.left<bounds.left)links.scrollLeft-=bounds.left-selected.left;
  }

  function placeActions(){
    const slot=nav.querySelector('.section-page-actions');
    if(!slot)return;
    const node=pageActions.get(active);
    if(slot.firstElementChild!==node)slot.replaceChildren(...(node?[node]:[]));
    slot.hidden=!node;
    const tools=nav.querySelector('.section-nav-tools');
    if(tools)tools.hidden=parent!=='projects'&&!node&&!refreshable;
    measure();
  }
  refreshActions=placeActions;

  function render(group,entries){
    const inner=document.createElement('div');inner.className='section-nav-inner';
    const label=PRIMARY_TABS.find(tab=>tab.id===group)?.label||group;
    const links=document.createElement('div');links.className='section-nav-links';
    for(const page of entries){
      const link=document.createElement('a');
      link.href='#/'+(page.id==='data'?lastData.route:page.route);link.textContent=page.label;
      link.dataset.sectionPage=page.id;
      links.appendChild(link);
    }
    inner.appendChild(links);
    const tools=document.createElement('div');tools.className='section-nav-tools';
    const slot=document.createElement('div');slot.className='section-page-actions';slot.hidden=true;
    tools.appendChild(slot);
    const quick=document.createElement('div');quick.className='section-nav-actions';
    if(group==='projects'){
      const actions=document.createElement('div');actions.className='section-nav-actions';
      if(graphRoot)actions.appendChild(graphRoot);
      tools.appendChild(actions);
    }
    const refresh=document.createElement('button');
    refresh.id=group==='projects'?'project-refresh':'section-refresh';refresh.dataset.pageRefresh='';refresh.type='button';
    refresh.className='section-nav-action';refresh.textContent='Refresh';
    refresh.addEventListener('click',async()=>{
      const page=active;
      try{await refreshCurrentView();}
      catch(error){console.error('Page refresh failed:',error);if(active===page)toast('Could not refresh this page. Try again.');}
    });
    quick.appendChild(refresh);tools.appendChild(quick);inner.appendChild(tools);
    const title=document.createElement('h1');title.className='section-page-title';title.hidden=true;
    // Keep the same picker node mounted. Its controller owns state,
    // listeners and visibility; section navigation only places it.
    nav.replaceChildren(inner,title,...(group!=='projects'&&graphRoot?[graphRoot]:[]));
    nav.dataset.section=group;
    nav.setAttribute('aria-label',label+' pages');
    rendered=entriesKey(entries);
  }

  function sync(detail={}){
    lastDetail=detail;
    const legacy=LEGACY_PAGES.get(detail.id);
    const view=registered.find(v=>v.id===detail.id);
    const group=detail.tab||legacy?.parent||null;
    const entries=group?pagesFor(group):[];
    for(const section of stage.querySelectorAll('.view.has-section-nav'))section.classList.remove('has-section-nav');
    nav.hidden=!detail.id||!group||!entries.length||(view?view.sectionNav===false:!legacy);
    if(nav.hidden){parent=null;active=null;rendered='';measure();return;}
    if(group!==parent||entriesKey(entries)!==rendered){render(group,entries);parent=group;}
    active=detail.id;
    refreshState({busy:detail.refreshing,available:detail.refreshable});
    const dataActive=group==='projects'&&DATA_IDS.has(detail.id);
    if(dataActive&&legacy)lastData=legacy;
    const section=document.getElementById('view-'+(detail.section||legacy?.section||detail.id));
    section?.classList.add('has-section-nav');
    for(const link of nav.querySelectorAll('[data-section-page]')){
      if(link.dataset.sectionPage===(dataActive?'data':detail.id))link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
      if(link.dataset.sectionPage==='data')link.href='#/'+lastData.route;
    }
    placeActions();
    const title=nav.querySelector('.section-page-title');
    title.hidden=!UNTITLED_PAGES.has(detail.id);title.textContent=detail.label||legacy?.label||'';
    measure();
    requestAnimationFrame(revealCurrent);
  }

  addEventListener('space:view',event=>sync(event.detail));
  addEventListener('space:pages',event=>{
    registered=Array.isArray(event.detail?.views)?event.detail.views:[];
    /* The page list changed under an open tab: redraw its links in place. */
    if(lastDetail&&active)sync(lastDetail);
  });
  addEventListener('space:refresh-state',event=>{
    if(event.detail?.id===active)refreshState({busy:event.detail.busy,available:refreshable});
  });
  addEventListener('resize',()=>{measure();if(active)revealCurrent();});
  if(typeof ResizeObserver==='function')new ResizeObserver(measure).observe(nav);
  measure();
}
