/* The block renderer for page specs (services/schema/page.schema.json).

   One renderer per block type, over the kit (shadcn.js, chart.js):
     stats list table cards detail form toggles timeline calendar chart
     stream text widget
   createRenderer(host, page, ctx) builds one section per block once and
   returns {render(payload), search(query), show(), hide(), destroy()}:
   render paints every block from the payload (a block with its own `read`
   fetches that route first), `when` hides a block or an action whose
   expression is falsy, `empty` goes through the kit's empty state, expand
   renders a nested block under a row when its toggle is pressed (with its
   own read, {field} filled from the row) and keeps it open across
   re-renders, search hides rows whose text does not contain the query.

   Safety: every value that reaches innerHTML passes through esc() exactly
   once, here. A spec carries text and expressions, never markup: a title
   is escaped, a link is only ever an http(s) URL, and text blocks go
   through markdown.js, which escapes before it transforms. */
import {esc,toast} from './ui.js';
import * as kit from './shadcn.js';
import {areaChart,barChartHorizontal,barChartStacked,donutChart,sparkline} from './chart.js';
import {evaluate,truthy,fill,pathObject,get} from './expr.js';
import {mdToHtml} from './markdown.js';
import {actionVisible,runAction,parseCall,safeLink} from './actions.js';
import {mountStream} from './stream.js';
import {mountWidget} from './widgets.js';
import {API_BASE,apiFetch,failText} from './api.js';

export const BLOCK_TYPES=Object.freeze(['stats','list','table','cards','detail','form','toggles',
  'timeline','calendar','chart','stream','text','widget']);
const HTTP=/^https?:\/\//i;
const PERSISTENT=new Set(['stream','widget']);

/* ── values ─────────────────────────────────────────────────────────────── */

/* One display string for any value: nothing for null, yes/no for booleans,
   a list joined with commas, an object as JSON. Not escaped: esc() is
   applied where the string lands in markup. */
export function text(value){
  if(value===null||value===undefined)return'';
  if(typeof value==='boolean')return value?'yes':'no';
  if(Array.isArray(value))return value.map(text).filter(Boolean).join(', ');
  if(typeof value==='object')return JSON.stringify(value);
  return String(value);
}
const mapKey=value=>value===null||value===undefined?'null':String(value);
/* A {value, map} pair: the map applies whether or not the expression says
   "|map" (the specs write both), "*" is the default, "null" the missing. */
export function mapped(expr,map,payload,row){
  const value=evaluate(expr,payload,row,map);
  if(!map||typeof map!=='object'||/\|\s*map\b/.test(String(expr)))return value;
  const key=mapKey(value);
  if(Object.prototype.hasOwnProperty.call(map,key))return map[key];
  if(Object.prototype.hasOwnProperty.call(map,'*'))return map['*'];
  return value;
}
const asRows=value=>Array.isArray(value)?value.filter(r=>r!==null&&r!==undefined)
  :value&&typeof value==='object'?Object.entries(value).map(([key,entry])=>(
    entry&&typeof entry==='object'&&!Array.isArray(entry)?{key,...entry}:{key,value:entry})):[];
const badgeVariant=tone=>({error:'destructive',danger:'destructive',warn:'outline',warning:'outline',
  primary:'default',accent:'default'})[tone]||'secondary';
const buttonVariant=tone=>({danger:'destructive',primary:'default'})[tone]||'outline';
const safeUrl=value=>{const s=text(value);return HTTP.test(s)?s:null;};

/* ── row pieces ─────────────────────────────────────────────────────────── */

function metaHtml(list,payload,row){
  if(!Array.isArray(list))return'';
  const parts=list.map(expr=>text(evaluate(expr,payload,row))).filter(Boolean);
  return parts.length?'<div class="spec-meta">'+parts.map(p=>'<span>'+esc(p)+'</span>').join('')+'</div>':'';
}
function badgeHtml(badge,payload,row){
  if(!badge||!badge.value)return'';
  const value=text(mapped(badge.value,badge.map,payload,row));
  if(!value)return'';
  const tone=badge.tone||'';
  return kit.badge(esc(value),{variant:badgeVariant(tone)});
}
function toneOf(spec,payload,row){
  if(!spec||!spec.value)return'';
  return text(mapped(spec.value,spec.map,payload,row));
}
function titleHtml(rowSpec,payload,row,fallback){
  const raw=rowSpec&&rowSpec.title!==undefined?evaluate(rowSpec.title,payload,row):undefined;
  const title=text(raw===undefined||raw===null?fallback:raw)||fallback||'';
  const url=rowSpec&&rowSpec.link?safeUrl(evaluate(rowSpec.link,payload,row)):null;
  const inner=esc(title);
  return url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+inner+'</a>':inner;
}
function rowSearchText(parts){return parts.map(text).join(' ').toLowerCase();}

/* ── the renderer ───────────────────────────────────────────────────────── */

export function createRenderer(host,page,ctx){
  const blocks=Array.isArray(page.spec?.blocks)?page.spec.blocks:[];
  const expanded=new Set();          /* "<blockKey>:<rowKey>" */
  const persistent=new Map();        /* blockIndex -> stream or widget controller */
  const calendars=new Map();         /* blockKey -> {month, selected} */
  const scopes=new Map();            /* scope id -> {block, payload, row, rowsByKey, key} */
  let scopeSeq=0,generation=0,payload={},query='',shown=false,dead=false;
  const actionCtx={refresh:()=>ctx.refresh?.(),switchTo:route=>ctx.switchTo?.(route)};

  host.innerHTML=blocks.map((block,i)=>'<section class="spec-block" data-block="'+i+'" data-type="'+esc(block.type)+'">'
    +(block.title?'<h2 class="spec-block-title">'+esc(block.title)+'</h2>':'')
    +'<div class="spec-block-body"></div></section>').join('');
  host.addEventListener('click',onClick);
  host.addEventListener('submit',onSubmit);

  function scope(block,scopePayload,row,rows,key){
    const id='s'+(++scopeSeq);
    const rowsByKey=new Map();
    if(rows)rows.forEach((r,i)=>rowsByKey.set(rowKey(block,r,i),r));
    scopes.set(id,{block,payload:scopePayload,row,rowsByKey,key});
    return id;
  }
  function rowKey(block,row,i){
    const value=block.key?get(row,block.key):undefined;
    return value===undefined||value===null||value===''?String(i):text(value);
  }
  function emptyHtml(block,fallback){
    return kit.empty({title:esc(block.empty||fallback||'Nothing here yet.'),size:'sm'});
  }
  function failHtml(res,what){
    return kit.alert({variant:'destructive',icon:kit.icons.alert,title:esc(what||'Could not load this block'),description:esc(failText(res))});
  }

  /* ── actions and expansion controls ── */
  function actionsHtml(block,scopeId,rowK,scopePayload,row){
    const actions=Array.isArray(block.actions)?block.actions:[];
    const buttons=actions.map((action,i)=>actionVisible(action,scopePayload,row)
      ?kit.button(esc(action.label||'Go'),{variant:buttonVariant(action.tone),size:'sm',
        attrs:'data-act="'+i+'" data-scope="'+scopeId+'"'+(rowK!==undefined?' data-row-key="'+esc(rowK)+'"':'')})
      :'').join('');
    const expand=block.expand&&rowK!==undefined
      ?kit.button(kit.icons.chevronDown+'<span>Details</span>',{variant:'ghost',size:'sm',
        attrs:'data-expand data-scope="'+scopeId+'" data-row-key="'+esc(rowK)+'" aria-expanded="'
          +(expanded.has(scopes.get(scopeId).key+':'+rowK)?'true':'false')+'"'})
      :'';
    return buttons+expand;
  }
  const expandHost=(block,rowK)=>block.expand?'<div class="spec-expand" data-expand-host data-row-key="'+esc(rowK)+'" hidden></div>':'';

  /* ── block renderers: (block, scope payload, row, key) -> html ── */
  function renderStats(block,p,row,key){
    const items=Array.isArray(block.items)?block.items:[];
    return'<div class="spec-stats">'+items.map(item=>{
      const value=text(mapped(item.value,item.map,p,row));
      const open=item.open?fill(item.open,p,row):'';
      return kit.card({stat:true,title:esc(value||'0'),description:esc(item.label||''),
        attrs:open?'data-open="'+esc(open)+'" role="link" tabindex="0"':''});
    }).join('')+'</div>';
  }

  function renderList(block,p,row,key){
    let rows=asRows(evaluate(block.items,p,row));
    if(block.limit)rows=rows.slice(0,block.limit);
    if(!rows.length)return emptyHtml(block);
    const scopeId=scope(block,p,row,rows,key);
    const spec=block.row||{};
    return kit.itemGroup(rows.map((r,i)=>{
      const k=rowKey(block,r,i);
      const detail=text(evaluate(spec.detail,p,r));
      const meta=Array.isArray(spec.meta)?spec.meta.map(e=>evaluate(e,p,r)):[];
      const tone=toneOf(spec.tone,p,r);
      const open=spec.open?fill(spec.open,p,r):'';
      const titleText=text(spec.title!==undefined?evaluate(spec.title,p,r):k);
      return kit.item({
        title:'<span class="spec-row-title">'+titleHtml(spec,p,r,k)+'</span>'+badgeHtml(spec.badge,p,r),
        description:detail?esc(detail):'',
        footer:metaHtml(spec.meta,p,r),
        actions:actionsHtml(block,scopeId,k,p,r),
        variant:'outline',
        attrs:'data-row data-row-key="'+esc(k)+'" data-search="'+esc(rowSearchText([titleText,detail,...meta,text(mapped(spec.badge?.value,spec.badge?.map,p,r))]))+'"'
          +(tone?' data-tone="'+esc(tone)+'"':'')+(open?' data-open="'+esc(open)+'"':''),
      })+expandHost(block,k);
    }).join(''))+'<p class="spec-nomatch" hidden>No rows match.</p>';
  }

  function renderCards(block,p,row,key){
    let rows=asRows(evaluate(block.items,p,row));
    if(block.limit)rows=rows.slice(0,block.limit);
    if(!rows.length)return emptyHtml(block);
    const scopeId=scope(block,p,row,rows,key);
    const spec=block.row||{};
    return'<div class="spec-cards">'+rows.map((r,i)=>{
      const k=rowKey(block,r,i);
      const detail=text(evaluate(spec.detail,p,r));
      const meta=Array.isArray(spec.meta)?spec.meta.map(e=>evaluate(e,p,r)):[];
      const tone=toneOf(spec.tone,p,r);
      const open=spec.open?fill(spec.open,p,r):'';
      const titleText=text(spec.title!==undefined?evaluate(spec.title,p,r):k);
      const actions=actionsHtml(block,scopeId,k,p,r);
      return kit.card({
        title:'<span class="spec-row-title">'+titleHtml(spec,p,r,k)+'</span>',
        description:detail?esc(detail):'',
        action:badgeHtml(spec.badge,p,r),
        content:metaHtml(spec.meta,p,r)+expandHost(block,k),
        footer:actions||'',
        tone:tone||null,
        attrs:'data-row data-row-key="'+esc(k)+'" data-search="'+esc(rowSearchText([titleText,detail,...meta]))+'"'
          +(open?' data-open="'+esc(open)+'"':''),
      });
    }).join('')+'</div><p class="spec-nomatch" hidden>No rows match.</p>';
  }

  function renderTable(block,p,row,key){
    let rows=asRows(evaluate(block.items,p,row));
    if(block.limit)rows=rows.slice(0,block.limit);
    const columns=Array.isArray(block.columns)?block.columns:[];
    if(!rows.length)return emptyHtml(block);
    const scopeId=scope(block,p,row,rows,key);
    const hasActions=(Array.isArray(block.actions)&&block.actions.length)||block.expand;
    const head=columns.map(c=>({html:esc(c.label||'')})).concat(hasActions?[{html:'',cls:'spec-actions-col'}]:[]);
    const out=[];
    rows.forEach((r,i)=>{
      const k=rowKey(block,r,i);
      const values=columns.map(c=>text(mapped(c.value,c.map,p,r)));
      const cells=columns.map((c,ci)=>{
        const url=c.link?safeUrl(evaluate(c.link,p,r)):null;
        const inner=url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(values[ci])+'</a>':esc(values[ci]);
        return{html:inner,cls:c.mono?'spec-mono':''};
      });
      if(hasActions)cells.push({html:'<div class="spec-row-actions">'+actionsHtml(block,scopeId,k,p,r)+'</div>',cls:'spec-actions-col'});
      out.push({attrs:'data-row data-row-key="'+esc(k)+'" data-search="'+esc(rowSearchText(values))+'"',cells});
      if(block.expand)out.push({attrs:'data-expand-row data-row-key="'+esc(k)+'" hidden',
        cells:[{html:'<div class="spec-expand" data-expand-host data-row-key="'+esc(k)+'" hidden></div>',attrs:'colspan="'+head.length+'"'}]});
    });
    return kit.table({head,rows:out})+'<p class="spec-nomatch" hidden>No rows match.</p>';
  }

  function renderDetail(block,p,row,key){
    const object=block.items!==undefined?evaluate(block.items,p,row):(row!==undefined?row:p);
    if(!object||typeof object!=='object')return emptyHtml(block);
    const columns=Array.isArray(block.columns)&&block.columns.length?block.columns
      :Object.keys(object).map(k=>({label:k,value:k}));
    const rows=columns.map(c=>{
      const value=text(mapped(c.value,c.map,p,object===p?row:object));
      const url=c.link?safeUrl(evaluate(c.link,p,object===p?row:object)):null;
      const inner=url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(value)+'</a>':esc(value);
      return'<dt>'+esc(c.label||'')+'</dt><dd'+(c.mono?' class="spec-mono"':'')+'>'+(inner||'<span class="spec-none">none</span>')+'</dd>';
    });
    return'<dl class="spec-detail">'+rows.join('')+'</dl>';
  }

  function renderForm(block,p,row,key){
    const fields=Array.isArray(block.fields)?block.fields:[];
    const scopeId=scope(block,p,row,null,key);
    const controls=fields.map((f,i)=>{
      const id='f'+scopeId+'-'+i;
      const value=f.value!==undefined?evaluate(f.value,p,row):'';
      const type=f.type||'text';
      let control='';
      if(type==='textarea')control='<textarea data-slot="textarea" id="'+id+'" name="'+esc(f.name)+'"'+(f.placeholder?' placeholder="'+esc(f.placeholder)+'"':'')+(f.required?' required':'')+'>'+esc(text(value))+'</textarea>';
      else if(type==='select'){
        const options=Array.isArray(f.options)?f.options:asRows(evaluate(f.options_from,p,row)).map(o=>(
          o&&typeof o==='object'?{value:o.value!==undefined?o.value:o.key,label:o.label!==undefined?o.label:(o.value!==undefined?o.value:o.key)}:{value:o,label:o}));
        control='<select data-slot="native-select" id="'+id+'" name="'+esc(f.name)+'">'+options.map(o=>
          '<option value="'+esc(text(o.value))+'"'+(text(o.value)===text(value)?' selected':'')+'>'+esc(text(o.label))+'</option>').join('')+'</select>';
      }else if(type==='toggle')control=kit.switchControl({checked:truthy(value),id,attrs:'data-field name="'+esc(f.name)+'"'});
      else control=kit.input({value:text(value),placeholder:f.placeholder||'',type:type==='number'?'number':type==='password'?'password':'text',id,
        attrs:'name="'+esc(f.name)+'"'+(f.min!==undefined?' min="'+esc(f.min)+'"':'')+(f.max!==undefined?' max="'+esc(f.max)+'"':'')+(f.required?' required':'')});
      return kit.field({label:esc(f.label||f.name),htmlFor:id,control,description:f.help?esc(f.help):''});
    }).join('');
    const submit=block.submit?kit.button(esc(block.title||'Save'),{variant:'default',size:'sm',type:'submit'}):'';
    return'<form class="spec-form" data-form data-scope="'+scopeId+'" novalidate>'+controls+(submit?'<div class="spec-form-actions">'+submit+'</div>':'')+'</form>';
  }

  function renderToggles(block,p,row,key){
    const scopeId=scope(block,p,row,null,key);
    const defs=Array.isArray(block.toggles)?block.toggles:[];
    const items=[];
    for(const def of defs){
      if(def.rows!==undefined){
        for(const item of asRows(evaluate(def.rows,p,row))){
          const merged=row&&typeof row==='object'?{...row,...item}:item;
          if(def.when!==undefined&&!truthy(evaluate(def.when,p,merged)))continue;
          items.push({label:fill(def.label,p,merged),help:def.help?fill(def.help,p,merged):'',
            path:fill(def.path||'',p,merged),checked:truthy(evaluate(def.value,p,merged))});
        }
        continue;
      }
      if(def.when!==undefined&&!truthy(evaluate(def.when,p,row)))continue;
      items.push({label:fill(def.label,p,row),help:def.help?fill(def.help,p,row):'',
        path:fill(def.path||'',p,row),checked:truthy(evaluate(def.value,p,row))});
    }
    if(!items.length)return emptyHtml(block,'Nothing to switch here.');
    return'<div class="spec-toggles">'+items.map((it,i)=>{
      const id='t'+scopeId+'-'+i;
      return'<div class="spec-toggle" data-row data-search="'+esc(rowSearchText([it.label,it.help]))+'">'
        +'<label class="spec-toggle-text" for="'+id+'"><b>'+esc(it.label)+'</b>'+(it.help?'<small>'+esc(it.help)+'</small>':'')+'</label>'
        +kit.switchControl({checked:it.checked,id,attrs:'data-toggle data-scope="'+scopeId+'" data-path="'+esc(it.path)+'"'+(block.write?'':' disabled')})
        +'</div>';
    }).join('')+'</div>';
  }

  function renderTimeline(block,p,row,key){
    let rows=asRows(evaluate(block.items,p,row));
    if(block.limit)rows=rows.slice(0,block.limit);
    if(!rows.length)return emptyHtml(block);
    const spec=block.row||{};
    const scopeId=scope(block,p,row,rows,key);
    return'<ol class="spec-timeline">'+rows.map((r,i)=>{
      const k=rowKey(block,r,i);
      const detail=text(evaluate(spec.detail,p,r));
      const meta=Array.isArray(spec.meta)?spec.meta.map(e=>evaluate(e,p,r)):[];
      const titleText=text(spec.title!==undefined?evaluate(spec.title,p,r):k);
      return'<li data-row data-row-key="'+esc(k)+'" data-search="'+esc(rowSearchText([titleText,detail,...meta]))+'">'
        +'<div class="spec-row-title">'+titleHtml(spec,p,r,k)+badgeHtml(spec.badge,p,r)+'</div>'
        +(detail?'<div class="spec-detail-text">'+esc(detail)+'</div>':'')+metaHtml(spec.meta,p,r)
        +(block.actions?'<div class="spec-row-actions">'+actionsHtml(block,scopeId,k,p,r)+'</div>':'')+'</li>';
    }).join('')+'</ol><p class="spec-nomatch" hidden>No rows match.</p>';
  }

  const dayKey=value=>{
    if(!value)return'';
    const t=new Date(value);
    return isFinite(t.getTime())?t.getFullYear()+'-'+String(t.getMonth()+1).padStart(2,'0')+'-'+String(t.getDate()).padStart(2,'0'):'';
  };
  function renderCalendar(block,p,row,key){
    const rows=asRows(evaluate(block.items,p,row));
    const x=block.x||'ts';
    const state=calendars.get(key)||{month:new Date(),selected:''};
    calendars.set(key,state);
    const marks=new Set(rows.map(r=>dayKey(evaluate(x,p,r))).filter(Boolean));
    const spec=block.row||{};
    const chosen=state.selected?rows.filter(r=>dayKey(evaluate(x,p,r))===state.selected):[];
    const list=state.selected
      ?(chosen.length?kit.itemGroup(chosen.map((r,i)=>kit.item({title:titleHtml(spec,p,r,String(i)),description:esc(text(evaluate(spec.detail,p,r))),
          footer:metaHtml(spec.meta,p,r),variant:'outline',attrs:'data-row data-search="'+esc(rowSearchText([text(evaluate(spec.title,p,r))]))+'"'})).join(''))
        :kit.empty({title:'Nothing on '+esc(state.selected)+'.',size:'sm'}))
      :'<p class="spec-detail-text">Pick a marked day.</p>';
    return'<div class="spec-calendar"><div data-calendar data-key="'+esc(key)+'">'
      +kit.calendar({month:state.month,selected:state.selected,marks})+'</div><div class="spec-calendar-day">'+list+'</div></div>';
  }
  function wireCalendarBlock(section,block,p,row,key){
    const box=section.querySelector('[data-calendar]');
    if(!box)return;
    const state=calendars.get(key);
    kit.wireCalendar(box,{
      onSelect(day){state.selected=state.selected===day?'':day;repaint(section,block,p,row,key);},
      onMonth(step){state.month=new Date(state.month.getFullYear(),state.month.getMonth()+step,1);repaint(section,block,p,row,key);},
    });
  }

  function renderChart(block,p,row,key){
    return'<div class="spec-chart" data-chart data-height="'+esc(block.height||'')+'"></div>';
  }
  function drawChart(section,block,p,row){
    const box=section.querySelector('[data-chart]');
    if(!box)return;
    const rows=asRows(evaluate(block.items,p,row));
    const kind=String(block.kind||'area');
    const x=block.x||'x';
    const series=Array.isArray(block.series)&&block.series.length?block.series:[{key:'y',label:block.title||'Value',value:block.y||'y'}];
    const config={};
    series.forEach((s,i)=>{config[s.key]={label:s.label||s.key,color:s.color||'var(--chart-'+((i%8)+1)+')'};});
    const height=block.height?parseInt(block.height,10)||undefined:undefined;
    try{
      if(kind==='bar'){
        config.value={label:series[0].label,color:series[0].color||'var(--chart-1)'};
        barChartHorizontal(box,{data:rows.map(r=>({label:text(evaluate(x,p,r)),value:Number(evaluate(series[0].value||series[0].key,p,r))||0})),config});
      }else if(kind==='donut'){
        rows.forEach((r,i)=>{config['k'+i]={label:text(evaluate(x,p,r)),color:'var(--chart-'+((i%8)+1)+')'};});
        donutChart(box,{data:rows.map((r,i)=>({key:'k'+i,value:Number(evaluate(series[0].value||series[0].key,p,r))||0})),config,height:height||240});
      }else{
        const data=rows.map(r=>{const d={x:text(evaluate(x,p,r))};series.forEach(s=>{d[s.key]=Number(evaluate(s.value||s.key,p,r))||0;});return d;});
        const draw=kind==='stacked'?barChartStacked:kind==='sparkline'?sparkline:areaChart;
        if(kind==='sparkline')sparkline(box,{data,key:series[0].key,config,height:height||44});
        else draw(box,{data,x:'x',series:series.map(s=>({key:s.key})),config,height});
      }
    }catch(err){console.error('chart failed:',err);box.innerHTML=kit.alert({variant:'destructive',description:'The chart could not be drawn.'});}
  }

  function renderText(block,p,row){
    const source=block.markdown!==undefined?String(block.markdown):text(evaluate(block.text,p,row));
    if(!source)return emptyHtml(block,'Nothing to show.');
    return'<div class="spec-text md">'+mdToHtml(source)+'</div>';
  }

  const RENDERERS={stats:renderStats,list:renderList,cards:renderCards,table:renderTable,detail:renderDetail,
    form:renderForm,toggles:renderToggles,timeline:renderTimeline,calendar:renderCalendar,chart:renderChart,text:renderText};

  /* Paint one block (or nested block) into a container. */
  async function paint(container,block,p,row,key,rev){
    let scopePayload=p;
    if(block.read){
      const res=await apiFetch(API_BASE+fill(block.read,p,row,true));
      if(rev!==generation||dead)return;
      if(!res.ok){container.innerHTML=failHtml(res);return;}
      scopePayload=res.data&&typeof res.data==='object'?res.data:{};
    }
    repaint(container,block,scopePayload,row,key);
    await paintExpansions(container,block,scopePayload,row,key,rev);
  }
  function repaint(container,block,p,row,key){
    const render=RENDERERS[block.type];
    if(!render){container.innerHTML=kit.alert({variant:'destructive',description:'Unknown block type '+esc(block.type)+'.'});return;}
    try{container.innerHTML=render(block,p,row,key);}
    catch(err){console.error('block '+block.type+' failed:',err);container.innerHTML=kit.alert({variant:'destructive',description:'This block could not be rendered.'});}
    if(block.type==='chart')drawChart(container,block,p,row);
    if(block.type==='calendar')wireCalendarBlock(container,block,p,row,key);
    applySearch(container);
  }
  async function paintExpansions(container,block,p,row,key,rev){
    if(!block.expand)return;
    const hosts=[...container.querySelectorAll('[data-expand-host]')].filter(h=>expanded.has(key+':'+h.dataset.rowKey));
    const found=[...scopes.values()].find(s=>s.block===block&&s.key===key);
    await Promise.all(hosts.map(async h=>{
      const r=found?.rowsByKey.get(h.dataset.rowKey);
      if(r===undefined)return;
      openHost(container,h,true);
      await paintNested(h,block,p,r,key+':'+h.dataset.rowKey,rev);
    }));
  }
  function openHost(container,h,open){
    h.hidden=!open;
    const tr=h.closest('[data-expand-row]');
    if(tr)tr.hidden=!open;
    const button=container.querySelector('[data-expand][data-row-key="'+CSS.escape(h.dataset.rowKey)+'"]');
    if(button)button.setAttribute('aria-expanded',open?'true':'false');
  }
  async function paintNested(h,block,p,r,key,rev){
    const nested=block.expand;
    if(!nested)return;
    if(PERSISTENT.has(nested.type)){h.innerHTML=kit.alert({description:'A '+esc(nested.type)+' block cannot be nested under a row.'});return;}
    h.innerHTML=kit.skeleton('height:38px');
    await paint(h,nested,p,r,key,rev);
  }

  /* ── search ── */
  function applySearch(container){
    const q=query.trim().toLowerCase();
    for(const el of container.querySelectorAll('[data-row]')){
      const hit=!q||String(el.dataset.search||'').includes(q);
      el.hidden=!hit;
      const key=el.dataset.rowKey;
      if(key!==undefined){
        const h=el.parentElement?.querySelector(':scope > [data-expand-host][data-row-key="'+CSS.escape(key)+'"]')
          ||el.querySelector('[data-expand-host]');
        if(h&&!h.hidden&&!hit)h.hidden=true;
        else if(h&&hit&&h.hidden&&[...expanded].some(e=>e.endsWith(':'+key)))h.hidden=false;
        const tr=el.parentElement?.querySelector(':scope > [data-expand-row][data-row-key="'+CSS.escape(key)+'"]');
        if(tr)tr.hidden=!hit||h?.hidden;
      }
    }
    for(const note of container.querySelectorAll('.spec-nomatch')){
      const group=note.previousElementSibling;
      const rows=group?[...group.querySelectorAll('[data-row]')]:[];
      note.hidden=!q||!rows.length||rows.some(r=>!r.hidden);
    }
  }

  /* ── events ── */
  async function onClick(event){
    const act=event.target.closest('[data-act]');
    if(act&&host.contains(act)){
      event.preventDefault();
      const s=scopes.get(act.dataset.scope);
      if(!s)return;
      const row=act.dataset.rowKey!==undefined?s.rowsByKey.get(act.dataset.rowKey):s.row;
      const action=(s.block.actions||[])[Number(act.dataset.act)];
      if(!action)return;
      await runAction(action,{payload:s.payload,row,ctx:actionCtx,button:act});
      return;
    }
    const toggle=event.target.closest('[data-toggle]');
    if(toggle&&host.contains(toggle)){
      event.preventDefault();
      await flip(toggle);
      return;
    }
    const expand=event.target.closest('[data-expand]');
    if(expand&&host.contains(expand)){
      event.preventDefault();
      const s=scopes.get(expand.dataset.scope);
      if(!s)return;
      const k=expand.dataset.rowKey;
      const section=expand.closest('.spec-block-body, .spec-expand');
      const h=section?.querySelector('[data-expand-host][data-row-key="'+CSS.escape(k)+'"]');
      const full=s.key+':'+k;
      if(expanded.has(full)){expanded.delete(full);if(h)openHost(section,h,false);return;}
      expanded.add(full);
      if(!h)return;
      openHost(section,h,true);
      const r=s.rowsByKey.get(k);
      await paintNested(h,s.block,s.payload,r,full,generation);
      return;
    }
    if(event.target.closest('a, button, input, select, textarea, label'))return;
    const opener=event.target.closest('[data-open]');
    if(opener&&host.contains(opener)&&opener.dataset.open)ctx.switchTo?.(opener.dataset.open);
  }

  async function flip(toggle){
    const s=scopes.get(toggle.dataset.scope);
    if(!s||toggle.disabled)return;
    const call=parseCall(s.block.write,s.payload,s.row);
    if(!call){toast('This switch has nowhere to write.');return;}
    const next=toggle.getAttribute('aria-checked')!=='true';
    toggle.disabled=true;toggle.setAttribute('aria-busy','true');
    try{
      const res=await apiFetch(API_BASE+call.path,{method:call.method,body:pathObject(toggle.dataset.path,next)});
      if(!res.ok){toast(failText(res));return;}
      toggle.setAttribute('aria-checked',next?'true':'false');
      toggle.dataset.state=next?'checked':'unchecked';
      dispatchEvent(new CustomEvent('space:wrote',{detail:{method:call.method,path:call.path}}));
      await ctx.refresh?.();
    }finally{
      if(toggle.isConnected){toggle.disabled=false;toggle.removeAttribute('aria-busy');}
    }
  }

  async function onSubmit(event){
    const form=event.target.closest('form[data-form]');
    if(!form||!host.contains(form))return;
    event.preventDefault();
    const s=scopes.get(form.dataset.scope);
    if(!s||!s.block.submit)return;
    const body={};
    for(const f of s.block.fields||[]){
      const el=form.querySelector('[name="'+CSS.escape(f.name)+'"]');
      if(!el)continue;
      if(el.getAttribute('role')==='switch')body[f.name]=el.getAttribute('aria-checked')==='true';
      else if(f.type==='number')body[f.name]=el.value===''?null:Number(el.value);
      else body[f.name]=el.value;
    }
    const button=form.querySelector('button[type="submit"]');
    await runAction({label:s.block.title||'Save',call:s.block.submit,then:'refresh'},{payload:s.payload,row:s.row,ctx:actionCtx,button,body});
  }

  /* ── the page-level API ── */
  async function render(next){
    if(dead)return;
    payload=next&&typeof next==='object'?next:{};
    const rev=++generation;
    scopes.clear();
    await Promise.all(blocks.map(async(block,i)=>{
      const section=host.querySelector('.spec-block[data-block="'+i+'"]');
      if(!section)return;
      const hidden=block.when!==undefined&&!truthy(evaluate(block.when,payload));
      section.hidden=hidden;
      if(hidden)return;
      const body=section.querySelector('.spec-block-body');
      if(block.type==='stream'){
        if(!persistent.has(i)){
          const spec=block.row||{};
          const controller=mountStream(body,block,{payload,renderLine:line=>'<div class="spec-row-title">'+titleHtml(spec,payload,line,text(line.type))+'</div>'
            +(spec.detail?'<div class="spec-detail-text">'+esc(text(evaluate(spec.detail,payload,line)))+'</div>':'')+metaHtml(spec.meta,payload,line)});
          persistent.set(i,controller);
          if(shown)controller.open();
        }
        return;
      }
      if(block.type==='widget'){
        if(!persistent.has(i))persistent.set(i,mountWidget(body,block,{switchTo:ctx.switchTo,refresh:ctx.refresh,page}));
        if(shown)persistent.get(i).update(block.data!==undefined?evaluate(block.data,payload):payload);
        return;
      }
      await paint(body,block,payload,undefined,String(i),rev);
    }));
  }
  function search(next){
    query=String(next??'');
    applySearch(host);
  }
  function show(){
    shown=true;
    for(const[i,controller]of persistent){
      const block=blocks[i];
      if(block.type==='stream')controller.open();
      else if(block.type==='widget')controller.update(block.data!==undefined?evaluate(block.data,payload):payload);
    }
  }
  function hide(){
    shown=false;
    for(const[i,controller]of persistent)if(blocks[i].type==='stream')controller.close();
  }
  function destroy(){
    dead=true;
    for(const[i,controller]of persistent){
      if(blocks[i].type==='stream')controller.close();
      else controller.destroy?.();
    }
    persistent.clear();
    host.removeEventListener('click',onClick);
    host.removeEventListener('submit',onSubmit);
  }
  return{render,search,show,hide,destroy,get payload(){return payload;}};
}
