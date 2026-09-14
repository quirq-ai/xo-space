/* Primary sections and their pages are separate concepts. This is the shared
   route/label vocabulary for the registry, secondary navigation and handoffs. */
const pages=(parent,entries)=>Object.freeze(entries.map(([id,slug,label,aliases=[]])=>Object.freeze({
  id,route:parent+'/'+slug,label,aliases:Object.freeze(aliases),parent,nav:false,
})));

export const PROJECT_PAGES=pages('projects',[
  ['dashboard','overview','Overview',['dashboard']],
  ['project-list','list','List',['list']],
  ['graph','graph','Graph',['graph']],
  ['tree','tree','Tree',['tree']],
  ['sharing','sharing','Sharing',['sharing']],
  ['time','timeline','Timeline',['time','timeline']],
]);
export const AGENT_PAGES=Object.freeze(pages('agents',[
  ['agents-overview','overview','Overview'],
  ['agents-sessions','sessions','Sessions',['agents/list']],
  ['agents-tools','tools','Tools'],
  ['agents-models','models','Models'],
  ['agents-trends','trends','Trends'],
]).map(page=>Object.freeze({...page,section:'agents'})));
export const INBOX_PAGES=Object.freeze(pages('inbox',[
  ['inbox-items','items','Items',['inbox/list']],
  ['inbox-connections','connections','Connections'],
  ['inbox-jobs','jobs','Jobs'],
]).map(page=>Object.freeze({...page,section:'inbox'})));

export const PRIMARY_TABS=Object.freeze([
  {id:'projects',label:'Projects',defaultView:'projects/list'},
  {id:'agents',label:'Agents',defaultView:'agents/sessions',aliases:['sessions']},
  {id:'inbox',label:'Inbox',defaultView:'inbox/items'},
  {id:'setup',label:'Setup',defaultView:'setup/workspace'},
].map(tab=>Object.freeze(tab)));

export function projectPage(id){return PROJECT_PAGES.find(page=>page.id===id);}
export function isProjectRoute(route){
  return route==='projects'||PROJECT_PAGES.some(page=>page.id===route||page.route===route||page.aliases.includes(route));
}

/* Representation is independent of the primary data section. Existing pages
   remain the List workspace; relationship views share the same renderer. */
export const DATA_VIEWS=Object.freeze(['list','graph','tree']);
export const EXPLORER_PAGES=Object.freeze(['agents','inbox','setup'].flatMap(parent=>
  ['graph','tree'].map(mode=>Object.freeze({id:parent+'-'+mode,route:parent+'/'+mode,
    label:mode==='graph'?'Graph':'Tree',parent,section:parent+'-explorer',mode,nav:false}))));
