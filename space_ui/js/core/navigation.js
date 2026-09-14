/* Primary sections and their pages are separate concepts. This is the shared
   route/label vocabulary for the registry, secondary navigation and handoffs. */
const pages=(parent,entries)=>Object.freeze(entries.map(([id,slug,label,aliases=[]])=>Object.freeze({
  id,route:parent+'/'+slug,label,aliases:Object.freeze(aliases),parent,nav:false,
})));

export const PROJECT_PAGES=pages('projects',[
  ['dashboard','overview','Overview',['dashboard']],
  ['project-list','files/list','List',['list','projects/list','projects/files']],
  ['graph','files/graph','Graph',['graph','projects/graph']],
  ['tree','files/tree','Tree',['tree','projects/tree']],
  ['time','timeline','Timeline',['time','timeline']],
]);
export const FILE_VIEWS=Object.freeze(PROJECT_PAGES.filter(page=>['project-list','graph','tree'].includes(page.id)));
export const PROJECT_SECTIONS=Object.freeze([
  PROJECT_PAGES.find(page=>page.id==='dashboard'),
  Object.freeze({id:'files',route:'projects/files',label:'Files',parent:'projects'}),
  PROJECT_PAGES.find(page=>page.id==='time'),
]);
export const AGENT_PAGES=Object.freeze(pages('agents',[
  ['agents-overview','overview','Overview'],
  ['agents-sessions','sessions','Sessions'],
  ['agents-tools','tools','Tools'],
  ['agents-models','models','Models'],
  ['agents-trends','trends','Trends'],
]).map(page=>Object.freeze({...page,section:'agents'})));
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
  {id:'inbox',label:'Inbox',defaultView:'inbox/items'},
  {id:'setup',label:'Setup',defaultView:'setup/workspace'},
].map(tab=>Object.freeze(tab)));

export function projectPage(id){return PROJECT_PAGES.find(page=>page.id===id);}
export function isProjectRoute(route){
  return route==='projects'||PROJECT_PAGES.some(page=>page.id===route||page.route===route||page.aliases.includes(route));
}
