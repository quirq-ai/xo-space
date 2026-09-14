/* Factual Setup summaries from /api/runtime-config. These describe settings
   and filesystem checks, never verified authentication or watcher health. */
const summary=(label,tone='muted')=>({label,tone});
const inaccessible=path=>path?.exists===false||path?.readable===false;
const accessible=path=>path?.exists===true&&path?.readable===true;
const WATCHER_FIELDS=['watcher_enabled','watcher_source_mode','watcher_interval_seconds'];

export function setupSteps(runtimeData){
  if(!runtimeData)return{
    workspace:summary('Checking'),agent:summary('Checking'),activity:summary('Checking'),next:null
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
  const agent=summary(agentName||'Not checked',agentPending?'pending'
    :agentMissing?'muted':agentName&&agentName===applied.agent_name?'good':'muted');

  const activityPending=WATCHER_FIELDS.some(key=>configured[key]!==undefined&&applied[key]!==undefined
    &&configured[key]!==applied[key]);
  const activityKnown=typeof applied.watcher_enabled==='boolean';
  const activity=summary(activityKnown?(applied.watcher_enabled?'On':'Off')+(activityPending?' · pending':''):'Not checked',
    activityPending?'pending':applied.watcher_enabled===true?'good':'muted');

  let next=null;
  if(rootsPending)next={panel:'workspace',label:'Apply changes',message:runtimeData.managed_container===true
    ?'Run the installer again to use your saved folders.'
    :'Restart the server to use your saved folders.'};
  else if(runtimeData.restart_required===true)next={panel:'server',label:'Apply changes',
    message:'Restart the server to use your saved settings.'};
  else if(foldersBlocked)next={panel:'workspace',label:'Check folders',
    message:'Check that xo-space can read your projects and write to its state folder.'};
  else if(agentName&&agentMissing)next={panel:'agent',label:'Check agent',
    message:'Install the agent or check access to its folder.'};
  return{workspace,agent,activity,next};
}
