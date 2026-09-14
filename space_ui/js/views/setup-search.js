import {esc} from '../core/ui.js';

/* Search setting names, never form values or credentials. Results navigate to
   the existing controls without rebuilding forms or changing their drafts. */
const SETTINGS=[
  ['workspace','Workspace identity','Space ID, owner and account connections','space user name xo github status','#setup-workspace-title'],
  ['workspace','Projects folder','Workspace · Folders','root directory path projects','#xo-root-input'],
  ['workspace','Space data folder','Workspace · Folders','quirq settings storage directory credentials','#quirq-root-input'],
  ['agent','Agent & access','Choose the agent for new chats','runtime cli install authentication','#runtime-agent'],
  ['agent','Projects','Clone a repository or remove a local project','git add delete remove shared sharing revoke access','#setup-projects-title'],
  ['activity','Activity sources','Choose which agent activity appears in Space','watcher sessions history automatic telemetry','#runtime-source-mode'],
  ['activity','Activity interval','Activity · Advanced','watcher polling seconds frequency','#runtime-interval'],
  ['connectors','Connectors','Connect apps and manage access','magicpath github vercel google drive onedrive gmail slack notion calendar outlook telegram oauth polling permissions','#setup-connectors-title'],
  ['secrets','Secrets','Add, replace or remove environment values','env environment key token credentials api password','#setup-secrets-title'],
  ['commands','Commands','Run commands and view their results','jobs scheduled interval automation inbox output logs','#setup-commands-title'],
  ['server','Restart server','Apply saved changes','restart apply runtime','#setup-server-title'],
  ['server','Updates','Check for a newer version of Space','update version upgrade','#update-check'],
];

export function mountSetupSearch(root,openPanel,refreshToolbar){
  const results=root.querySelector('#setup-search-results');
  const content=root.querySelector('.setup-content');
  let query='';

  function render(){
    const words=query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const searching=words.length>0;
    content.classList.toggle('is-searching',searching);
    results.hidden=!searching;
    if(!searching){results.replaceChildren();return;}
    const matches=SETTINGS.filter(row=>words.every(word=>row.slice(0,4).join(' ').toLowerCase().includes(word)));
    results.innerHTML='<header class="setup-section-head"><h2>Search setup</h2>'
      +'<p role="status">'+(matches.length?matches.length+' '+(matches.length===1?'result':'results'):'No settings found')
      +' for “'+esc(query.trim())+'”</p></header>'
      +(matches.length?'<div class="setup-search-list">'+matches.map(row=>
        '<button type="button" class="setup-search-result" data-setup-result="'+SETTINGS.indexOf(row)+'">'
          +'<span><b>'+esc(row[1])+'</b><small>'+esc(row[2])+'</small></span><span aria-hidden="true">→</span>'
        +'</button>').join('')+'</div>':'<p class="setup-search-empty">Try “folders”, “secrets” or “restart”.</p>');
  }

  function clear(){query='';render();}
  results.addEventListener('click',event=>{
    const button=event.target.closest('[data-setup-result]');
    if(!button)return;
    const row=SETTINGS[Number(button.dataset.setupResult)];
    if(!row)return;
    clear();
    openPanel(row[0],{focus:true});
    refreshToolbar();
    const target=root.querySelector(row[4]);
    if(target){
      const details=target.closest('details');
      if(details)details.open=true;
      if(!target.disabled)target.focus({preventScroll:true});
      target.scrollIntoView({block:'nearest'});
    }
  });
  return {
    toolbar:{search:{placeholder:'Search setup…',getValue:()=>query,
      setValue(value){query=String(value??'');render();}}},
    clear,
  };
}
