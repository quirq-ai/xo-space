/* Pure Setup guidance checks: no DOM, network, credentials, or state writes. */
import assert from 'node:assert/strict';
import {setupSteps} from '../../space_ui/js/core/setup-state.js';
import {SETUP_STEPS,SETUP_MANAGE,resolveSetupSection} from '../../space_ui/js/core/setup-sections.js';

const folder={exists:true,readable:true,writable:true};
const settings={agent_name:'fixture_agent',watcher_enabled:true,
  watcher_source_mode:'all',watcher_interval_seconds:1};
const fixture=()=>({configured:{...settings},applied:{...settings},restart_required:false,
  roots:{change_required:false},paths:{projects:{...folder},state:{...folder}},
  agents:[{name:'fixture_agent',binary_available:true,home:{...folder},watched:true,
    session_files:0,secrets:[{key:'OPTIONAL_PROVIDER_KEY',configured:false}]}]});

// Missing diagnostics are uncertainty, not a fabricated setup failure/action.
for(const data of [null,undefined,{}, {configured:{},applied:{},paths:{},agents:[]}]){
  const state=setupSteps(data);
  assert.equal(state.next,null);
  assert.ok(Object.values(state).filter(Boolean).every(step=>step.tone==='muted'));
}
const base=fixture();
assert.equal(setupSteps(base).next,null,'Zero counted files and unset optional credentials do not block setup');
assert.deepEqual(Object.keys(setupSteps(base)),['workspace','intelligence','projects','next']);
assert.equal(setupSteps(base).intelligence.label,'Agent set · Activity on');
assert.equal(setupSteps(base).intelligence.tone,'good');
assert.equal(setupSteps(base).workspace.tone,'good');

// Source selection must not override the applied watcher switch.
const paused=fixture();paused.configured.watcher_enabled=paused.applied.watcher_enabled=false;
assert.equal(paused.agents[0].watched,true);
assert.equal(setupSteps(paused).intelligence.label,'Agent set · Activity off');
assert.equal(setupSteps(paused).intelligence.tone,'good','A configured, intentionally paused watcher is not an error');
assert.equal(setupSteps(paused).next,null,'Turning the watcher off is a supported choice');
paused.configured.watcher_enabled=true;
assert.equal(setupSteps(paused).intelligence.label,'Apply changes','Saved watcher settings are not described as already applied');
assert.equal(setupSteps(paused).intelligence.tone,'pending');
for(const [key,value] of [['watcher_source_mode','active'],['watcher_interval_seconds',2.5]]){
  const data=fixture();data.configured[key]=value;
  assert.equal(setupSteps(data).intelligence.tone,'pending','All watcher settings can await restart');
}

const readOnlyProjects=fixture();readOnlyProjects.paths.projects.writable=false;
assert.equal(setupSteps(readOnlyProjects).next,null,'Read-only projects remain useful for browsing');
assert.equal(setupSteps(readOnlyProjects).workspace.tone,'good');
const readOnlyState=fixture();readOnlyState.paths.state.writable=false;
assert.equal(setupSteps(readOnlyState).next.panel,'workspace','State writes need permission');
for(const path of ['projects','state'])for(const flag of ['exists','readable']){
  const data=fixture();data.paths[path][flag]=false;
  assert.equal(setupSteps(data).next.panel,'workspace');
}
const incomplete=fixture();delete incomplete.paths.state.writable;
assert.equal(setupSteps(incomplete).workspace.tone,'muted');
assert.equal(setupSteps(incomplete).next,null,'Missing a field is not the same as failed access');

const missingAgent=fixture();missingAgent.agents[0].binary_available=false;
assert.equal(setupSteps(missingAgent).next.panel,'intelligence');
assert.equal(setupSteps(missingAgent).intelligence.label,'Check agent');
assert.equal(setupSteps(missingAgent).intelligence.tone,'error');
const missingHome=fixture();missingHome.agents[0].home.exists=false;
assert.equal(setupSteps(missingHome).next.panel,'intelligence');
const unknownAgent=fixture();unknownAgent.agents=[];
assert.equal(setupSteps(unknownAgent).next,null,'Missing source diagnostics are not missing installation');
assert.equal(setupSteps(unknownAgent).intelligence.tone,'muted','Agent selection alone does not verify the executable or folder');
for(const field of ['binary_available','home']){
  const data=fixture();delete data.agents[0][field];
  assert.equal(setupSteps(data).intelligence.label,'Not checked');
  assert.equal(setupSteps(data).next,null);
}
const unknownWatcher=fixture();delete unknownWatcher.applied.watcher_enabled;
assert.equal(setupSteps(unknownWatcher).intelligence.tone,'muted','Do not infer the applied watcher switch from source selection');
const unknownApplied=fixture();delete unknownApplied.applied.agent_name;
assert.equal(setupSteps(unknownApplied).intelligence.tone,'muted','Configured selection is not proof it is running');
const switching=fixture();switching.configured.agent_name='next_agent';
assert.equal(setupSteps(switching).intelligence.label,'Apply changes');
assert.equal(setupSteps(switching).intelligence.tone,'pending');

// Prefer applying known pending changes to diagnosing the old mounted roots.
const priority=fixture();priority.roots.change_required=true;priority.restart_required=true;
priority.paths.projects.exists=false;priority.agents[0].binary_available=false;
assert.equal(setupSteps(priority).next.panel,'workspace');
assert.equal(setupSteps(priority).workspace.tone,'pending');
priority.managed_container=true;
assert.match(setupSteps(priority).next.message,/installer/,'Container roots require the installer to remap mounts');
priority.roots.change_required=false;
assert.equal(setupSteps(priority).next.panel,'server','Restart applies pending runtime/credential changes');
priority.restart_required=false;
assert.equal(setupSteps(priority).next.panel,'workspace','Folder access precedes agent inspection');
priority.paths.projects.exists=true;
assert.equal(setupSteps(priority).next.panel,'intelligence');

// Project catalog status has its own request lifecycle, independent of runtime.
for(const data of [null,undefined,{},fixture()]){
  assert.deepEqual(setupSteps(data,{status:'ready',count:1}).projects,{label:'1 project',tone:'good'});
  assert.deepEqual(setupSteps(data,{status:'ready',count:12}).projects,{label:'12 projects',tone:'good'});
  assert.deepEqual(setupSteps(data,{status:'ready',count:0}).projects,{label:'No projects',tone:'muted'});
  assert.deepEqual(setupSteps(data,{status:'error',count:8}).projects,{label:'Unavailable',tone:'error'});
  for(const status of ['idle','loading']){
    assert.deepEqual(setupSteps(data,{status,count:0}).projects,{label:'Checking',tone:'muted'});
    assert.equal(setupSteps(data,{status,count:0}).next,null,'A pending catalog request is not an empty project list');
  }
}
for(const count of [undefined,null,'0',-1,1.5,NaN,Infinity]){
  assert.deepEqual(setupSteps(base,{status:'ready',count}).projects,{label:'Not checked',tone:'muted'});
  assert.equal(setupSteps(base,{status:'ready',count}).next,null,'Malformed counts must not trigger an Add project recommendation');
}
assert.equal(setupSteps(base,{status:'ready',count:0}).next.panel,'projects');
assert.equal(setupSteps(base,{status:'ready',count:0}).next.label,'Add project');
assert.equal(setupSteps(null,{status:'ready',count:0}).next,null,'Do not skip unknown runtime setup to recommend a project');
assert.equal(setupSteps(missingAgent,{status:'ready',count:0}).next.panel,'intelligence','Agent repair precedes adding projects');
assert.equal(setupSteps(readOnlyState,{status:'ready',count:0}).next.panel,'workspace','Folder repair precedes adding projects');
assert.equal(setupSteps(unknownAgent,{status:'ready',count:0}).next,null,'Missing diagnostics are not completed setup');
const pending=fixture();pending.restart_required=true;
assert.equal(setupSteps(pending,{status:'ready',count:0}).next.panel,'server','Apply pending settings before adding projects');

// Definitions and legacy aliases share one canonical navigation vocabulary.
assert.deepEqual(SETUP_STEPS.map(({id,label,number})=>[id,label,number]),[
  ['workspace','Workspace',1],['intelligence','Intelligence layer',2],['projects','Projects',3],
]);
assert.deepEqual(SETUP_MANAGE.map(section=>section.id),['connectors','secrets','commands','server']);
for(const section of [...SETUP_STEPS,...SETUP_MANAGE]){
  assert.equal(resolveSetupSection(section.id),section.id);
  assert.ok(Object.isFrozen(section));
}
assert.equal(resolveSetupSection('agent'),'intelligence');
assert.equal(resolveSetupSection('activity'),'intelligence');
for(const unknown of [null,undefined,0,{},'','unknown','constructor','__proto__']){
  assert.equal(resolveSetupSection(unknown),null);
}

// The helper must neither mutate server snapshots nor need credential values.
const untouched=fixture(),before=structuredClone(untouched),catalog={status:'ready',count:2};
setupSteps(untouched,catalog);assert.deepEqual(untouched,before);assert.deepEqual(catalog,{status:'ready',count:2});
console.log('Setup summaries: canonical sections, legacy aliases, combined intelligence checks, independent project status, pending priority and unknown diagnostics passed.');
