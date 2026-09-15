/* shadcn/ui markup builders for Space. Each function returns the HTML
   string the matching React component renders (same data-slot attributes,
   same structure), so css/shadcn.css styles it identically. Pure string
   builders like ui.js: callers own the DOM, attach listeners after
   innerHTML, and pass `attrs` for ids, data-* hooks and aria. Every value
   that reaches an attribute or text goes through esc() unless the caller
   hands over pre-built HTML (the `html`-named arguments). */
import {esc} from './ui.js';

const attr=(name,value)=>value===undefined||value===null||value===false?'':' '+name+'="'+esc(value)+'"';
const a=attrs=>attrs?' '+attrs:'';

/* lucide icons the ported components ship with (16px stroke set) */
const ico=body=>'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+body+'</svg>';
export const icons={
  check:ico('<path d="M20 6 9 17l-5-5"/>'),
  chevronLeft:ico('<path d="m15 18-6-6 6-6"/>'),
  chevronRight:ico('<path d="m9 18 6-6-6-6"/>'),
  chevronUp:ico('<path d="m18 15-6-6-6 6"/>'),
  chevronDown:ico('<path d="m6 9 6 6 6-6"/>'),
  chevronsUpDown:ico('<path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/>'),
  arrowUp:ico('<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>'),
  arrowDown:ico('<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>'),
  refresh:ico('<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/>'),
  alert:ico('<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>'),
  inbox:ico('<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'),
  filter:ico('<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>'),
  dots:ico('<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'),
  loader:ico('<path d="M21 12a9 9 0 1 1-6.219-8.56"/>'),
  bot:ico('<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>'),
  wifiOff:ico('<path d="M12 20h.01"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/><path d="M5 12.859a10 10 0 0 1 5.17-2.69"/><path d="M19 12.859a10 10 0 0 0-2.007-1.523"/><path d="M2 8.82a15 15 0 0 1 4.177-2.643"/><path d="M22 8.82a15 15 0 0 0-11.288-3.764"/><path d="m2 2 20 20"/>'),
  search:ico('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>'),
  download:ico('<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>'),
  activity:ico('<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>'),
  layers:ico('<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"/><path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"/><path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"/>'),
  coins:ico('<circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18"/><path d="M7 6h1v4"/><path d="m16.71 13.88.7.71-2.82 2.82"/>'),
  sparkles:ico('<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>'),
  shield:ico('<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>'),
  folder:ico('<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>'),
  wrench:ico('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'),
};

/* Button: buttonVariants({variant,size}). `tag` lets a link carry the same
   look (pagination, breadcrumbs). */
export function button(inner,{variant='default',size='default',type='button',tag='button',cls='',attrs='',disabled=false,slot='button'}={}){
  return'<'+tag+(tag==='button'?' type="'+type+'"':'')+' class="ui-btn'+(cls?' '+cls:'')+'" data-slot="'+slot+'" data-variant="'+variant+'" data-size="'+size+'"'
    +(disabled?(tag==='button'?' disabled':' aria-disabled="true"'):'')+a(attrs)+'>'+inner+'</'+tag+'>';
}

export function badge(inner,{variant='default',cls='',attrs='',mono=false,source=null}={}){
  return'<span data-slot="badge" data-variant="'+variant+'"'+(mono?' data-mono':'')+attr('data-source',source)+(cls?' class="'+cls+'"':'')+a(attrs)+'>'+inner+'</span>';
}

/* Card: header (title, description, action), content, footer. Every part
   is optional; `stat` renders the dashboard "section card" variant. */
export function card({title,description,action,content,footer,before='',cls='',attrs='',stat=false,tone=null,headerCls='',footerCls=''}={}){
  const header=title||description||action
    ?'<div data-slot="card-header"'+(headerCls?' class="'+headerCls+'"':'')+'>'
      +(stat?(description?'<div data-slot="card-description">'+description+'</div>':'')+(title?'<div data-slot="card-title">'+title+'</div>':'')
        :(title?'<div data-slot="card-title">'+title+'</div>':'')+(description?'<div data-slot="card-description">'+description+'</div>':''))
      +(action?'<div data-slot="card-action">'+action+'</div>':'')
    +'</div>':'';
  return'<div data-slot="card"'+(stat?' data-stat':'')+attr('data-tone',tone)+(cls?' class="'+cls+'"':'')+a(attrs)+'>'
    +before
    +header
    +(content?'<div data-slot="card-content">'+content+'</div>':'')
    +(footer?'<div data-slot="card-footer"'+(footerCls?' class="'+footerCls+'"':'')+'>'+footer+'</div>':'')
  +'</div>';
}

/* Table. head: [{html, cls, attrs}], rows: [{attrs, cls, cells:[{html, cls, attrs}]}].
   Cell html is trusted (callers escape their values). */
export function table({head=[],rows=[],caption='',cls='',attrs='',empty=''}={}){
  const th=head.map(h=>'<th data-slot="table-head"'+(h.cls?' class="'+h.cls+'"':'')+a(h.attrs)+'>'+h.html+'</th>').join('');
  const body=rows.map(r=>'<tr data-slot="table-row"'+(r.cls?' class="'+r.cls+'"':'')+a(r.attrs)+'>'
    +r.cells.map(c=>'<td data-slot="table-cell"'+(c.cls?' class="'+c.cls+'"':'')+a(c.attrs)+'>'+c.html+'</td>').join('')+'</tr>').join('');
  return'<div data-slot="table-container"'+a(attrs)+'><table data-slot="table"'+(cls?' class="'+cls+'"':'')+'>'
    +(caption?'<caption data-slot="table-caption">'+caption+'</caption>':'')
    +(th?'<thead data-slot="table-header"><tr data-slot="table-row">'+th+'</tr></thead>':'')
    +'<tbody data-slot="table-body">'+body+'</tbody></table>'
    +(!rows.length&&empty?empty:'')+'</div>';
}

/* Sortable column header: the data-table pattern (ghost button + arrow). */
export function sortHead(label,{key,active=false,dir=-1,cls='',attrs=''}){
  const icon=active?(dir<0?icons.arrowDown:icons.arrowUp):icons.chevronsUpDown;
  return{html:button(label+icon,{variant:'ghost',size:'sm',attrs:'data-k="'+esc(key)+'"'}),cls,
    attrs:(active?'aria-sort="'+(dir<0?'descending':'ascending')+'" ':'')+attrs};
}

export function checkbox({checked=false,disabled=false,attrs='',id=''}={}){
  return'<button type="button" role="checkbox" data-slot="checkbox" aria-checked="'+(checked?'true':'false')+'" data-state="'+(checked?'checked':'unchecked')+'"'
    +attr('id',id)+(disabled?' disabled':'')+a(attrs)+'><span data-slot="checkbox-indicator">'+icons.check+'</span></button>';
}
export function label(inner,{htmlFor='',attrs='',disabled=false}={}){
  return'<label data-slot="label"'+attr('for',htmlFor)+(disabled?' data-disabled="true"':'')+a(attrs)+'>'+inner+'</label>';
}

/* ToggleGroup type="single": items [{value,label,attrs}]. Radix renders the
   group as role="group" and each item as role="radio". */
export function toggleGroup({items,value,variant='outline',size='sm',attrs='',ariaLabel=''}){
  return'<div role="group" data-slot="toggle-group" data-variant="'+variant+'" data-size="'+size+'"'+attr('aria-label',ariaLabel)+a(attrs)+'>'
    +items.map(it=>'<button type="button" role="radio" data-slot="toggle-group-item" data-variant="'+variant+'" data-size="'+size+'" '
      +'aria-checked="'+(it.value===value?'true':'false')+'" data-state="'+(it.value===value?'on':'off')+'" data-value="'+esc(it.value)+'"'+a(it.attrs)+'>'+it.label+'</button>').join('')
  +'</div>';
}

/* Tabs: items [{value,label,content}]. Panels render inside the tabs root;
   wireTabs() switches them without a re-render and reports the choice. */
export function tabs({items,value,attrs='',listCls=''}){
  return'<div data-slot="tabs"'+a(attrs)+'><div role="tablist" data-slot="tabs-list"'+(listCls?' class="'+listCls+'"':'')+'>'
    +items.map(it=>'<button type="button" role="tab" data-slot="tabs-trigger" aria-selected="'+(it.value===value)+'" data-state="'+(it.value===value?'active':'inactive')+'" data-value="'+esc(it.value)+'"'+a(it.attrs)+'>'+it.label+'</button>').join('')
  +'</div>'
  +items.filter(it=>it.content!==undefined).map(it=>'<div role="tabpanel" data-slot="tabs-content" data-value="'+esc(it.value)+'" data-state="'+(it.value===value?'active':'inactive')+'"'+(it.value===value?'':' hidden')+'>'+it.content+'</div>').join('')
  +'</div>';
}
export function wireTabs(root,onChange){
  root.querySelectorAll('[data-slot="tabs-trigger"]').forEach(trigger=>trigger.addEventListener('click',()=>{
    const value=trigger.dataset.value;
    root.querySelectorAll('[data-slot="tabs-trigger"]').forEach(t=>{const on=t===trigger;t.dataset.state=on?'active':'inactive';t.setAttribute('aria-selected',String(on));});
    root.querySelectorAll('[data-slot="tabs-content"]').forEach(panel=>{const on=panel.dataset.value===value;panel.dataset.state=on?'active':'inactive';panel.hidden=!on;});
    onChange?.(value);
  }));
}

/* Pagination with a windowed page list. Links carry data-page (0-based);
   the caller wires clicks. */
export function pagination({page,pages,attrs=''}){
  const link=(i,inner,extra='')=>'<li data-slot="pagination-item">'
    +button(inner,{tag:'a',variant:i===page?'outline':'ghost',size:'sm',slot:'pagination-link',attrs:'href="#" data-page="'+i+'" data-active="'+(i===page)+'"'+(i===page?' aria-current="page"':'')+extra})+'</li>';
  const nums=[];
  const span=new Set([0,pages-1,page-1,page,page+1].filter(i=>i>=0&&i<pages));
  let last=-1;
  for(const i of[...span].sort((x,y)=>x-y)){
    if(last>=0&&i-last>1)nums.push('<li data-slot="pagination-item"><span aria-hidden="true" data-slot="pagination-ellipsis">'+icons.dots+'<span class="sr-only">More pages</span></span></li>');
    nums.push(link(i,String(i+1)));last=i;
  }
  return'<nav role="navigation" aria-label="pagination" data-slot="pagination"'+a(attrs)+'><ul data-slot="pagination-content">'
    +'<li data-slot="pagination-item">'+button(icons.chevronLeft+'<span>Previous</span>',{tag:'a',variant:'ghost',size:'sm',disabled:page<=0,slot:'pagination-previous',attrs:'href="#" aria-label="Go to previous page" data-page="'+(page-1)+'"'})+'</li>'
    +nums.join('')
    +'<li data-slot="pagination-item">'+button('<span>Next</span>'+icons.chevronRight,{tag:'a',variant:'ghost',size:'sm',disabled:page>=pages-1,slot:'pagination-next',attrs:'href="#" aria-label="Go to next page" data-page="'+(page+1)+'"'})+'</li>'
  +'</ul></nav>';
}

export const skeleton=(style='',cls='')=>'<div data-slot="skeleton"'+(cls?' class="'+cls+'"':'')+attr('style',style)+'></div>';

export function alert({variant='default',icon='',title='',description='',attrs=''}={}){
  return'<div role="alert" data-slot="alert" data-variant="'+variant+'"'+a(attrs)+'>'+icon
    +(title?'<div data-slot="alert-title">'+title+'</div>':'')
    +(description?'<div data-slot="alert-description">'+description+'</div>':'')+'</div>';
}

/* Breadcrumb: items [{text, href, attrs}]; the last item is the page. */
export function breadcrumb(items,{attrs=''}={}){
  return'<nav aria-label="breadcrumb" data-slot="breadcrumb"'+a(attrs)+'><ol data-slot="breadcrumb-list">'
    +items.map((it,i)=>{
      const last=i===items.length-1;
      return(i?'<li data-slot="breadcrumb-separator" role="presentation" aria-hidden="true">'+icons.chevronRight+'</li>':'')
        +'<li data-slot="breadcrumb-item">'+(last
          ?'<span data-slot="breadcrumb-page" role="link" aria-disabled="true" aria-current="page">'+it.text+'</span>'
          :'<a data-slot="breadcrumb-link" href="'+esc(it.href||'#')+'"'+a(it.attrs)+'>'+it.text+'</a>')+'</li>';
    }).join('')+'</ol></nav>';
}

export function empty({icon='',title='',description='',content='',size='',attrs=''}={}){
  return'<div data-slot="empty"'+attr('data-size',size||null)+a(attrs)+'><div data-slot="empty-header">'
    +(icon?'<div data-slot="empty-media" data-variant="icon">'+icon+'</div>':'')
    +(title?'<div data-slot="empty-title">'+title+'</div>':'')
    +(description?'<div data-slot="empty-description">'+description+'</div>':'')
    +'</div>'+(content?'<div data-slot="empty-content">'+content+'</div>':'')+'</div>';
}

/* Item: media | content(title, description) | actions, with optional
   full-width header/footer rows. */
export function item({media,title,description,actions,header,footer,content,variant='default',size='default',cls='',attrs=''}={}){
  return'<div data-slot="item" data-variant="'+variant+'" data-size="'+size+'"'+(cls?' class="'+cls+'"':'')+a(attrs)+'>'
    +(header?'<div data-slot="item-header">'+header+'</div>':'')
    +(media?'<div data-slot="item-media" data-variant="icon">'+media+'</div>':'')
    +(title||description?'<div data-slot="item-content">'+(title?'<div data-slot="item-title">'+title+'</div>':'')+(description?'<div data-slot="item-description">'+description+'</div>':'')+'</div>':'')
    +(content||'')
    +(actions?'<div data-slot="item-actions">'+actions+'</div>':'')
    +(footer?'<div data-slot="item-footer">'+footer+'</div>':'')
  +'</div>';
}
export const itemGroup=(inner,attrs='')=>'<div role="list" data-slot="item-group"'+a(attrs)+'>'+inner+'</div>';
export const itemSeparator=()=>separator({cls:'',attrs:'data-slot="item-separator"'});

export function progress(value,{attrs=''}={}){
  const v=Math.max(0,Math.min(100,Number(value)||0));
  return'<div role="progressbar" data-slot="progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+v.toFixed(0)+'"'+a(attrs)+'>'
    +'<div data-slot="progress-indicator" style="transform:translateX(-'+(100-v).toFixed(2)+'%)"></div></div>';
}

/* Switch: a button role="switch" with a sliding thumb, like Radix. */
export function switchControl({checked=false,disabled=false,attrs='',id=''}={}){
  return'<button type="button" role="switch" data-slot="switch" aria-checked="'+(checked?'true':'false')+'" data-state="'+(checked?'checked':'unchecked')+'"'
    +attr('id',id)+(disabled?' disabled':'')+a(attrs)+'><span data-slot="switch-thumb"></span></button>';
}

export function input({value='',placeholder='',type='text',id='',mono=false,disabled=false,attrs=''}={}){
  return'<input data-slot="input" type="'+esc(type)+'"'+attr('id',id)+attr('value',value)+attr('placeholder',placeholder)
    +(mono?' data-mono':'')+(disabled?' disabled':'')+' autocomplete="off" spellcheck="false"'+a(attrs)+'>';
}

/* Field: label above a control, description and error below. */
export function field({label:labelHtml='',htmlFor='',control='',description='',error='',attrs=''}={}){
  return'<div data-slot="field"'+a(attrs)+'>'
    +(labelHtml?'<label data-slot="field-label"'+attr('for',htmlFor)+'>'+labelHtml+'</label>':'')
    +control
    +(description?'<div data-slot="field-description">'+description+'</div>':'')
    +'<div data-slot="field-error" role="alert">'+error+'</div>'
  +'</div>';
}

export function separator({orientation='horizontal',cls='',attrs=''}={}){
  return'<div role="none" data-slot="separator" data-orientation="'+orientation+'"'+(cls?' class="'+cls+'"':'')+a(attrs)+'></div>';
}

export const spinner=(label='Loading')=>icons.loader.replace('<svg ','<svg data-slot="spinner" role="status" aria-label="'+esc(label)+'" ');
