/* Read-only session relationships. A project is grouped within its runtime;
   only reported parent/subagent relationships are shown as containment. */
import {API_BASE} from '../core/api.js';
import {readSnapshot} from '../core/read-snapshot.js?v=20260914-unified1';

const text=value=>typeof value==='string'?value.trim():'';
const number=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;
const key=(...parts)=>JSON.stringify(parts);
const facts=rows=>rows.filter(([,value])=>value!==null&&value!==undefined&&value!=='').map(([label,value])=>[label,String(value)]);

export function buildAgentData(payload){
  const nodes=[{id:'agents',label:'Agents',kind:'root',route:'agents/sessions'}],edges=[];
  const warnings=[];
  const add=node=>{nodes.push(node);if(node.parentId)edges.push({source:node.parentId,target:node.id,label:'contains'});};
  if(!Array.isArray(payload?.sessions))return {title:'Agents',description:'Session activity is unavailable.',nodes,edges,warning:'Could not load agent sessions. Open the session list to retry.'};
  const sources=new Map();
  for(const source of Array.isArray(payload.meta?.sources)?payload.meta.sources:[]){
    if(text(source?.id))sources.set(source.id,{...source,label:text(source.label)||source.id});
  }
  const sessions=[],seen=new Set();
  for(const session of payload.sessions){
    if(!session||typeof session!=='object'||!text(session.id)){warnings.push('Some session records could not be read.');continue;}
    const agent=text(session.agent)||text(session.source)||'unknown';
    const sessionId=key(agent,text(session.key)||session.id);
    if(seen.has(sessionId))continue;
    seen.add(sessionId);sessions.push({session,agent,sessionId});
    if(!sources.has(agent))sources.set(agent,{id:agent,label:agent==='unknown'?'Unknown runtime':agent.replaceAll('_',' ')});
  }
  const loadedByAgent=new Map();
  for(const {agent} of sessions)loadedByAgent.set(agent,(loadedByAgent.get(agent)||0)+1);
  for(const [agent,source] of sources){
    const loaded=loadedByAgent.get(agent)||0;
    const total=number(payload.totals?.sessions_by_agent?.[agent])??number(source.session_count);
    const unavailable=source.available===false||source.status==='unavailable';
    if(unavailable)warnings.push(source.label+' telemetry is unavailable.');
    if(total!==null&&loaded<total)warnings.push(source.label+': showing '+loaded+' loaded sessions of '+total+' recorded.');
    add({id:key('runtime',agent),parentId:'agents',label:source.label,kind:'runtime',route:'agents/sessions',
      detail:unavailable?'Telemetry unavailable':loaded+' loaded sessions',
      meta:facts([['Loaded sessions',loaded],['Recorded sessions',total],['Telemetry',unavailable?'Unavailable':source.available===true?'Available':'Status unknown']])});
  }
  const projects=new Set();
  for(const {session:s,agent,sessionId} of sessions){
    // Full paths disambiguate same-named checkouts; missing paths stay unknown.
    const path=text(s.project_path),projectId=key('project',agent,path||'unknown');
    if(!projects.has(projectId)){
      projects.add(projectId);
      add({id:projectId,parentId:key('runtime',agent),label:path?(text(s.project)||path.split('/').filter(Boolean).pop()):'Unknown project',
        kind:'project',detail:path||'No project path was reported.',route:'agents/sessions',meta:facts([['Project path',path]])});
    }
    const id=key('session',sessionId),subagents=Array.isArray(s.subagents)?s.subagents:[];
    const tools=Array.isArray(s.tools)?s.tools.filter(tool=>text(tool?.name)):[];
    const total=number(s.total_tokens)??number(s.tokens);
    add({id,parentId:projectId,label:s.id,kind:'session',route:'agents/sessions',detail:text(s.started_at)||'Start time unavailable',
      meta:facts([['Session ID',s.id],['Runtime',sources.get(agent).label],['Project path',path],['Model',text(s.model)],
        ['Started',text(s.started_at)],['Ended',text(s.ended_at)],['Duration (seconds)',number(s.duration_sec)],
        ['Tokens (including listed subagents)',total],['Own tokens',number(s.own_tokens)],['Turns',number(s.turns)],
        ['Cost',s.cost_known===false?'Unavailable':number(s.cost)!==null?'Estimated $'+s.cost:null],
        ['Recorded tools',tools.map(tool=>tool.name+(number(tool.calls)!==null?' ('+tool.calls+' calls)':'')).join(', ')],
        ['Listed subagents',subagents.length]]),
      searchText:[s.id,s.key,agent,path,s.project,s.model,...tools.map(tool=>tool.name)].filter(value=>typeof value==='string').join(' ')});
    const children=new Set();
    for(const child of subagents){
      if(!text(child?.id)||children.has(child.id))continue;
      children.add(child.id);
      add({id:key('subagent',sessionId,child.id),parentId:id,label:child.id.split('/').pop(),kind:'subagent',route:'agents/sessions',
        detail:'Reported child of '+s.id,meta:facts([['Subagent ID',child.id],['Tokens',number(child.total_tokens)??number(child.tokens)],
          ['Turns',number(child.turns)],['Cost',child.cost_known===false?'Unavailable':number(child.cost)!==null?'Estimated $'+child.cost:null]])});
    }
  }
  const total=number(payload.totals?.sessions);
  if(total!==null&&sessions.length<total)warnings.push('The session list is capped; older sessions are not included in these views.');
  if(total===null)warnings.push('The total recorded session count is unavailable. These views contain loaded sessions only.');
  return {title:'Agents',description:sessions.length+' loaded sessions grouped by runtime and project. Parent token totals include their listed subagents.',
    nodes,edges,warning:[...new Set(warnings)].join(' ')};
}

export async function loadAgentData({timeoutMs=12000}={}){
  const res=await readSnapshot(API_BASE+'/xo/sessions.json',{timeoutMs});
  return buildAgentData(res.ok?res.data:null);
}
