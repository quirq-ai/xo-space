/* One vocabulary for Setup navigation, status and URLs. The old agent and
   activity section names still reach their controls in Intelligence layer. */
const section=value=>Object.freeze({...value,route:'setup/'+value.id});
export const SETUP_STEPS=Object.freeze([
  section({id:'workspace',label:'Workspace',number:1}),
  section({id:'intelligence',label:'Intelligence layer',number:2}),
]);

export const SETUP_MANAGE=Object.freeze([
  section({id:'connectors',label:'Connectors',description:'Apps, access and polling',aliases:Object.freeze(['connectors'])}),
  section({id:'secrets',label:'Secrets',description:'Environment values',aliases:Object.freeze(['secrets'])}),
  section({id:'commands',label:'Commands',description:'Run and view results'}),
  section({id:'server',label:'Server',description:'Updates and restart'}),
]);

export const SETUP_SECTIONS=Object.freeze([...SETUP_STEPS,...SETUP_MANAGE]);
const sections=new Map(SETUP_SECTIONS.map(section=>[section.id,section.id]));
sections.set('agent','intelligence');
sections.set('activity','intelligence');

export function resolveSetupSection(id){
  return typeof id==='string'?(sections.get(id)||null):null;
}

export function setupSectionRoute(id){
  const section=resolveSetupSection(id);
  return section?'setup/'+section:null;
}
