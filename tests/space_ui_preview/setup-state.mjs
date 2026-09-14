/* Pure Setup guidance checks: no DOM, network, credentials, or state writes. */
import assert from 'node:assert/strict';
import {setupSteps} from '../../space_ui/js/core/setup-state.js';

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
assert.equal(setupSteps(base).agent.label,'fixture_agent');
assert.equal(setupSteps(base).activity.label,'On');
assert.equal(setupSteps(base).workspace.tone,'good');

// Source selection must not override the applied watcher switch.
const paused=fixture();paused.configured.watcher_enabled=paused.applied.watcher_enabled=false;
assert.equal(paused.agents[0].watched,true);
assert.equal(setupSteps(paused).activity.label,'Off');
assert.equal(setupSteps(paused).next,null,'Turning the watcher off is a supported choice');
paused.configured.watcher_enabled=true;
assert.match(setupSteps(paused).activity.label,/^Off.*pending/,'Describe the running process until restart');
for(const [key,value] of [['watcher_source_mode','active'],['watcher_interval_seconds',2.5]]){
  const data=fixture();data.configured[key]=value;
  assert.equal(setupSteps(data).activity.tone,'pending','All watcher settings can await restart');
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
assert.equal(setupSteps(missingAgent).next.panel,'agent');
const missingHome=fixture();missingHome.agents[0].home.exists=false;
assert.equal(setupSteps(missingHome).next.panel,'agent');
const unknownAgent=fixture();unknownAgent.agents=[];
assert.equal(setupSteps(unknownAgent).next,null,'Missing source diagnostics are not missing installation');
const switching=fixture();switching.configured.agent_name='next_agent';
assert.equal(setupSteps(switching).agent.label,'next_agent');
assert.equal(setupSteps(switching).agent.tone,'pending');

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
assert.equal(setupSteps(priority).next.panel,'agent');

// The helper must neither mutate server snapshots nor need credential values.
const untouched=fixture(),before=structuredClone(untouched);
setupSteps(untouched);assert.deepEqual(untouched,before);
console.log('Setup summaries: pending priority, applied watcher state, folder permissions, unknown diagnostics and optional credentials passed.');
