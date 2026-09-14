/* Factual Setup summaries from /api/runtime-config. These describe settings
   and filesystem checks, never verified authentication or watcher health. */
const summary=(label,tone='muted')=>({label,tone});
const inaccessible=path=>path?.exists===false||path?.readable===false;
const accessible=path=>path?.exists===true&&path?.readable===true;
const WATCHER_FIELDS=['watcher_enabled','watcher_source_mode','watcher_interval_seconds'];

function projectsSummary(projectStatus){
  if(projectStatus?.status==='error')return summary('Unavailable','error');
  if(projectStatus?.status==='ready'){
    const count=projectStatus.count;
    return Number.isInteger(count)&&count>=0
      ?summary(count?count+' '+(count===1?'project':'projects'):'No projects',count?'good':'muted')
      :summary('Not checked');
  }
  return projectStatus?.status==='idle'||projectStatus?.status==='loading'
    ?summary('Checking'):summary('Not checked');
}

export function setupSteps(runtimeData,projectStatus={status:'idle',count:0}){
  const projects=projectsSummary(projectStatus);
  if(!runtimeData)return{
    workspace:summary('Checking'),intelligence:summary('Checking'),projects,next:null
  };
  const configured=runtimeData.configured||{},applied=runtimeData.applied||{};
  const roots=runtimeData.roots||{},paths=runtimeData.paths||{};
  const rootsPending=roots.change_required===true;
  // Read-only projects can still be browsed. Machine-local state needs writes.
  const foldersBlocked=inaccessible(paths.projects)||inaccessible(paths.state)||paths.state?.writable===false;
  const foldersChecked=accessible(paths.projects)&&accessible(paths.state)&&paths.state.writable===true;
  const workspace=rootsPending?summary('Apply changes','pending')
    :foldersBlocked?summary('Check folders','error')
      :foldersChecked?summary('Folders set','good'):summary('Not checked');

  const agentName=typeof configured.agent_name==='string'?configured.agent_name:'';
  const source=Array.isArray(runtimeData.agents)?runtimeData.agents.find(item=>item?.name===agentName):null;
  const agentPending=Boolean(agentName&&applied.agent_name&&agentName!==applied.agent_name);
  const agentMissing=source?.binary_available===false||inaccessible(source?.home);
  const agentChecked=Boolean(agentName&&agentName===applied.agent_name
    &&source?.binary_available===true&&accessible(source?.home));

  const activityPending=WATCHER_FIELDS.some(key=>configured[key]!==undefined&&applied[key]!==undefined
    &&configured[key]!==applied[key]);
  const activityKnown=typeof applied.watcher_enabled==='boolean';
  const intelligence=agentPending||activityPending?summary('Apply changes','pending')
    :agentName&&agentMissing?summary('Check agent','error')
      :agentChecked&&activityKnown?summary('Agent set · Activity '+(applied.watcher_enabled?'on':'off'),'good')
        :summary('Not checked');

  let next=null;
  if(rootsPending)next={panel:'workspace',label:'Apply changes',message:runtimeData.managed_container===true
    ?'Run the installer again to use your saved folders.'
    :'Restart the server to use your saved folders.'};
  else if(runtimeData.restart_required===true)next={panel:'server',label:'Apply changes',
    message:'Restart the server to use your saved settings.'};
  else if(foldersBlocked)next={panel:'workspace',label:'Check folders',
    message:'Check that xo-space can read your projects and write to its state folder.'};
  else if(agentName&&agentMissing)next={panel:'intelligence',label:'Check agent',
    message:'Install the agent or check access to its folder.'};
  else if(foldersChecked&&intelligence.tone==='good'&&projectStatus?.status==='ready'&&projectStatus.count===0)
    next={panel:'projects',label:'Add project',message:'Clone a Git repository into this Space.'};
  return{workspace,intelligence,projects,next};
}
