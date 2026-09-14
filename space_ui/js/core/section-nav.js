/* Secondary navigation belongs to the application shell. Views own their
   page headers and content; Setup publishes status without owning a second nav. */
import {PRIMARY_TABS,PROJECT_PAGES,AGENT_PAGES,INBOX_PAGES,EXPLORER_PAGES,DATA_VIEWS} from './navigation.js?v=20260914-unified1';
import {SETUP_SECTIONS} from './setup-sections.js?v=20260914-setuproutes1';

const SETUP_PAGES=SETUP_SECTIONS.map(page=>({...page,id:page.route,parent:'setup',section:'setup',setupId:page.id}));
const GROUPS={projects:[{id:'project-list',route:'projects/list',label:'Workspace',parent:'projects'},...PROJECT_PAGES.filter(page=>['time','sharing'].includes(page.id))],agents:AGENT_PAGES,inbox:INBOX_PAGES,setup:SETUP_PAGES};
const PAGES=new Map([...PROJECT_PAGES,...Object.values(GROUPS).flat(),...EXPLORER_PAGES].map(page=>[page.id,page]));

export function initSectionNav(){
  const nav=document.getElementById('section-nav'),stage=document.getElementById('stage');
  if(!nav||!stage||nav.dataset.initialized)return;
  nav.dataset.initialized='true';
  let parent=null,active=null,height=-1,width=-1,frame=0,statuses={};
  const lastList={projects:'projects/list',agents:'agents/sessions',inbox:'inbox/items',setup:'setup/workspace'};
  function measure(){
    const rect=nav.getBoundingClientRect();
    const next=nav.hidden?0:Math.ceil(rect.height),nextWidth=nav.hidden?0:Math.ceil(rect.width);
    if(next===height&&nextWidth===width)return;
    height=next;width=nextWidth;
    stage.style.setProperty('--section-nav-height',next+'px');
    document.documentElement.style.setProperty('--section-nav-inset',next+'px');
    cancelAnimationFrame(frame);
    frame=requestAnimationFrame(()=>dispatchEvent(new Event('resize')));
  }
  function revealCurrent(){
    const links=nav.querySelector('.section-nav-links'),link=links?.querySelector('[aria-current="page"]');
    if(!links||!link)return;
    const bounds=links.getBoundingClientRect(),selected=link.getBoundingClientRect();
    if(selected.left<bounds.left)links.scrollLeft-=bounds.left-selected.left;
    else if(selected.right>bounds.right)links.scrollLeft+=selected.right-bounds.right;
  }
  function updateStatuses(){
    if(parent!=='setup')return;
    for(const link of nav.querySelectorAll('[data-setup-go]')){
      const status=statuses[link.dataset.setupGo],small=link.querySelector('small');
      if(!small)continue;
      small.textContent=status?.text||'';small.hidden=!status?.text;
      small.className='space-sr-only'+(status?.tone?' is-'+status.tone:'');
    }
  }
  function render(group){
    const inner=document.createElement('div');inner.className='section-nav-inner';
    const heading=document.createElement('span');heading.className='section-nav-label';
    heading.textContent=PRIMARY_TABS.find(tab=>tab.id===group)?.label||group;
    const links=document.createElement('div');links.className='section-nav-links';
    for(const page of GROUPS[group]){
      const link=document.createElement('a');link.href='#/'+page.route;link.dataset.sectionPage=page.id;
      const text=document.createElement('span');text.className='section-nav-text';
      const label=document.createElement('b');label.textContent=page.label;text.appendChild(label);
      if(page.setupId){
        link.dataset.setupGo=page.setupId;link.setAttribute('aria-controls','setup-panel-'+page.setupId);
        const status=document.createElement('small');status.hidden=true;status.className='space-sr-only';text.appendChild(status);
      }
      link.appendChild(text);links.appendChild(link);
    }
    inner.append(heading,links);
    const modes=document.createElement('div');modes.className='space-view-switch space-segmented';
    modes.setAttribute('aria-label','View data as');
    for(const mode of DATA_VIEWS){
      const link=document.createElement('a');link.dataset.viewMode=mode;
      link.href='#/'+(mode==='list'?lastList[group]:group+'/'+mode);
      link.textContent=mode[0].toUpperCase()+mode.slice(1);
      link.title='View '+group+' as '+mode;modes.appendChild(link);
    }
    inner.appendChild(modes);
    nav.replaceChildren(inner);nav.setAttribute('aria-label',heading.textContent+' pages');
    nav.dataset.section=group;
    updateStatuses();
  }
  function sync(detail={}){
    const page=PAGES.get(detail.id)||(detail.route==='setup/server/details'?PAGES.get('setup/server'):null);
    const group=page?.parent||detail.tab;
    for(const section of stage.querySelectorAll('.view.has-section-nav'))section.classList.remove('has-section-nav');
    nav.hidden=!page||!GROUPS[group];
    stage.classList.toggle('has-page-navigation',!nav.hidden);
    if(nav.hidden){parent=null;active=null;measure();return;}
    if(group!==parent){parent=group;render(group);}
    active=page.id;
    const mode=page.mode||(group==='projects'&&['dashboard','graph','tree'].includes(page.id)?page.id==='tree'?'tree':'graph':'list');
    if(mode==='list')lastList[group]=page.route;
    for(const link of nav.querySelectorAll('[data-view-mode]')){
      if(link.dataset.viewMode==='list')link.href='#/'+lastList[group];
      if(link.dataset.viewMode===mode)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
    }
    document.getElementById('view-'+(detail.section||page.section||page.id))?.classList.add('has-section-nav');
    for(const link of nav.querySelectorAll('[data-section-page]')){
      if(link.dataset.sectionPage===(group==='projects'&&['dashboard','graph','tree'].includes(page.id)?'project-list':page.id))link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
    }
    measure();requestAnimationFrame(revealCurrent);
  }
  addEventListener('space:setup-status',event=>{statuses=event.detail?.statuses||{};updateStatuses();measure();});
  addEventListener('space:view',event=>sync(event.detail));
  addEventListener('resize',()=>{measure();if(active)revealCurrent();});
  if(typeof ResizeObserver==='function')new ResizeObserver(measure).observe(nav);
  measure();
}
