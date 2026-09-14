/* Section navigation is shell chrome. Native links keep history, deep links
   and opening a page in another tab available without importing the router. */
import {PRIMARY_TABS,PROJECT_PAGES,AGENT_PAGES,INBOX_PAGES} from './navigation.js?v=20260914-navigation1';

const GROUPS={projects:PROJECT_PAGES,agents:AGENT_PAGES,inbox:INBOX_PAGES};
const PAGES=new Map(Object.values(GROUPS).flat().map(page=>[page.id,page]));
const CONTEXT={
  dashboard:'Projects grouped by environment.',
  graph:'Explore projects, folders and files.',
  tree:'Browse the workspace hierarchy.',
  sharing:'Manage shared projects and workspace access.',
};

export function initSectionNav(){
  const nav=document.getElementById('section-nav');
  const stage=document.getElementById('stage');
  if(!nav||!stage||nav.dataset.initialized)return;
  nav.dataset.initialized='true';
  let parent=null,active=null,height=-1,frame=0;

  function measure(){
    const next=nav.hidden?0:Math.ceil(nav.getBoundingClientRect().height);
    if(next===height)return;
    height=next;
    stage.style.setProperty('--section-nav-height',next+'px');
    cancelAnimationFrame(frame);
    frame=requestAnimationFrame(()=>dispatchEvent(new Event('resize')));
  }

  function revealCurrent(){
    const links=nav.querySelector('.section-nav-links');
    const link=nav.querySelector('[aria-current="page"]');
    if(!links||!link)return;
    const bounds=links.getBoundingClientRect(),selected=link.getBoundingClientRect();
    if(selected.left<bounds.left)links.scrollLeft-=bounds.left-selected.left;
    else if(selected.right>bounds.right)links.scrollLeft+=selected.right-bounds.right;
  }

  function render(group){
    const inner=document.createElement('div');inner.className='section-nav-inner';
    const heading=document.createElement('span');heading.className='section-nav-label';
    heading.textContent=PRIMARY_TABS.find(tab=>tab.id===group)?.label||group;
    const links=document.createElement('div');links.className='section-nav-links';
    for(const page of GROUPS[group]){
      const link=document.createElement('a');
      link.href='#/'+page.route;link.textContent=page.label;
      link.dataset.sectionPage=page.id;
      links.appendChild(link);
    }
    inner.append(heading,links);
    if(group==='projects'){
      const manage=document.createElement('a');
      manage.className='section-nav-action';manage.href='#/setup/projects';
      manage.textContent='Manage projects';inner.appendChild(manage);
    }
    const context=document.createElement('div');context.className='section-page-context';
    context.hidden=true;
    const title=document.createElement('h1');
    const description=document.createElement('p');context.append(title,description);
    nav.replaceChildren(inner,context);
    nav.setAttribute('aria-label',heading.textContent+' pages');
  }

  function sync(detail={}){
    const page=PAGES.get(detail.id);
    const group=page?.parent||detail.tab;
    for(const section of stage.querySelectorAll('.view.has-section-nav'))section.classList.remove('has-section-nav');
    nav.hidden=!page||!GROUPS[group];
    if(nav.hidden){parent=null;active=null;measure();return;}
    if(group!==parent){render(group);parent=group;}
    active=page.id;
    const section=document.getElementById('view-'+(detail.section||page.section||page.id));
    section?.classList.add('has-section-nav');
    for(const link of nav.querySelectorAll('[data-section-page]')){
      if(link.dataset.sectionPage===page.id)link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
    }
    const context=nav.querySelector('.section-page-context');
    context.hidden=!Object.hasOwn(CONTEXT,page.id);
    if(!context.hidden){
      context.querySelector('h1').textContent=page.label;
      context.querySelector('p').textContent=CONTEXT[page.id];
    }
    measure();
    requestAnimationFrame(revealCurrent);
  }

  addEventListener('space:view',event=>sync(event.detail));
  addEventListener('resize',()=>{measure();if(active)revealCurrent();});
  if(typeof ResizeObserver==='function')new ResizeObserver(measure).observe(nav);
  measure();
}
