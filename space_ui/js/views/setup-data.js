/* A read-only map of existing Setup records. Explicit allowlists below keep
   environment values, auth tokens, command arguments and output out of it. */
import {API_BASE} from '../core/api.js';
import {readSnapshot} from '../core/read-snapshot.js?v=20260914-unified1';
import {SETUP_SECTIONS} from '../core/setup-sections.js?v=20260914-setuproutes1';

const text=value=>typeof value==='string'?value.trim():'';
const number=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;
const bool=value=>value===true?'Enabled':value===false?'Disabled':'Unknown';
const key=(...parts)=>JSON.stringify(parts);
const object=value=>value&&typeof value==='object'&&!Array.isArray(value);
const facts=rows=>rows.filter(([,value])=>value!==null&&value!==undefined&&value!=='').map(([label,value])=>[label,String(value)]);
const NATIVE=[['github','GitHub'],['magicpath','MagicPath'],['vercel','Vercel'],['gdrive','Google Drive'],['onedrive','OneDrive']];
const ENDPOINTS={runtime:'/api/runtime-config',identity:'/space/setup/status',projects:'/api/xo-projects',
  connections:'/api/connections',secrets:'/api/secrets',jobs:'/api/schedules',server:'/space/server/status',
  ...Object.fromEntries(NATIVE.map(([id])=>[id,'/api/connectors/'+id+(id==='gdrive'||id==='onedrive'?'/remotes':'/status')]))};

export function buildSetupData(results={}){
  const nodes=[{id:'setup',label:'Setup',kind:'root',route:'setup/workspace'}],edges=[],warnings=[];
  const byId=new Map(nodes.map(node=>[node.id,node]));
  const add=node=>{
    if(byId.has(node.id))return;
    nodes.push(node);byId.set(node.id,node);
    if(node.parentId)edges.push({source:node.parentId,target:node.id,label:'contains'});
  };
  const section=id=>'setup/'+id;
  for(const item of SETUP_SECTIONS)add({id:section(item.id),parentId:'setup',label:item.label,kind:'section',route:item.route,
    detail:item.description||'',meta:item.number?[['Setup step',String(item.number)]]:[]});
  function read(name,label,valid){
    const response=results[name];
    if(response?.ok&&object(response.data)&&valid(response.data))return response.data;
    warnings.push(label+' unavailable.');return null;
  }
  function unavailable(id,label){
    const parent=byId.get(section(id));
    parent.detail=[parent.detail,label+' unavailable.'].filter(Boolean).join(' ');
  }
  function records(rows,field,label){
    const seen=new Set(),valid=[];
    for(const row of rows){
      if(!text(row?.[field])){warnings.push('Some '+label+' records could not be read.');continue;}
      if(seen.has(row[field]))continue;
      seen.add(row[field]);valid.push(row);
    }
    return valid;
  }
  const identity=read('identity','Workspace identity',data=>object(data.space)&&object(data.xo)&&object(data.github));
  if(identity){
    const s=identity.space;
    add({id:'setup/workspace/identity',parentId:section('workspace'),label:text(s.label)||'Workspace identity',kind:'workspace',route:section('workspace'),
      detail:s.status==='configured'?'Configured':s.status==='not_configured'?'Not configured':'Unavailable',
      meta:facts([['Space ID',s.status==='configured'?text(s.id):null],['Owner',text(s.owner)],['Checked',text(identity.checked_at)]])});
    for(const [id,label,field] of [['xo','XO account','user_id'],['github','GitHub identity','username']]){
      const account=identity[id],connected=account.status==='connected'&&Boolean(text(account[field]));
      add({id:key('account',id),parentId:section('workspace'),label,kind:'account',route:section('workspace'),
        detail:connected?'Connected':account.status==='not_configured'?'Not configured':account.status==='rejected'?'Authentication rejected':'Unable to verify',
        meta:connected?[[id==='xo'?'User ID':'Username',text(account[field])]]:[]});
    }
  }else unavailable('workspace','Identity');

  const runtime=read('runtime','Runtime settings',data=>object(data.configured)&&object(data.applied));
  if(runtime){
    const configured=runtime.configured,applied=runtime.applied;
    add({id:'setup/intelligence/runtime',parentId:section('intelligence'),label:'Chat agent',kind:'runtime',route:section('intelligence'),
      detail:text(applied.agent_name)||'No applied agent reported',meta:facts([['Saved agent',text(configured.agent_name)],
        ['Applied agent',text(applied.agent_name)],['Restart required',runtime.restart_required===true?'Yes':runtime.restart_required===false?'No':'Unknown']])});
    add({id:'setup/intelligence/activity',parentId:section('intelligence'),label:'Activity collection',kind:'setting',route:section('intelligence'),
      detail:bool(applied.watcher_enabled),meta:facts([['Applied collection',bool(applied.watcher_enabled)],['Saved collection',bool(configured.watcher_enabled)],
        ['Applied source mode',text(applied.watcher_source_mode)],['Applied interval (seconds)',number(applied.watcher_interval_seconds)]])});
    for(const [id,label] of [['xo_projects_root','Projects folder'],['quirq_state_root','Space data folder']]){
      const path=text(runtime.roots?.applied?.[id]);
      if(path)add({id:key('folder',id),parentId:section('workspace'),label,kind:'folder',detail:path,route:section('workspace'),meta:[['Applied path',path]]});
    }
  }else unavailable('intelligence','Runtime settings');

  const projects=read('projects','Local projects',data=>Array.isArray(data.items));
  if(projects){
    const items=records(projects.items,'id','project');
    byId.get(section('projects')).detail=items.length+' loaded local projects';
    for(const project of items){
      add({id:key('project',project.id),parentId:section('projects'),label:text(project.display_name)||project.id,kind:'project',route:section('projects'),
        detail:text(project.description),meta:facts([['Project ID',project.id],['Path',text(project.path)]])});
    }
  }else unavailable('projects','Project list');

  const connections=read('connections','Polled connections',data=>Array.isArray(data.connections));
  if(connections){
    for(const connection of connections.connections){
      if(!text(connection?.toolkit)||!(connection.configured||connection.connected_here))continue;
      add({id:key('connection',connection.toolkit),parentId:section('connectors'),label:text(connection.display_name)||connection.toolkit,
        kind:'connection',route:section('connectors'),detail:connection.connected_here===true?'Connected to this workspace':'Configured',
        meta:facts([['Polling',bool(connection.enabled)],['Interval (seconds)',number(connection.interval_s)],['Last poll',text(connection.last_poll_at)]])});
    }
  }else unavailable('connectors','Polled connections');
  for(const [id,label] of NATIVE){
    const drive=id==='gdrive'||id==='onedrive';
    const data=read(id,label,data=>drive?Array.isArray(data.remotes):id==='magicpath'?typeof data.logged_in==='boolean':['connected','needs_auth','failed'].includes(data.status));
    let detail='Unavailable',meta=[];
    if(data&&drive){detail=data.remotes.some(remote=>remote?.complete===true)?'Configured':data.remotes.length?'Needs attention':'Not configured';}
    else if(data&&id==='magicpath'){
      detail=data.logged_in?'Connected':data.cli_installed?'Sign-in not verified':'Install required';
      meta=[['CLI installed',data.cli_installed===true?'Yes':'No'],['Skill installed',data.skill_installed===true?'Yes':'No']];
    }else if(data){
      detail=data.status==='connected'?'Connected':data.status==='needs_auth'?'Not connected':'Unavailable';
      meta=facts([['Account',data.status==='connected'?(text(data.username)||text(data.email)||text(data.name)):null]]);
    }
    const nodeId=key('native',id);
    add({id:nodeId,parentId:section('connectors'),label,kind:'connector',detail,meta,route:section('connectors')});
    if(data&&drive)for(const remote of data.remotes){
      if(!text(remote?.name))continue;
      add({id:key('remote',id,remote.name),parentId:nodeId,label:remote.name,kind:'account',route:section('connectors'),
        detail:remote.complete===true?'Configured':'Incomplete'});
    }
  }

  const secrets=read('secrets','Secret names',data=>Array.isArray(data.items));
  if(secrets){
    const items=secrets.items.filter(item=>typeof item?.key==='string'&&/^[A-Z_][A-Z0-9_]*$/.test(item.key));
    byId.get(section('secrets')).detail=items.length+' saved keys. Values are never included.';
    for(const item of items)add({id:key('secret',item.key),parentId:section('secrets'),label:item.key,kind:'secret',route:section('secrets'),
      detail:item.is_set===true?'Configured':item.is_set===false?'Empty':'Status unknown'});
  }else unavailable('secrets','Secret names');

  const jobs=read('jobs','Saved commands',data=>Array.isArray(data.jobs));
  if(jobs){
    const items=records(jobs.jobs,'id','command');
    byId.get(section('commands')).detail=items.length+' loaded saved commands';
    for(const job of items){
      const interval=number(job.every_seconds);
      add({id:key('command',job.id),parentId:section('commands'),label:text(job.name)||job.id,kind:'command',route:section('commands'),
        detail:job.every_seconds===null?'Manual command':interval!==null&&interval>0?'Every '+interval+' seconds':'Interval unknown',
        meta:facts([['Command ID',job.id],['Enabled',bool(job.enabled)],['Running',job.running===true?'Yes':job.running===false?'No':'Unknown'],
          ['Next due',text(job.next_run)]])});
    }
  }else unavailable('commands','Command list');
  const server=read('server','Server status',data=>typeof data.running==='boolean');
  if(server)add({id:'setup/server/process',parentId:section('server'),label:'Space server',kind:'server',route:section('server'),detail:server.running?'Running':'Stopped'});
  else unavailable('server','Server status');
  return {title:'Setup',description:'Current workspace settings and saved records, grouped by their management section. Secret values and command contents are excluded.',nodes,edges,warning:[...new Set(warnings)].join(' ')};
}

export async function loadSetupData({timeoutMs=12000}={}){
  const entries=await Promise.all(Object.entries(ENDPOINTS).map(async([name,path])=>[name,await readSnapshot(API_BASE+path,{timeoutMs})]));
  return buildSetupData(Object.fromEntries(entries));
}
