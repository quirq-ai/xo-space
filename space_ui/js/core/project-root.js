/* The root picker reads graph metadata independently of the canvas engine.
   Its DOM has one lifetime; changing pages never installs another listener. */
import {esc} from './ui.js';

export function rootRecords(data){
  if(!data?.root||typeof data.root.id!=='string'||typeof data.root.label!=='string')throw new Error('Invalid graph root');
  const rows=[{...data.root,type:'root'},...(data.hubs||[]).map(row=>({...row,type:'hub'})),
    ...(data.groups||[]).map(row=>({...row,type:'group'})),...(data.leaves||[]).map(row=>({...row,type:'leaf'}))];
  const records=new Map();
  for(const row of rows)if(typeof row.id==='string'&&typeof row.label==='string')records.set(row.id,{...row,degree:0});
  const join=(a,b)=>{if(records.has(a))records.get(a).degree++;if(records.has(b))records.get(b).degree++;};
  for(const row of data.hubs||[])join(data.root.id,row.id);
  for(const row of data.groups||[])join(row.cat,row.id);
  for(const row of data.leaves||[])join(row.group,row.id);
  for(const row of data.ties||[])join(row.s,row.t);
  for(const node of records.values()){
    const category=node.cat||records.get(node.group)?.cat,color=data.categories?.[category]?.color;
    node.color=typeof color==='string'&&/^#[0-9a-f]{3,8}$/i.test(color)?color:'#a8d94f';
  }
  return{root:records.get(data.root.id),records:[...records.values()],hubLabel:data.meta?.hubLabel||'Group',groupLabel:data.meta?.collectionLabel||'Group'};
}

export function rootMatches(records,value){
  const query=value.trim().toLowerCase();if(!query)return[];
  return records.filter(node=>node.type!=='root').map(node=>{
    const label=node.label.toLowerCase(),index=label.indexOf(query);
    const rank=index===0?0:index>0?/\W/.test(label[index-1])?1:2
      :String(node.tag||'').toLowerCase().includes(query)?3:String(node.blurb||'').toLowerCase().includes(query)?4:-1;
    return{node,index,rank};
  }).filter(item=>item.rank>=0).sort((a,b)=>a.rank-b.rank||b.node.degree-a.node.degree||a.node.label.length-b.node.label.length).slice(0,8);
}

export function createProjectRootPicker({readDataset,onPick}){
  const host=document.getElementById('graph-root'),button=document.getElementById('root-btn');
  const dropdown=document.getElementById('rootdd'),input=document.getElementById('root-q');
  const results=document.getElementById('root-ac'),reset=document.getElementById('root-reset'),label=document.getElementById('root-name');
  if(!host||!button||!dropdown||!input||!results||!reset||!label)return null;
  const cache=new Map(),selections=new Map();
  let dataset=null,revision=0,items=[],index=-1,loading=false;
  function close(){
    revision++;loading=false;dropdown.classList.remove('is-open');results.classList.remove('is-open');
    button.setAttribute('aria-expanded','false');button.removeAttribute('aria-busy');
    results.innerHTML='';items=[];index=-1;
    if(document.activeElement===input)input.blur();
  }
  function paintLabel(){
    const data=cache.get(dataset),selected=selections.get(dataset);
    label.textContent=selected?.label||data?.root.label||'Choose root';
    reset.textContent=data?'Reset to '+data.root.label:'Reset root';reset.disabled=!data||loading;
  }
  function setData(key,data){
    const next=rootRecords(data);cache.set(key,next);
    const selected=selections.get(key);
    const retained=selected&&next.records.find(node=>node.id===selected.id);
    selections.set(key,retained||next.root);
    if(dataset===key)paintLabel();
  }
  function render(){
    const query=input.value.trim().toLowerCase();
    items=rootMatches(cache.get(dataset)?.records||[],input.value);index=items.length?Math.max(0,Math.min(index,items.length-1)):-1;
    results.innerHTML=items.map(({node,index:offset},at)=>{
      const name=offset>=0?esc(node.label.slice(0,offset))+'<em>'+esc(node.label.slice(offset,offset+query.length))+'</em>'+esc(node.label.slice(offset+query.length)):esc(node.label);
      const kind=node.type==='hub'?cache.get(dataset).hubLabel:node.type==='group'?cache.get(dataset).groupLabel:node.tag||'file';
      return'<button type="button" data-root-index="'+at+'"'+(at===index?' class="is-active"':'')+'><span class="tdot'+(node.shape==='diamond'?' dia':'')+'" style="background:'+node.color+'"></span><span>'+name+'</span><span class="meta">'+esc(kind)+'</span></button>';
    }).join('')|| (query?'<div class="empty">No match in this workspace</div>':'');
    results.classList.toggle('is-open',Boolean(query));
  }
  async function open(){
    if(!dataset)return;
    if(dropdown.classList.contains('is-open')){close();return;}
    const key=dataset,mine=++revision;
    dropdown.classList.add('is-open');button.setAttribute('aria-expanded','true');input.value='';index=-1;
    loading=!cache.has(key);input.disabled=loading;paintLabel();
    if(loading){
      button.setAttribute('aria-busy','true');results.innerHTML='<div class="empty" role="status">Loading roots…</div>';results.classList.add('is-open');
      try{
        const data=await readDataset(key);
        if(mine!==revision||dataset!==key)return;
        setData(key,data);
      }catch{
        if(mine!==revision||dataset!==key)return;
        results.innerHTML='<div class="empty" role="status">Could not load roots. Close and reopen to retry.</div>';
        return;
      }finally{if(mine===revision){loading=false;button.removeAttribute('aria-busy');paintLabel();}}
    }
    if(mine!==revision||dataset!==key)return;
    input.disabled=false;render();input.focus();
  }
  function choose(node){
    if(!node||loading||!dataset)return;
    const key=dataset;close();onPick({dataset:key,id:node.id});
  }
  button.addEventListener('click',event=>{event.stopPropagation();open();});
  dropdown.addEventListener('click',event=>event.stopPropagation());
  reset.addEventListener('click',()=>choose(cache.get(dataset)?.root));
  input.addEventListener('input',()=>{if(!loading){index=0;render();}});
  input.addEventListener('keydown',event=>{
    if(event.isComposing)return;
    if(event.key==='Escape'){event.preventDefault();close();button.focus();}
    else if(event.key==='Enter'){event.preventDefault();choose(items[Math.max(0,index)]?.node);}
    else if(['ArrowDown','ArrowUp'].includes(event.key)&&items.length){event.preventDefault();index=(index+(event.key==='ArrowDown'?1:items.length-1))%items.length;render();}
  });
  results.addEventListener('pointerdown',event=>{
    const row=event.target.closest('[data-root-index]');if(row){event.preventDefault();choose(items[Number(row.dataset.rootIndex)]?.node);}
  });
  results.addEventListener('click',event=>{
    if(event.detail!==0)return;
    const row=event.target.closest('[data-root-index]');if(row)choose(items[Number(row.dataset.rootIndex)]?.node);
  });
  addEventListener('click',event=>{if(!host.contains(event.target))close();});
  return{
    close,setData,
    setContext(key){close();dataset=key;host.hidden=!key;button.disabled=!key;input.disabled=false;paintLabel();},
    selectedRoot:key=>selections.get(key)?.id||null,
    setRoot(key,node){selections.set(key,{id:node.id,label:node.label});if(dataset===key)paintLabel();},
    invalidate(){close();cache.clear();paintLabel();},
  };
}
