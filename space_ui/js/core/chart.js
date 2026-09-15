/* shadcn Chart for Space: SVG, zero deps.

   In React, shadcn's Chart is a thin styling layer over Recharts:
   ChartContainer sets `--color-<key>` per series from a config, and
   ChartTooltipContent / ChartLegendContent render the tooltip and legend.
   Space has no bundler, so this module draws the Recharts part itself and
   emits the same data-slot markup, styled by the chart block in
   css/shadcn.css. Each chart function takes a host element, measures it,
   and renders a fresh SVG; the view re-renders on width changes.

   Shared contract:
     config: {key: {label, color}}   color is any CSS color, usually
                                      var(--chart-n)
     format: value -> display string (tooltips, axis, labels)
   Mouse math is container-local (getBoundingClientRect), never page-level. */
import {esc} from './ui.js';

const NS='http://www.w3.org/2000/svg';
let uidCounter=0;
const uid=()=>'ch'+(++uidCounter).toString(36);

function svgEl(tag,attrs={},parent=null){
  const el=document.createElementNS(NS,tag);
  for(const[k,v]of Object.entries(attrs))if(v!==undefined&&v!==null)el.setAttribute(k,String(v));
  if(parent)parent.appendChild(el);
  return el;
}
const text=(parent,x,y,str,attrs={})=>{const t=svgEl('text',{x,y,...attrs},parent);t.textContent=str;return t;};

/* ChartContainer: measures the host, sets the series colors, returns the
   SVG plus a tooltip node. `aspect` keeps shadcn's aspect-video default
   when no height is given. */
function container(host,{config={},height,aspect=16/9,minWidth=220}){
  host.innerHTML='';
  const el=document.createElement('div');
  el.dataset.slot='chart';el.dataset.chart=uid();
  for(const[key,c]of Object.entries(config))if(c?.color)el.style.setProperty('--color-'+key,c.color);
  host.appendChild(el);
  const w=Math.max(minWidth,Math.floor(host.clientWidth||el.clientWidth||600));
  const h=Math.round(height||w/aspect);
  const svg=svgEl('svg',{width:w,height:h,viewBox:'0 0 '+w+' '+h,role:'img'},el);
  const tip=document.createElement('div');
  tip.dataset.slot='chart-tooltip';tip.hidden=true;
  el.appendChild(tip);
  return{el,svg,tip,w,h};
}

function emptyNote(host,msg){
  host.innerHTML='<div data-slot="chart"><div class="chart-empty">'+esc(msg)+'</div></div>';
}

/* ChartTooltipContent markup: label row, then one row per item with the
   color indicator, the series name and the tabular value. */
function tooltipHtml(label,items,{indicator='dot'}={}){
  return(label?'<div data-slot="chart-tooltip-label">'+esc(label)+'</div>':'')
    +items.map(it=>'<div data-slot="chart-tooltip-item">'
      +'<div data-slot="chart-tooltip-indicator" data-indicator="'+indicator+'" style="--color-bg:'+it.color+';--color-border:'+it.color+'"></div>'
      +'<div data-slot="chart-tooltip-body"><span data-slot="chart-tooltip-name">'+esc(it.name)+'</span>'
      +'<span data-slot="chart-tooltip-value">'+esc(it.value)+'</span></div></div>').join('');
}
function showTip(ctx,x,y,html){
  const{tip,el}=ctx;
  tip.innerHTML=html;tip.hidden=false;
  const tw=tip.offsetWidth,th=tip.offsetHeight,W=el.clientWidth,H=el.clientHeight;
  let left=x+14,top=y-th/2;
  if(left+tw>W)left=Math.max(0,x-tw-14);
  top=Math.max(0,Math.min(H-th,top));
  tip.style.left=left+'px';tip.style.top=top+'px';
}
const hideTip=ctx=>{ctx.tip.hidden=true;};
const local=(svg,e)=>{const r=svg.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top];};

/* ChartLegendContent */
export function legendHtml(config,keys=Object.keys(config)){
  return'<div data-slot="chart-legend">'+keys.map(k=>'<div data-slot="chart-legend-item">'
    +'<div data-slot="chart-legend-indicator" style="--color-bg:'+(config[k]?.color||'currentColor')+'"></div>'+esc(config[k]?.label||k)+'</div>').join('')+'</div>';
}

/* "nice" axis ticks: 4 horizontal grid lines with rounded labels */
function niceMax(v){
  if(v<=0)return 1;
  const p=Math.pow(10,Math.floor(Math.log10(v)));
  const n=v/p;
  return(n<=1?1:n<=2?2:n<=2.5?2.5:n<=5?5:10)*p;
}
const pick=(n,want)=>{const step=Math.max(1,Math.ceil(n/want));const out=[];for(let i=0;i<n;i+=step)out.push(i);if(out[out.length-1]!==n-1&&n-1-out[out.length-1]>=step/2)out.push(n-1);return out;};

/* Area chart (shadcn "Area Chart - Gradient"): data [{x, <key>}], one or
   more series drawn as stacked-free overlapping areas. */
export function areaChart(host,{data,x='x',series,config,format=v=>String(v),xFormat=v=>String(v),height,aspect=3.2,tickMono=true}){
  if(!data?.length)return emptyNote(host,'No data in this window');
  const ctx=container(host,{config,height,aspect});
  const{svg,w,h}=ctx;
  const P={l:52,r:14,t:14,b:26};
  const iw=w-P.l-P.r,ih=h-P.t-P.b;
  const vmax=niceMax(Math.max(...data.map(d=>Math.max(...series.map(s=>Number(d[s.key])||0)))));
  const X=i=>P.l+(data.length===1?iw/2:iw*i/(data.length-1));
  const Y=v=>P.t+ih*(1-v/vmax);
  const defs=svgEl('defs',{},svg);
  for(let g=0;g<=3;g++){
    const gy=P.t+ih*g/3;
    svgEl('line',{x1:P.l,x2:w-P.r,y1:gy,y2:gy,class:'chart-grid-line'},svg);
    text(svg,P.l-8,gy+4,format(vmax*(1-g/3)),{class:'chart-axis-tick'+(tickMono?' mono':''),'text-anchor':'end'});
  }
  for(const i of pick(data.length,Math.max(2,Math.floor(iw/90))))
    text(svg,X(i),h-8,xFormat(data[i][x]),{class:'chart-axis-tick'+(tickMono?' mono':''),'text-anchor':i===0?'start':i===data.length-1?'end':'middle'});
  series.forEach(s=>{
    const color='var(--color-'+s.key+')';
    const gid=ctx.el.dataset.chart+'-'+s.key;
    const grad=svgEl('linearGradient',{id:gid,x1:0,y1:0,x2:0,y2:1},defs);
    svgEl('stop',{offset:'5%','stop-color':color,'stop-opacity':.8},grad);
    svgEl('stop',{offset:'95%','stop-color':color,'stop-opacity':.1},grad);
    const line=data.map((d,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(Number(d[s.key])||0).toFixed(1)).join('');
    svgEl('path',{d:line+'L'+X(data.length-1).toFixed(1)+' '+(P.t+ih)+'L'+X(0).toFixed(1)+' '+(P.t+ih)+'Z',fill:'url(#'+gid+')',stroke:'none'},svg);
    svgEl('path',{d:line,fill:'none',stroke:color,'stroke-width':2,'stroke-linejoin':'round'},svg);
  });
  const cursor=svgEl('line',{y1:P.t,y2:P.t+ih,class:'chart-cursor-line',visibility:'hidden'},svg);
  const dots=series.map(s=>svgEl('circle',{r:4,fill:'var(--color-'+s.key+')',stroke:'var(--background)','stroke-width':2,visibility:'hidden'},svg));
  const hit=svgEl('rect',{x:P.l,y:P.t,width:iw,height:ih,fill:'transparent'},svg);
  hit.addEventListener('mousemove',e=>{
    const[mx,my]=local(svg,e);
    const i=Math.max(0,Math.min(data.length-1,Math.round((mx-P.l)/(iw/Math.max(1,data.length-1)))));
    cursor.setAttribute('x1',X(i));cursor.setAttribute('x2',X(i));cursor.setAttribute('visibility','visible');
    series.forEach((s,k)=>{dots[k].setAttribute('cx',X(i));dots[k].setAttribute('cy',Y(Number(data[i][s.key])||0));dots[k].setAttribute('visibility','visible');});
    showTip(ctx,mx,my,tooltipHtml(xFormat(data[i][x]),series.map(s=>({color:config[s.key]?.color||'currentColor',name:config[s.key]?.label||s.key,value:format(Number(data[i][s.key])||0)}))));
  });
  hit.addEventListener('mouseleave',()=>{cursor.setAttribute('visibility','hidden');dots.forEach(d=>d.setAttribute('visibility','hidden'));hideTip(ctx);});
  return ctx;
}

/* Sparkline: an area chart with no axes, for a card's usage strip. */
export function sparkline(host,{data,x='x',key='v',config,format=v=>String(v),xFormat=v=>String(v),height=44}){
  if(!data?.length||!data.some(d=>(Number(d[key])||0)>0))return emptyNote(host,'No activity yet');
  const ctx=container(host,{config,height});
  const{svg,w,h}=ctx;
  const P={l:2,r:2,t:4,b:2};
  const iw=w-P.l-P.r,ih=h-P.t-P.b;
  const vmax=Math.max(...data.map(d=>Number(d[key])||0))||1;
  const X=i=>P.l+(data.length===1?iw/2:iw*i/(data.length-1));
  const Y=v=>P.t+ih*(1-v/vmax);
  const color='var(--color-'+key+')';
  const gid=ctx.el.dataset.chart+'-'+key;
  const grad=svgEl('linearGradient',{id:gid,x1:0,y1:0,x2:0,y2:1},svgEl('defs',{},svg));
  svgEl('stop',{offset:'5%','stop-color':color,'stop-opacity':.6},grad);
  svgEl('stop',{offset:'95%','stop-color':color,'stop-opacity':.05},grad);
  const line=data.map((d,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(Number(d[key])||0).toFixed(1)).join('');
  svgEl('path',{d:line+'L'+X(data.length-1).toFixed(1)+' '+(P.t+ih)+'L'+X(0).toFixed(1)+' '+(P.t+ih)+'Z',fill:'url(#'+gid+')'},svg);
  svgEl('path',{d:line,fill:'none',stroke:color,'stroke-width':1.5,'stroke-linejoin':'round'},svg);
  const dot=svgEl('circle',{r:3,fill:color,stroke:'var(--background)','stroke-width':1.5,visibility:'hidden'},svg);
  const hit=svgEl('rect',{x:0,y:0,width:w,height:h,fill:'transparent'},svg);
  hit.addEventListener('mousemove',e=>{
    const[mx,my]=local(svg,e);
    const i=Math.max(0,Math.min(data.length-1,Math.round((mx-P.l)/(iw/Math.max(1,data.length-1)))));
    dot.setAttribute('cx',X(i));dot.setAttribute('cy',Y(Number(data[i][key])||0));dot.setAttribute('visibility','visible');
    showTip(ctx,mx,my,tooltipHtml(xFormat(data[i][x]),[{color:config[key]?.color||'currentColor',name:config[key]?.label||key,value:format(Number(data[i][key])||0)}]));
  });
  hit.addEventListener('mouseleave',()=>{dot.setAttribute('visibility','hidden');hideTip(ctx);});
  return ctx;
}

/* Horizontal bar chart (shadcn "Bar Chart - Horizontal" with labels): data
   [{label, value}], one series keyed `value`. */
export function barChartHorizontal(host,{data,config,key='value',format=v=>String(v),rowHeight=30,labelWidth}){
  if(!data?.length)return emptyNote(host,'No data');
  const lw=labelWidth||Math.min(190,Math.max(80,Math.max(...data.map(d=>String(d.label).length))*7.2+12));
  const ctx=container(host,{config,height:data.length*rowHeight+8});
  const{svg,w}=ctx;
  const vmax=Math.max(...data.map(d=>Number(d[key])||0))||1;
  const bw=w-lw-78;
  data.forEach((d,i)=>{
    const y=4+i*rowHeight,v=Number(d[key])||0;
    const row=svgEl('g',{class:'chart-row'},svg);
    const cur=svgEl('rect',{x:0,y,width:w,height:rowHeight,rx:4,class:'chart-cursor-rect',visibility:'hidden'},row);
    text(row,lw-10,y+rowHeight/2+4,String(d.label),{class:'chart-axis-tick','text-anchor':'end'});
    svgEl('rect',{x:lw,y:y+6,width:Math.max(2,bw*v/vmax),height:rowHeight-12,rx:4,fill:'var(--color-'+key+')',class:'chart-bar'},row);
    text(row,lw+Math.max(2,bw*v/vmax)+8,y+rowHeight/2+4,format(v),{class:'chart-label'});
    const hit=svgEl('rect',{x:0,y,width:w,height:rowHeight,fill:'transparent'},row);
    hit.addEventListener('mousemove',e=>{const[mx,my]=local(svg,e);cur.setAttribute('visibility','visible');
      showTip(ctx,mx,my,tooltipHtml(String(d.label),[{color:config[key]?.color||'currentColor',name:config[key]?.label||key,value:format(v)}]));});
    hit.addEventListener('mouseleave',()=>{cur.setAttribute('visibility','hidden');hideTip(ctx);});
  });
  return ctx;
}

/* Stacked vertical bars with legend (shadcn "Bar Chart - Stacked + Legend"):
   data [{x, <key>...}], series in stack order (bottom first). */
export function barChartStacked(host,{data,x='x',series,config,format=v=>String(v),xFormat=v=>String(v),height,aspect=3,legend=true,tickMono=true}){
  if(!data?.length)return emptyNote(host,'No data');
  const ctx=container(host,{config,height,aspect});
  const{svg,w,h}=ctx;
  const P={l:52,r:14,t:14,b:26};
  const iw=w-P.l-P.r,ih=h-P.t-P.b;
  const totals=data.map(d=>series.reduce((a,s)=>a+(Number(d[s.key])||0),0));
  const vmax=niceMax(Math.max(...totals));
  const slot=iw/data.length,barW=Math.max(4,Math.min(48,slot*0.62));
  const Y=v=>P.t+ih*(1-v/vmax);
  for(let g=0;g<=3;g++){
    const gy=P.t+ih*g/3;
    svgEl('line',{x1:P.l,x2:w-P.r,y1:gy,y2:gy,class:'chart-grid-line'},svg);
    text(svg,P.l-8,gy+4,format(vmax*(1-g/3)),{class:'chart-axis-tick'+(tickMono?' mono':''),'text-anchor':'end'});
  }
  const ticks=new Set(pick(data.length,Math.max(2,Math.floor(iw/80))));
  data.forEach((d,i)=>{
    const cx=P.l+slot*(i+.5);
    const col=svgEl('g',{},svg);
    const cur=svgEl('rect',{x:P.l+slot*i,y:P.t,width:slot,height:ih,class:'chart-cursor-rect',visibility:'hidden'},col);
    let acc=0;
    const visible=series.filter(s=>(Number(d[s.key])||0)>0);
    visible.forEach((s,k)=>{
      const v=Number(d[s.key])||0,y0=Y(acc),y1=Y(acc+v);
      const top=k===visible.length-1,r=top?4:0;
      const hgt=Math.max(0,y0-y1);
      if(top&&hgt>0){
        const x0=cx-barW/2,x1=cx+barW/2,rr=Math.min(r,hgt,barW/2);
        svgEl('path',{d:'M'+x0+' '+y0+'V'+(y1+rr)+'Q'+x0+' '+y1+' '+(x0+rr)+' '+y1+'H'+(x1-rr)+'Q'+x1+' '+y1+' '+x1+' '+(y1+rr)+'V'+y0+'Z',fill:'var(--color-'+s.key+')',class:'chart-bar'},col);
      }else svgEl('rect',{x:cx-barW/2,y:y1,width:barW,height:hgt,fill:'var(--color-'+s.key+')',class:'chart-bar'},col);
      acc+=v;
    });
    if(ticks.has(i))text(svg,cx,h-8,xFormat(d[x]),{class:'chart-axis-tick'+(tickMono?' mono':''),'text-anchor':'middle'});
    const hit=svgEl('rect',{x:P.l+slot*i,y:P.t,width:slot,height:ih,fill:'transparent'},col);
    hit.addEventListener('mousemove',e=>{const[mx,my]=local(svg,e);cur.setAttribute('visibility','visible');
      showTip(ctx,mx,my,tooltipHtml(xFormat(d[x]),series.filter(s=>(Number(d[s.key])||0)>0).map(s=>({color:config[s.key]?.color||'currentColor',name:config[s.key]?.label||s.key,value:format(Number(d[s.key])||0)}))
        .concat([{color:'transparent',name:'Total',value:format(totals[i])}])));});
    hit.addEventListener('mouseleave',()=>{cur.setAttribute('visibility','hidden');hideTip(ctx);});
  });
  if(legend)ctx.el.insertAdjacentHTML('beforeend',legendHtml(config,series.map(s=>s.key)));
  if(legend)ctx.el.style.flexWrap='wrap';
  return ctx;
}

/* Donut (shadcn "Pie Chart - Donut with Text"): data [{key, value}], the
   center shows a big value and a muted label. Hovered sectors grow like
   the interactive pie example. */
export function donutChart(host,{data,config,format=v=>String(v),center={},height=240,legend=true}){
  const rows=data.filter(d=>(Number(d.value)||0)>0);
  if(!rows.length)return emptyNote(host,'No data');
  const ctx=container(host,{config,height});
  const{svg,w,h}=ctx;
  const cx=w/2,cy=h/2,R=Math.min(w,h)/2-10,r0=R*0.62;
  const total=rows.reduce((a,d)=>a+(Number(d.value)||0),0);
  const arc=(a0,a1,ro,ri)=>{
    const p=(a,rad)=>[cx+rad*Math.cos(a),cy+rad*Math.sin(a)];
    const[x0,y0]=p(a0,ro),[x1,y1]=p(a1,ro),[x2,y2]=p(a1,ri),[x3,y3]=p(a0,ri);
    const big=a1-a0>Math.PI?1:0;
    return'M'+x0+' '+y0+'A'+ro+' '+ro+' 0 '+big+' 1 '+x1+' '+y1+'L'+x2+' '+y2+'A'+ri+' '+ri+' 0 '+big+' 0 '+x3+' '+y3+'Z';
  };
  let a=-Math.PI/2;
  rows.forEach(d=>{
    const v=Number(d.value)||0,a1=a+2*Math.PI*v/total;
    const sector=svgEl('path',{d:arc(a,Math.min(a1,a+2*Math.PI-1e-4),R,r0),fill:'var(--color-'+d.key+')',stroke:'var(--card)','stroke-width':3,class:'chart-sector'},svg);
    const a0=a;
    sector.addEventListener('mousemove',e=>{const[mx,my]=local(svg,e);sector.setAttribute('d',arc(a0,Math.min(a1,a0+2*Math.PI-1e-4),R+6,r0));
      showTip(ctx,mx,my,tooltipHtml('',[{color:config[d.key]?.color||'currentColor',name:config[d.key]?.label||d.key,value:format(v)+' · '+(100*v/total).toFixed(1)+'%'}]));});
    sector.addEventListener('mouseleave',()=>{sector.setAttribute('d',arc(a0,Math.min(a1,a0+2*Math.PI-1e-4),R,r0));hideTip(ctx);});
    a=a1;
  });
  if(center.value!==undefined)text(svg,cx,cy+(center.label?4:10),String(center.value),{class:'chart-big','text-anchor':'middle'});
  if(center.label)text(svg,cx,cy+24,String(center.label),{class:'chart-big-sub','text-anchor':'middle'});
  if(legend){ctx.el.insertAdjacentHTML('beforeend',legendHtml(config,rows.map(d=>d.key)));ctx.el.style.flexWrap='wrap';}
  return ctx;
}

/* Radial gauge (shadcn "Radial Chart - Text"): one value in 0..1 drawn as
   an arc over a muted track, with the number in the middle. */
export function radialChart(host,{value,key='value',config,text:big,label,height=200}){
  const ctx=container(host,{config,height});
  const{svg,w,h}=ctx;
  const cx=w/2,cy=h/2+10,R=Math.min(w,h)/2-6,sw=Math.max(10,R*0.22),r=R-sw/2;
  const start=Math.PI*0.75,sweep=Math.PI*1.5;
  const arc=(a0,a1)=>{const big=a1-a0>Math.PI?1:0;return'M'+(cx+r*Math.cos(a0))+' '+(cy+r*Math.sin(a0))+'A'+r+' '+r+' 0 '+big+' 1 '+(cx+r*Math.cos(a1))+' '+(cy+r*Math.sin(a1));};
  svgEl('path',{d:arc(start,start+sweep),fill:'none',stroke:'var(--muted)','stroke-width':sw,'stroke-linecap':'round'},svg);
  const v=Math.max(0,Math.min(1,Number(value)||0));
  if(v>0)svgEl('path',{d:arc(start,start+sweep*Math.max(v,0.002)),fill:'none',stroke:'var(--color-'+key+')','stroke-width':sw,'stroke-linecap':'round'},svg);
  text(svg,cx,cy+4,String(big),{class:'chart-big','text-anchor':'middle'});
  if(label)text(svg,cx,cy+24,String(label),{class:'chart-big-sub','text-anchor':'middle'});
  return ctx;
}

/* Calendar heatmap: byDay Map(YYYY-MM-DD -> value), the last `weeks`
   weeks ending this week, one column per week. Not a Recharts chart, but
   it shares the container, colors and tooltip. */
export function heatmapChart(host,{byDay,weeks=16,key='value',config,format=v=>String(v),cell=15,gap=3}){
  const left=34,top=6;
  const width=left+weeks*(cell+gap)+6,height=top+7*(cell+gap)+14;
  const ctx=container(host,{config,height,minWidth:width});
  const{svg}=ctx;
  svg.setAttribute('width',width);svg.setAttribute('viewBox','0 0 '+width+' '+height);
  ctx.el.style.overflowX='auto';ctx.el.style.justifyContent='flex-start';
  const today=new Date(),dow=(today.getDay()+6)%7;
  const end=new Date(today.getTime()+(6-dow)*864e5);
  const start=new Date(end.getTime()-(weeks*7-1)*864e5);
  let vmax=1;byDay.forEach(v=>{if(v>vmax)vmax=v;});
  ['Mon','','Wed','','Fri','','Sun'].forEach((l,r)=>l&&text(svg,2,top+r*(cell+gap)+11,l,{class:'chart-axis-tick'}));
  for(let i=0;i<weeks*7;i++){
    const d=new Date(start.getTime()+i*864e5);
    if(d>today)break;
    const dayKey=d.toISOString().slice(0,10),v=byDay.get(dayKey)||0;
    const px=left+Math.floor(i/7)*(cell+gap),py=top+(i%7)*(cell+gap);
    const rect=svgEl('rect',{x:px,y:py,width:cell,height:cell,rx:3,fill:v?'var(--color-'+key+')':'var(--muted)','fill-opacity':v?(0.15+0.85*Math.sqrt(v/vmax)).toFixed(3):1},svg);
    rect.addEventListener('mousemove',e=>{const[mx,my]=local(svg,e);
      showTip(ctx,mx,my,tooltipHtml(dayKey,[{color:config[key]?.color||'currentColor',name:config[key]?.label||key,value:format(v)}]));});
    rect.addEventListener('mouseleave',()=>hideTip(ctx));
  }
  return ctx;
}
