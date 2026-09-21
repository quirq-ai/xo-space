/* Cmd+K command palette: a global, searchable overlay for jumping to any
   section, opening a project, running a quick action, or handing a query to
   the active page's own search.

   Shell chrome, like core/lens-switch.js: it never imports the view modules
   and navigates through the switchTo (and refreshCurrentView) it is handed at
   init, the same instances app.js uses, so the no-bundler stamp split can
   never hand it an empty registry. The navigable destinations are kept here
   as route vocabulary, the same way the lens switch keeps its lens list.

   Vanilla ES module + the shared CSS tokens, styled to read like shadcn's
   Command dialog (dimmed backdrop, centred panel, grouped list, keyboard
   nav, Esc to close). No React/Tailwind/build step. */
import {API_BASE,apiFetch} from './api.js';
import {toast} from './ui.js';

const esc=value=>String(value??'').replace(/[&<>"]/g,
  ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));

/* Curated navigation destinations: [route, label, group, keywords]. Each
   route is one switchTo understands (see core/navigation.js / setup-sections). */
const NAV=[
  ['projects/overview','Dashboard','Projects','overview home map'],
  ['projects/data/list','Projects list','Projects','list files catalog'],
  ['projects/data/graph','Graph','Projects','graph map nodes'],
  ['projects/data/tree','Tree','Projects','tree files folders'],
  ['projects/timeline','Timeline','Projects','time history commits'],
  ['projects/manage','Manage projects','Projects','manage create new project settings'],
  ['agents/overview','Agents','Agents','sessions telemetry overview tokens'],
  ['agents/sessions','Agent sessions','Agents','sessions list transcript'],
  ['agents/tools','Agent tools','Agents','tools usage'],
  ['agents/models','Agent models','Agents','models usage'],
  ['agents/trends','Agent trends','Agents','trends usage'],
  ['inbox/items','Inbox','Work','inbox items notifications work'],
  ['inbox/jobs','Work jobs','Work','scheduled manual jobs commands results'],
  ['inbox/activity','Activity','Work','activity feed'],
  ['sharing','Sharing','Work','sharing repos incoming'],
  ['setup/workspace','Setup','Setup','settings workspace roots'],
  ['setup/intelligence','Intelligence layer','Setup','agent runtime watcher intelligence'],
  ['setup/connections','Connections','Setup','connections connectors apps composio polling'],
  ['setup/secrets','Secrets','Setup','environment credentials secrets'],
  ['setup/commands','Jobs','Setup','jobs commands run schedule manual timeout'],
  ['setup/server','Server','Setup','restart update server'],
  ['setup/server/details','Quirq state','Setup','quirq machine local state'],
  ['wiki','Wiki','Help','wiki docs help guide'],
].map(([route,label,group,keywords])=>({kind:'nav',route,label,group,keywords}));

/* Fuzzy-ish match: every whitespace token must appear in the haystack. The
   score favours a prefix, then a word-boundary hit, then any substring, so a
   short exact query surfaces the obvious destination first. */
function scoreEntry(tokens,label,keywords){
  const name=label.toLowerCase();
  const hay=(label+' '+(keywords||'')).toLowerCase();
  let total=0;
  for(const token of tokens){
    if(!hay.includes(token))return -1;
    const at=name.indexOf(token);
    total+=at===0?3:at>0&&/\W/.test(name[at-1])?2:at>0?1:0.5;
  }
  return total;
}

export function initCommandPalette({switchTo,refreshCurrentView}={}){
  if(typeof switchTo!=='function')return;
  if(document.getElementById('cmdk'))return; /* idempotent: one palette only */

  const go=route=>{try{switchTo(route);}catch(err){console.error('Command palette navigation failed:',err);}};

  /* Quick actions: safe, page-agnostic verbs. Navigation lives in NAV. */
  const ACTIONS=[
    {kind:'action',label:'Refresh this page',group:'Actions',keywords:'reload refresh update',
     run:()=>{try{refreshCurrentView?.();}catch(err){console.error(err);}}},
    {kind:'action',label:'New project',group:'Actions',keywords:'create add project new',
     run:()=>go('projects/manage')},
    {kind:'action',label:'Copy link to this page',group:'Actions',keywords:'copy url share link',
     run:()=>{try{navigator.clipboard?.writeText(location.href);toast('Page link copied');}
       catch(_err){toast('Could not copy the link');}}},
  ];

  const overlay=document.createElement('div');
  overlay.id='cmdk';overlay.className='cmdk';overlay.hidden=true;
  overlay.innerHTML=''
    +'<div class="cmdk-dialog" role="dialog" aria-modal="true" aria-label="Command menu">'
    +  '<div class="cmdk-inputwrap">'
    +    '<svg class="cmdk-icon" width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">'
    +      '<circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2"/>'
    +      '<line x1="16.5" y1="16.5" x2="21" y2="21" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
    +    '</svg>'
    +    '<input class="cmdk-input" type="text" role="combobox" aria-expanded="true"'
    +      ' aria-controls="cmdk-list" aria-autocomplete="list" spellcheck="false"'
    +      ' placeholder="Search projects, pages and actions…" aria-label="Command menu search">'
    +    '<kbd class="cmdk-esc">Esc</kbd>'
    +  '</div>'
    +  '<div class="cmdk-list" id="cmdk-list" role="listbox" aria-label="Results"></div>'
    +  '<div class="cmdk-foot">'
    +    '<span><kbd>&#8593;</kbd><kbd>&#8595;</kbd> navigate</span>'
    +    '<span><kbd>&#8629;</kbd> open</span>'
    +    '<span><kbd>Esc</kbd> close</span>'
    +  '</div>'
    +'</div>';
  document.body.appendChild(overlay);

  const dialog=overlay.querySelector('.cmdk-dialog');
  const input=overlay.querySelector('.cmdk-input');
  const list=overlay.querySelector('#cmdk-list');

  let open=false;
  let rows=[];          /* flat, selectable results in render order */
  let active=0;         /* index into rows */
  let restoreFocus=null;
  let projects=null;    /* cached [{id,label,keywords}] from /api/xo-projects */
  let projectsLoading=false;

  const pageSearch=()=>{
    const wrap=document.getElementById('view-search-wrap');
    const field=document.getElementById('view-search');
    return wrap&&!wrap.hidden&&field&&!field.disabled?field:null;
  };

  async function loadProjects(){
    if(projects||projectsLoading)return;
    projectsLoading=true;
    const res=await apiFetch(API_BASE+'/api/xo-projects');
    projectsLoading=false;
    if(res.ok&&Array.isArray(res.data?.items)){
      projects=res.data.items.filter(p=>p&&typeof p.id==='string'&&p.id).map(p=>({
        kind:'project',id:p.id,label:p.display_name||p.id,group:'Projects',
        keywords:[p.id,p.description||''].join(' '),
      }));
      if(open)renderResults();
    }
  }

  function results(query){
    const tokens=query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const out=[];
    const field=pageSearch();
    if(tokens.length&&field){
      out.push({kind:'page-search',label:'Search "'+query.trim()+'" on this page',
        group:'This page',query:query.trim(),field});
    }
    const rank=pool=>pool.map(entry=>[tokens.length?scoreEntry(tokens,entry.label,entry.keywords):1,entry])
      .filter(([s])=>s>=0)
      .sort((a,b)=>b[0]-a[0])
      .map(([,entry])=>entry);
    out.push(...rank(ACTIONS));
    out.push(...rank(NAV));
    out.push(...rank(projects||[]).slice(0,tokens.length?12:6));
    return out;
  }

  function renderResults(){
    rows=results(input.value);
    if(active>=rows.length)active=Math.max(0,rows.length-1);
    if(!rows.length){
      list.innerHTML='<div class="cmdk-empty">No results'
        +(projectsLoading?' yet; still loading projects…':'')+'</div>';
      input.removeAttribute('aria-activedescendant');
      return;
    }
    let html='',lastGroup=null;
    rows.forEach((row,i)=>{
      if(row.group!==lastGroup){
        html+='<div class="cmdk-group" role="presentation">'+esc(row.group)+'</div>';
        lastGroup=row.group;
      }
      html+='<div class="cmdk-opt'+(i===active?' is-active':'')+'" role="option" id="cmdk-opt-'+i+'"'
        +' data-i="'+i+'" aria-selected="'+(i===active?'true':'false')+'">'
        +'<span class="cmdk-opt-label">'+esc(row.label)+'</span>'
        +(row.kind==='nav'?'<span class="cmdk-opt-hint">'+esc(row.group)+'</span>':'')
        +(row.kind==='project'?'<span class="cmdk-opt-hint">Project</span>':'')
        +'</div>';
    });
    list.innerHTML=html;
    syncActive();
  }

  function syncActive(){
    const opts=list.querySelectorAll('.cmdk-opt');
    opts.forEach((el,i)=>{
      const on=i===active;
      el.classList.toggle('is-active',on);
      el.setAttribute('aria-selected',on?'true':'false');
    });
    const current=opts[active];
    if(current){
      input.setAttribute('aria-activedescendant',current.id);
      current.scrollIntoView({block:'nearest'});
    }else input.removeAttribute('aria-activedescendant');
  }

  function move(delta){
    if(!rows.length)return;
    active=(active+delta+rows.length)%rows.length;
    syncActive();
  }

  function choose(row){
    if(!row)return;
    close();
    if(row.kind==='nav')return void go(row.route);
    if(row.kind==='action')return void row.run();
    if(row.kind==='project'){
      go('projects/data/list');
      dispatchEvent(new CustomEvent('space:open-project',{detail:row.id}));
      return;
    }
    if(row.kind==='page-search'){
      row.field.value=row.query;
      row.field.dispatchEvent(new Event('input',{bubbles:true}));
      row.field.focus();
    }
  }

  function openPalette(){
    if(open)return;
    open=true;
    restoreFocus=document.activeElement;
    overlay.hidden=false;
    input.value='';active=0;
    loadProjects();
    renderResults();
    requestAnimationFrame(()=>input.focus());
  }
  function close(){
    if(!open)return;
    open=false;
    overlay.hidden=true;
    if(restoreFocus&&typeof restoreFocus.focus==='function'){
      try{restoreFocus.focus({preventScroll:true});}catch(_err){}
    }
    restoreFocus=null;
  }

  input.addEventListener('input',renderResults);
  list.addEventListener('pointermove',event=>{
    const opt=event.target.closest('.cmdk-opt');
    if(opt){active=Number(opt.dataset.i)||0;syncActive();}
  });
  list.addEventListener('click',event=>{
    const opt=event.target.closest('.cmdk-opt');
    if(opt)choose(rows[Number(opt.dataset.i)]);
  });
  overlay.addEventListener('pointerdown',event=>{
    if(event.target===overlay)close(); /* click the backdrop to dismiss */
  });
  /* Keys while the palette is open: own Escape/arrows/Enter, and keep them
     from reaching the page underneath (its own Escape clears a page search). */
  dialog.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();}
    else if(event.key==='ArrowDown'){event.preventDefault();move(1);}
    else if(event.key==='ArrowUp'){event.preventDefault();move(-1);}
    else if(event.key==='Enter'){event.preventDefault();choose(rows[active]);}
  });
  /* Cmd+K / Ctrl+K from anywhere, including inside an input, so the palette
     is always one chord away. Capture phase + preventDefault overrides the
     browser's own Ctrl/Cmd+K. */
  addEventListener('keydown',event=>{
    if((event.metaKey||event.ctrlKey)&&!event.altKey&&(event.key==='k'||event.key==='K')){
      event.preventDefault();
      open?close():openPalette();
    }
  },{capture:true});
  /* The navbar trigger and the `/` shortcut ask to open without importing
     this module (shell chrome talks by event, like the lens switch). */
  addEventListener('space:open-command-palette',()=>{if(!open)openPalette();});
}
