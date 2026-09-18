/* Primary sections and their pages are separate concepts. This is the shared
   route/label vocabulary for the registry, secondary navigation and handoffs. */
const pages=(parent,entries)=>Object.freeze(entries.map(([id,slug,label,aliases=[]])=>Object.freeze({
  id,route:slug?parent+'/'+slug:parent,label,aliases:Object.freeze(aliases),parent,nav:false,
})));

export const PROJECT_PAGES=pages('projects',[
  ['dashboard','overview','Overview',['dashboard']],
  ['project-list','data/list','List',['list','projects/list','projects/data','projects/files','projects/files/list']],
  ['graph','data/graph','Graph',['graph','projects/graph','projects/files/graph']],
  ['tree','data/tree','Tree',['tree','projects/tree','projects/files/tree']],
  ['time','timeline','Timeline',['time','timeline']],
  ['project-manage','manage','Manage',['setup/projects']],
]);
export const DATA_VIEWS=Object.freeze(PROJECT_PAGES.filter(page=>['project-list','graph','tree'].includes(page.id)));
export const PROJECT_SECTIONS=Object.freeze([
  PROJECT_PAGES.find(page=>page.id==='dashboard'),
  Object.freeze({id:'data',route:'projects/data',label:'Data',parent:'projects'}),
  PROJECT_PAGES.find(page=>page.id==='time'),
  PROJECT_PAGES.find(page=>page.id==='project-manage'),
]);
/* Trends absorbed the former Tools and Models pages; their routes stay
   valid as aliases so old deep links land on the merged page. */
export const AGENT_PAGES=Object.freeze(pages('agents',[
  ['agents-overview','overview','Overview'],
  ['agents-sessions','sessions','Sessions'],
  ['agents-trends','trends','Trends',['agents/tools','agents/models']],
  ['agents-configure','configure','Configure'],
]).map(page=>Object.freeze({...page,section:'agents'})));
/* Work: the Inbox tab renamed (first Feed, then Work) and reduced to three pages
   (docs/work-and-workitems.md). Inbox is everything that needs the person;
   Live is what is happening right now (the calendar beside a live log
   stream); History is what happened, over a range, with charts, split
   Space | Projects, with project sharing inside it. Every old Inbox,
   Activity and Sharing route stays valid as an alias so deep links and the
   wiki's hand-offs land. The Inbox page's route is the section itself. */
export const WORK_PAGES=Object.freeze(pages('work',[
  ['work','','Inbox',['inbox','inbox/items','feed']],
  ['work-live','live','Live',['inbox/jobs','inbox/connections','feed/live']],
  ['work-history','history','History',['work/activity','feed/activity','feed/history','inbox/activity','inbox/sharing-activity','sharing','projects/sharing','inbox/sharing']],
]));
/* The Inbox pages, kept only for views/inbox.js and views/inbox-activity.js
   until the Work's first PR removes them; the shell no longer registers
   either view. */
export const INBOX_PAGES=Object.freeze(pages('inbox',[
  ['inbox-items','items','Items'],
  ['inbox-connections','connections','Connections'],
  ['inbox-jobs','jobs','Jobs'],
  ['inbox-activity','activity','Activity'],
  ['inbox-sharing-activity','sharing-activity','Sharing activity'],
  ['sharing','sharing','Sharing',['sharing','projects/sharing']],
]).map(page=>Object.freeze({...page,section:['inbox-items','inbox-connections','inbox-jobs'].includes(page.id)?'inbox':page.id})));

export const PRIMARY_TABS=Object.freeze([
  {id:'projects',label:'Projects',defaultView:'projects/overview'},
  {id:'agents',label:'Agents',defaultView:'agents/overview',aliases:['sessions']},
  {id:'work',label:'Work',defaultView:'work',aliases:['inbox','feed']},
  {id:'setup',label:'Setup',defaultView:'setup/workspace'},
].map(tab=>Object.freeze(tab)));

export function projectPage(id){return PROJECT_PAGES.find(page=>page.id===id);}
export function isProjectRoute(route){
  return route==='projects'||PROJECT_PAGES.some(page=>page.id===route||page.route===route||page.aliases.includes(route));
}
