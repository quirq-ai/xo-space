/* Section navigation is shell chrome. Native links keep history, deep links
   and opening a page in another tab available without importing the router. */
import {PRIMARY_TABS,PROJECT_PAGES,PROJECT_SECTIONS,DATA_VIEWS,AGENT_PAGES,INBOX_PAGES} from './navigation.js?v=20260915-agents2';

const GROUPS={projects:PROJECT_SECTIONS,agents:AGENT_PAGES,inbox:INBOX_PAGES};
const PAGES=new Map([...PROJECT_PAGES,...AGENT_PAGES,...INBOX_PAGES].map(page=>[page.id,page]));
const DATA_IDS=new Set(DATA_VIEWS.map(page=>page.id));
const UNTITLED_PAGES=new Set(['dashboard','graph','tree','sharing']);
const pageActions=new Map();
let refreshActions=()=>{};

/* Views retain their own nodes, listeners and state; the shell places only
   the active page's actions beside the section-wide controls. */
export function setSectionActions(pageId,node){
  if(node)pageActions.set(pageId,node);else pageActions.delete(pageId);
  refreshActions();
}

export function initSectionNav(){
  const nav=document.getElementById('section-nav');
  const stage=document.getElementById('stage');
  const graphRoot=document.getElementById('graph-root');
  if(!nav||!stage||nav.dataset.initialized)return;
  nav.dataset.initialized='true';
  let parent=null,active=null,height=-1,frame=0;
  let lastData=DATA_VIEWS[0];

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
    if(tools)tools.hidden=parent!=='projects'&&!node;
    measure();
  }
  refreshActions=placeActions;

  function render(group){
    const inner=document.createElement('div');inner.className='section-nav-inner';
    const label=PRIMARY_TABS.find(tab=>tab.id===group)?.label||group;
    const links=document.createElement('div');links.className='section-nav-links';
    for(const page of GROUPS[group]){
      const link=document.createElement('a');
      link.href='#/'+(page.id==='data'?lastData.route:page.route);link.textContent=page.label;
      link.dataset.sectionPage=page.id;
      links.appendChild(link);
    }
    inner.appendChild(links);
    const tools=document.createElement('div');tools.className='section-nav-tools';
    const slot=document.createElement('div');slot.className='section-page-actions';slot.hidden=true;
    tools.appendChild(slot);
    if(group==='projects'){
      const actions=document.createElement('div');actions.className='section-nav-actions';
      if(graphRoot)actions.appendChild(graphRoot);
      tools.appendChild(actions);
    }
    inner.appendChild(tools);
    const title=document.createElement('h1');title.className='section-page-title';title.hidden=true;
    // Keep the same picker node mounted. Its controller owns state,
    // listeners and visibility; section navigation only places it.
    nav.replaceChildren(inner,title,...(group!=='projects'&&graphRoot?[graphRoot]:[]));
    nav.dataset.section=group;
    nav.setAttribute('aria-label',label+' pages');
  }

  function sync(detail={}){
    const page=PAGES.get(detail.id);
    const group=page?.parent||detail.tab;
    for(const section of stage.querySelectorAll('.view.has-section-nav'))section.classList.remove('has-section-nav');
    nav.hidden=!page||!GROUPS[group];
    if(nav.hidden){parent=null;active=null;measure();return;}
    if(group!==parent){render(group);parent=group;}
    active=page.id;
    const dataActive=group==='projects'&&DATA_IDS.has(page.id);
    if(dataActive)lastData=page;
    const section=document.getElementById('view-'+(detail.section||page.section||page.id));
    section?.classList.add('has-section-nav');
    for(const link of nav.querySelectorAll('[data-section-page]')){
      if(link.dataset.sectionPage===(dataActive?'data':page.id))link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
      if(link.dataset.sectionPage==='data')link.href='#/'+lastData.route;
    }
    placeActions();
    const title=nav.querySelector('.section-page-title');
    title.hidden=!UNTITLED_PAGES.has(page.id);title.textContent=page.label;
    measure();
    requestAnimationFrame(revealCurrent);
  }

  addEventListener('space:view',event=>sync(event.detail));
  addEventListener('resize',()=>{measure();if(active)revealCurrent();});
  if(typeof ResizeObserver==='function')new ResizeObserver(measure).observe(nav);
  measure();
}
