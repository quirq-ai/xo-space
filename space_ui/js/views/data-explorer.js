/* Shared read-only representations. Domain adapters supply actual records;
   existing List controllers remain the single owner of forms and mutations. */
import {pageHeader} from '../core/page-layout.js?v=20260914-unified1';
import {EXPLORER_PAGES} from '../core/navigation.js?v=20260914-unified1';
import {loadAgentData} from './agent-data.js?v=20260914-unified1';
import {loadInboxData} from './inbox-data.js?v=20260914-unified1';
import {loadSetupData} from './setup-data.js?v=20260914-unified1';

const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const LOADERS={agents:loadAgentData,inbox:loadInboxData,setup:loadSetupData};
const TITLES={agents:'Agents',inbox:'Inbox',setup:'Setup'};
const controllers=new Map();

export function visibleRecords(nodes,query=''){
  const byId=new Map(nodes.map(node=>[node.id,node]));
  const terms=query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const matches=new Set(nodes.filter(node=>{
    const text=[node.label,node.kind,node.detail,node.searchText,...(node.meta||[]).flat()].join(' ').toLowerCase();
    return terms.every(term=>text.includes(term));
  }).map(node=>node.id));
  const included=new Set(matches);
  for(const id of matches){
    let node=byId.get(id);const seen=new Set([id]);
    while(node?.parentId&&byId.has(node.parentId)&&!seen.has(node.parentId)){
      included.add(node.parentId);seen.add(node.parentId);node=byId.get(node.parentId);
    }
  }
  return{nodes:nodes.filter(node=>included.has(node.id)),matches};
}

export function createExplorerViews(){
  return EXPLORER_PAGES.map(page=>{
    let controller;
    return{...page,
      toolbar:()=>controller?.toolbar,
      mount(el,ctx){
        if(!controllers.has(page.parent))controllers.set(page.parent,mountExplorer(el,page.parent));
        controller=controllers.get(page.parent);controller.refreshers.add(ctx.refreshToolbar);
      },
      show(){controller.show(page.mode);},
    };
  });
}

function mountExplorer(root,section){
  let data=null,loading=false,query='',mode='graph',selected=null,zoom=1,revision=0,warning='',changes=0,dirty=false,toolsMode=null;
  let active=false,queuedRead=false;
  const collapsed=new Set(),graphExpanded=new Set(),refreshers=new Set();
  root.classList.add('space-explorer-view');
  root.innerHTML='<div class="space-page space-explorer">'+pageHeader({title:TITLES[section],
    description:'Explore the same data as a graph or a tree.',className:'space-explorer-header',
    actions:'<button type="button" class="space-button" data-explorer-action="refresh">Refresh</button>'})
    +'<div class="space-explorer-toolbar"><span class="space-explorer-count" role="status">Loading…</span>'
    +'<div class="space-explorer-tools"></div></div><div class="space-explorer-warning" role="status" hidden></div>'
    +'<div class="space-explorer-body"><div class="space-explorer-surface"></div>'
    +'<aside class="space-explorer-inspector" aria-label="Record details" aria-live="polite"></aside></div></div>';
  const surface=root.querySelector('.space-explorer-surface'),inspector=root.querySelector('.space-explorer-inspector');
  const toolbar={search:{placeholder:'Search '+TITLES[section].toLowerCase()+' data…',getValue:()=>query,
    setValue:value=>{query=String(value);render();}}};
  function refreshToolbar(){for(const refresh of refreshers)refresh?.();}
  async function load({queue=false}={}){
    if(loading){if(queue)queuedRead=true;return;}
    loading=true;const mine=++revision,readVersion=changes;
    root.querySelector('[data-explorer-action=refresh]').disabled=true;
    if(!data)surface.innerHTML='<div class="space-explorer-empty" role="status">Loading '+TITLES[section].toLowerCase()+' data…</div>';
    render();
    try{
      const next=await LOADERS[section]();if(mine!==revision)return;
      const firstSnapshot=!data;
      data={...next,nodes:Array.isArray(next.nodes)?next.nodes:[]};
      if(firstSnapshot)for(const node of data.nodes)if(!node.parentId)graphExpanded.add(node.id);
      warning=next.warning||'';dirty=readVersion!==changes;
      if(selected&&!data.nodes.some(node=>node.id===selected))selected=null;
    }catch(error){warning='Could not refresh this data. '+(data?'Showing the last loaded records.':'Try Refresh.');}
    finally{if(mine===revision){
      loading=false;
      const followup=queuedRead&&active;queuedRead=false;
      if(followup)load();
      else{root.querySelector('[data-explorer-action=refresh]').disabled=false;render();refreshToolbar();}
    }}
  }
  function select(id){
    selected=id;
    for(const el of surface.querySelectorAll('[data-record]'))el.classList.toggle('is-selected',el.dataset.record===id);
    renderInspector();
    if(matchMedia('(max-width:760px)').matches)inspector.scrollIntoView({block:'nearest',behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'});
  }
  function renderInspector(){
    const node=data?.nodes.find(item=>item.id===selected);
    if(!node){inspector.innerHTML='<h2>Record details</h2><p>Select a record to inspect it. Open it in List to use its existing controls.</p>';return;}
    const children=data.nodes.filter(item=>item.parentId===node.id).length;
    inspector.innerHTML='<span class="space-explorer-kind">'+esc(node.kind||'Record')+'</span><h2>'+esc(node.label)+'</h2>'
      +(node.detail?'<p>'+esc(node.detail)+'</p>':'')
      +(mode==='graph'&&children&&!query?'<button class="space-button" data-explorer-action="group">'+(graphExpanded.has(node.id)?'Collapse group':'Expand group')+' ('+children+')</button>':'')
      +'<dl>'+(node.meta||[]).filter(row=>row?.length===2).map(([label,value])=>'<div><dt>'+esc(label)+'</dt><dd>'+esc(value)+'</dd></div>').join('')+'</dl>'
      +(typeof node.route==='string'&&/^(projects|agents|inbox|setup)\//.test(node.route)?'<a class="space-button" href="#/'+esc(node.route)+'">Open in List ↗</a>':'');
  }
  function renderWarning(){
    const message=[warning,dirty?'Projects changed. Refresh to update these records.':''].filter(Boolean).join(' ');
    const warn=root.querySelector('.space-explorer-warning');warn.textContent=message;warn.hidden=!message;
  }
  function render(){
    renderWarning();
    const description=root.querySelector('.space-page-description');
    description.textContent=data?.description||'Explore the same data as a graph or a tree.';
    const count=root.querySelector('.space-explorer-count');
    if(!data){count.textContent=loading?'Loading…':'Data unavailable';if(!loading)surface.innerHTML='<div class="space-explorer-empty">No data loaded. Try Refresh or open List.</div>';renderInspector();return;}
    const result=visibleRecords(data.nodes,query),nodes=result.nodes;
    count.textContent=(loading?'Refreshing… · ':'')+(query?result.matches.size+' matching records · '+data.nodes.length+' loaded':data.nodes.length+' loaded records');
    const tools=root.querySelector('.space-explorer-tools');
    if(toolsMode!==mode){toolsMode=mode;tools.innerHTML=mode==='tree'
      ?'<button class="space-button is-compact" data-explorer-action="expand">Expand all</button><button class="space-button is-compact" data-explorer-action="collapse">Collapse all</button>'
      :'<button class="space-button is-compact" data-explorer-action="out" aria-label="Zoom out">−</button><button class="space-button is-compact" data-explorer-action="fit">Fit</button><button class="space-button is-compact" data-explorer-action="in" aria-label="Zoom in">+</button>';}
    for(const control of tools.querySelectorAll('[data-explorer-action]'))if(['expand','collapse'].includes(control.dataset.explorerAction))control.disabled=!!query;
    if(!nodes.length)surface.innerHTML='<div class="space-explorer-empty">No records match this search.</div>';
    else if(mode==='tree')renderTree(nodes);else renderGraph(nodes);
    renderInspector();
  }
  function hierarchy(nodes){
    const ids=new Set(nodes.map(node=>node.id)),children=new Map();
    for(const node of nodes){const parent=ids.has(node.parentId)&&node.parentId!==node.id?node.parentId:null;if(!children.has(parent))children.set(parent,[]);children.get(parent).push(node);}
    return children;
  }
  function renderTree(nodes){
    const children=hierarchy(nodes),seen=new Set();let html='';
    function row(node,depth){
      if(seen.has(node.id))return;seen.add(node.id);
      const kids=children.get(node.id)||[],open=!!query||!collapsed.has(node.id);
      html+='<div class="space-tree-row" style="--depth:'+Math.min(depth,12)+'">'
        +(kids.length?'<button type="button" class="space-tree-toggle" data-toggle-record="'+esc(node.id)+'" aria-label="'+(open?'Collapse ':'Expand ')+esc(node.label)+'" aria-expanded="'+open+'"'+(query?' disabled':'')+'>'+(open?'⌄':'›')+'</button>':'<span class="space-tree-leaf" aria-hidden="true">·</span>')
        +'<button type="button" class="space-tree-node'+(node.id===selected?' is-selected':'')+'" data-record="'+esc(node.id)+'"><span>'+esc(node.label)+'</span><small>'+esc(node.kind)+(kids.length?' · '+kids.length:'')+'</small></button></div>';
      if(open)for(const child of kids)row(child,depth+1);
    }
    for(const node of children.get(null)||[])row(node,0);
    // Malformed cyclic records remain inspectable rather than disappearing.
    if(!html)for(const node of nodes)row(node,0);
    surface.innerHTML='<div class="space-tree">'+html+'</div>';
  }
  function renderGraph(all){
    const byId=new Map(all.map(node=>[node.id,node]));
    const expanded=all.filter(node=>{
      if(query)return true;let parent=node.parentId;const seen=new Set([node.id]);
      while(parent&&byId.has(parent)){if(seen.has(parent)||!graphExpanded.has(parent))return false;seen.add(parent);parent=byId.get(parent).parentId;}return true;
    });
    const nodes=expanded.slice(0,160),children=hierarchy(nodes),positions=new Map(),seen=new Set();let leaf=0,maxDepth=0;
    function place(node,depth){
      if(seen.has(node.id))return;seen.add(node.id);depth=Math.min(depth,8);maxDepth=Math.max(maxDepth,depth);
      const kids=(children.get(node.id)||[]).filter(child=>!seen.has(child.id));
      for(const child of kids)place(child,depth+1);
      const ys=kids.map(child=>positions.get(child.id)?.y).filter(value=>Number.isFinite(value));
      const y=ys.length?ys.reduce((sum,n)=>sum+n,0)/ys.length:40+(leaf++)*78;
      positions.set(node.id,{x:24+depth*240,y});
    }
    for(const node of children.get(null)||[])place(node,0);
    for(const node of nodes)if(!seen.has(node.id))place(node,0);
    const width=(maxDepth+1)*240+30,height=Math.max(360,leaf*78+50);
    root.querySelector('.space-explorer-count').textContent+=(query?'':' · '+nodes.length+' on graph');
    let paths='',cards='';
    const edges=[...nodes.filter(n=>n.parentId).map(n=>({source:n.parentId,target:n.id})),...(data.edges||[])];
    const seenEdges=new Set();
    for(const edge of edges){const edgeKey=JSON.stringify([edge.source,edge.target]);if(seenEdges.has(edgeKey))continue;seenEdges.add(edgeKey);const a=positions.get(edge.source),b=positions.get(edge.target);if(!a||!b)continue;
      const x=a.x+196,y=a.y+24,m=(x+b.x)/2;
      paths+='<path class="space-graph-edge" d="M'+x+','+y+' C'+m+','+y+' '+m+','+(b.y+24)+' '+b.x+','+(b.y+24)+'"'+(edge.label?' aria-label="'+esc(edge.label)+'"':'')+'/>';
    }
    for(const node of nodes){const p=positions.get(node.id),label=String(node.label||'Record');
      cards+='<g class="space-graph-node'+(all.some(item=>item.parentId===node.id)?' is-group':'')+(node.id===selected?' is-selected':'')+'" transform="translate('+p.x+' '+p.y+')" data-record="'+esc(node.id)+'" tabindex="0" role="button" aria-label="'+esc(label)+', '+esc(node.kind||'record')+'">'
        +'<title>'+esc(label)+'</title><rect width="196" height="52" rx="8"/><text class="space-graph-label" x="12" y="21">'+esc(label.length>25?label.slice(0,24)+'…':label)+'</text><text class="space-graph-kind" x="12" y="39">'+esc(node.kind||'record')+'</text></g>';
    }
    surface.innerHTML=(expanded.length>160?'<p class="space-graph-limit">Showing 160 of '+expanded.length+' records. Search to narrow the graph, or use Tree for all records.</p>':'')
      +'<div class="space-graph"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '+width+' '+height+'" width="'+width*zoom+'" height="'+height*zoom+'" role="group" aria-label="'+TITLES[section]+' relationships">'+paths+cards+'</svg></div>';
  }
  root.addEventListener('click',event=>{
    const record=event.target.closest('[data-record]');if(record){select(record.dataset.record);return;}
    const toggle=event.target.closest('[data-toggle-record]');if(toggle){const id=toggle.dataset.toggleRecord;collapsed.has(id)?collapsed.delete(id):collapsed.add(id);render();root.querySelector('[data-toggle-record="'+CSS.escape(id)+'"]')?.focus({preventScroll:true});return;}
    const control=event.target.closest('[data-explorer-action]');
    const action=control?.dataset.explorerAction,restoreFocus=control===document.activeElement;
    if(action==='refresh'){load();return;}
    if(action==='group'&&selected){graphExpanded.has(selected)?graphExpanded.delete(selected):graphExpanded.add(selected);}
    if(action==='expand')collapsed.clear();
    if(action==='collapse')for(const node of data?.nodes||[])if(node.parentId)collapsed.add(node.id);
    if(action==='in')zoom=Math.min(2,zoom+.2);
    if(action==='out')zoom=Math.max(.05,zoom-.2);
    if(action==='fit'){
      const svg=surface.querySelector('svg'),graph=surface.querySelector('.space-graph');
      if(svg&&graph){
        const style=getComputedStyle(graph),bounds=svg.viewBox.baseVal;
        const width=surface.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)-2;
        const height=surface.clientHeight-parseFloat(style.paddingTop)-parseFloat(style.paddingBottom)-(surface.querySelector('.space-graph-limit')?.offsetHeight||0)-2;
        zoom=Math.max(.02,Math.min(1,width/bounds.width,height/bounds.height));
      }else zoom=1;
      surface.scrollTo(0,0);
    }
    if(action){render();if(restoreFocus)root.querySelector('[data-explorer-action="'+action+'"]')?.focus({preventScroll:true});}
  });
  root.addEventListener('dblclick',event=>{const id=event.target.closest('g[data-record]')?.dataset.record;if(id){graphExpanded.has(id)?graphExpanded.delete(id):graphExpanded.add(id);render();}});
  root.addEventListener('keydown',event=>{
    const node=event.target.closest('g[data-record]');if(node&&['Enter',' '].includes(event.key)){event.preventDefault();select(node.dataset.record);}
  });
  for(const type of ['space:projects-changed','space:project-access-changed'])addEventListener(type,()=>{changes++;dirty=true;renderWarning();});
  // Graph and Tree use one physical section. Leaving it can expose List
  // mutations; re-entry refreshes without resetting representation state.
  addEventListener('space:view',event=>{if(event.detail?.section!==section+'-explorer')active=false;});
  return{toolbar,refreshers,show(nextMode){
    const entering=!active;active=true;mode=nextMode;
    if(entering)load({queue:true});
    else if(!data&&!loading)load();
    render();refreshToolbar();
  }};
}
