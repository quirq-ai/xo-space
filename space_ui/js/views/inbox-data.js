/* Read-only relationships for Inbox's Graph and Tree representations. The
   existing scope pages keep all item, polling and command-result actions. */
import {API_BASE} from '../core/api.js';
import {readSnapshot} from '../core/read-snapshot.js?v=20260914-unified1';
import {collectorLabels} from '../core/connections.js';

const text=value=>typeof value==='string'?value:'';
const rows=value=>Array.isArray(value)?value.filter(row=>row&&typeof row==='object'&&!Array.isArray(row)):[];
const id=(...parts)=>'inbox:'+JSON.stringify(parts);
const count=value=>Number.isFinite(Number(value))?Math.max(0,Number(value)):0;
const interval=value=>{
  const seconds=Number(value);
  if(!Number.isFinite(seconds)||seconds<=0)return'Interval unavailable';
  for(const [unit,size] of [['d',86400],['h',3600],['min',60]])if(seconds%size===0)return'Every '+seconds/size+' '+unit;
  return'Every '+seconds+' s';
};

/* Only explicitly selected summary fields leave this boundary: command argv,
   output tails, connection credentials and provider errors are never copied. */
export function buildInboxData({inbox=null,connections=null,schedules=null,unavailable=[]}={}){
  const nodes=[],edges=[],known=new Set();
  function add(node){
    if(known.has(node.id))return node.id;
    known.add(node.id);nodes.push(node);
    if(node.parentId)edges.push({source:node.parentId,target:node.id});
    return node.id;
  }
  const root=add({id:id('root'),label:'Inbox',kind:'inbox',detail:'Items, polled connections and scheduled commands.'});
  const itemRows=rows(inbox?.items).filter(item=>text(item.id));
  const connectionRows=rows(connections?.connections).filter(connection=>text(connection.toolkit)&&(connection.configured||connection.connected_here));
  const jobRows=rows(schedules?.jobs).filter(job=>text(job.id)&&job.every_seconds!=null);
  const total=inbox?.counts?['new','seen','done'].reduce((sum,key)=>sum+count(inbox.counts[key]),0):itemRows.length;
  const scopes=[['items','Items',inbox,itemRows.length],['connections','Connections',connections,connectionRows.length],['jobs','Jobs',schedules,jobRows.length]];
  for(const [key,label,payload,n] of scopes){
    add({id:id(key),label,kind:'scope',parentId:root,route:'inbox/'+key,
      detail:payload===null?'Could not load '+label.toLowerCase()+'.':key==='items'
        ?itemRows.length+' loaded'+(total>itemRows.length?' of '+total:'')+' inbox items.'
        :n+' '+(key==='jobs'?'scheduled commands':'polled connections')+'.',
      meta:[['State',payload===null?'Unavailable':'Loaded'],['Loaded',String(n)]]});
  }
  for(const item of itemRows){
    const source=text(item.source)||'Unknown source';
    const sourceId=add({id:id('source',source),label:source,kind:'source',parentId:id('items'),route:'inbox/items'});
    const project=text(item.project_id);
    const parentId=project?add({id:id('project',source,project),label:project,kind:'project',parentId:sourceId,route:'inbox/items'}):sourceId;
    add({id:id('item',item.id),label:text(item.title)||'Untitled item',kind:'item',parentId,route:'inbox/items',
      detail:'Open Items to read, mark or remove this item.',
      meta:[['Item ID',item.id],['Status',text(item.status)||'Unknown'],['Source',source],['Kind',text(item.kind)||'Unknown'],
        ...(project?[['Project',project]]:[]),...(text(item.ts)?[['Received',item.ts]]:[])],
      searchText:[item.id,text(item.title),source,project,text(item.kind),text(item.status)].join(' ')});
  }
  for(const connection of connectionRows){
    const toolkit=connection.toolkit;
    const label=text(connection.display_name)||toolkit;
    const parentId=add({id:id('connection',toolkit),label,kind:'connection',parentId:id('connections'),route:'inbox/connections',
      detail:'Open Connections to poll or configure this app.',
      meta:[['Toolkit',toolkit],['Polling',connection.enabled?'Enabled':'Off'],['Interval',interval(connection.interval_s)],
        ['Collectors',collectorLabels(connection)],['Last poll',text(connection.last_poll_at)||'Never'],
        ['Last poll state',connection.last_error?'Failed':connection.last_poll_at?'Completed':'Not run'],
        ...(text(connection.account_label)?[['Account',connection.account_label]]:[])],
      searchText:[toolkit,label,text(connection.account_label)].join(' ')});
    const labels=new Map(rows(connection.available_collectors).map(collector=>[text(collector.id),text(collector.label)]));
    for(const collector of Array.isArray(connection.collectors)?connection.collectors:[]){
      if(!text(collector))continue;
      add({id:id('collector',toolkit,collector),label:labels.get(collector)||collector,kind:'collector',parentId,route:'inbox/connections'});
    }
  }
  for(const job of jobRows){
    const cadence=interval(job.every_seconds);
    const parentId=add({id:id('interval',cadence),label:cadence,kind:'interval',parentId:id('jobs'),route:'inbox/jobs'});
    const result=job.last_result&&typeof job.last_result==='object'?job.last_result:null;
    add({id:id('job',job.id),label:text(job.name)||job.id,kind:'job',parentId,route:'inbox/jobs',
      detail:'Open Jobs to view this command’s results.',
      meta:[['Command ID',job.id],['Interval',cadence],['Schedule',job.enabled===false?'Disabled':'Enabled'],
        ['Run state',job.running?'Running':result?text(result.status)||'Unknown result':'Not run yet'],
        ...(text(job.next_run)?[['Next due',job.next_run]]:[]),
        ...(text(result?.finished_at)?[['Last finished',result.finished_at]]:[]),
        ...(result?.duration_seconds!=null&&Number.isFinite(Number(result.duration_seconds))?[['Last duration',Number(result.duration_seconds)+' s']]:[])],
      searchText:[job.id,text(job.name),cadence,job.running?'running':'',text(result?.status)].join(' ')});
  }
  const warnings=[];
  if(unavailable.length)warnings.push('Could not load '+unavailable.join(', ')+'. Available data is shown.');
  if(total>itemRows.length&&inbox!==null)warnings.push('Showing the newest '+itemRows.length+' of '+total+' inbox items.');
  return{title:'Inbox',description:'Items by source and project, connections by collector, and jobs by interval.',nodes,edges,warning:warnings.join(' ')};
}

/* Each scope can fail independently. A stalled read is bounded too, so the
   successful scopes still become available without waiting on that service. */
async function readScope(path,key,timeoutMs){
  const response=await readSnapshot(API_BASE+path,{timeoutMs});
  return response?.ok&&Array.isArray(response.data?.[key])?response.data:null;
}
export async function loadInboxData({timeoutMs=12000}={}){
  const [inbox,connections,schedules]=await Promise.all([
    readScope('/api/inbox?status=all&limit=200','items',timeoutMs),
    readScope('/api/connections','connections',timeoutMs),
    readScope('/api/schedules','jobs',timeoutMs),
  ]);
  const unavailable=[];
  if(inbox===null)unavailable.push('items');
  if(connections===null)unavailable.push('connections');
  if(schedules===null)unavailable.push('jobs');
  return buildInboxData({inbox,connections,schedules,unavailable});
}
