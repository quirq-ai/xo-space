/* A project's GitHub mirror. Each mounted card keeps its own filter and DOM;
   only an explicit issue Refresh asks the server to poll GitHub immediately. */
import {API_BASE,apiFetch,failText} from './api.js';
import {esc,rel} from './ui.js';

const STATES=[['open','Open'],['closed','Closed'],['all','All']];
const EMPTY={
  no_remote:'No github.com remote. Add a GitHub origin to include this project’s issues.',
  never_polled:'Not polled yet. Refresh asks the server to check GitHub now.',
  issues_disabled:'Issues are turned off for this repository on GitHub.',
  empty:'No open issues.',
};
const validData=(data,id)=>data?.project_id===id&&Array.isArray(data.issues)
  &&['ok','empty','never_polled','issues_disabled','no_remote','error'].includes(data.state)
  &&data.issues.every(issue=>issue&&typeof issue==='object'&&!Array.isArray(issue));
const list=value=>Array.isArray(value)?value:[];
const count=(data,state)=>state==='all'?data.issues.length:data.issues.filter(issue=>issue.state===state).length;
function issueLink(value){
  try{const url=new URL(String(value||''));
    return url.protocol==='https:'&&url.hostname==='github.com'&&!url.username&&!url.password&&!url.port?url.href:null;
  }catch{return null;}
}
function issueRow(issue){
  const href=issueLink(issue.url),tag=href?'a':'div';
  const labels=list(issue.labels).slice(0,3).map(label=>'<span class="iss-label">'+esc(label)+'</span>').join('');
  const who=list(issue.assignees).map(assignee=>String(assignee?.login||'')).filter(Boolean).join(', ');
  return '<'+tag+' class="iss-row'+(issue.state==='closed'?' is-closed':'')+'"'
    +(href?' href="'+esc(href)+'" target="_blank" rel="noopener noreferrer"':'')
    +' title="'+esc((issue.title||'')+(who?' — '+who:''))+'">'
    +'<span class="iss-dot" aria-hidden="true"></span><span class="iss-num">#'+esc(issue.number||'?')+'</span>'
    +'<span class="iss-title">'+esc(issue.title||'(untitled)')+'</span><span class="iss-chips">'+labels
    +(issue.in_progress?'<span class="iss-work is-active">in progress</span>':issue.adopted?'<span class="iss-work">tracked</span>':'')
    +(who?'<span class="iss-assignees">'+esc(who)+'</span>':'')+'</span>'
    +'<span class="iss-when">'+esc(rel(issue.updated_at))+'</span></'+tag+'>';
}

export function createProjectIssues({projectId,onData=()=>{},request=apiFetch,timeoutMs=12000}){
  let data=null,state='open',query='',pending=null,disposed=false,readController=null,generation=0,forcing=false;
  const element=document.createElement('section');element.className='project-issues';
  element.setAttribute('aria-label','Issues for '+projectId);
  element.innerHTML='<header class="iss-heading"><h3>Issues</h3>'
    +'<button class="setup-secondary" data-iss-refresh type="button" title="Ask the server to poll GitHub now">Refresh issues</button></header>'
    +'<div class="iss-meta"></div><div class="iss-head">'
    +'<input class="iss-q" type="search" placeholder="Filter issues…" autocomplete="off" spellcheck="false" aria-label="Filter issues">'
    +'<div class="iss-states" role="group" aria-label="Issue state">'
    +STATES.map(([key,label])=>'<button type="button" data-iss-state="'+key+'" aria-pressed="'+(key===state)+'">'+label+' 0</button>').join('')
    +'</div></div><p class="iss-status" role="status" hidden></p><div class="iss-list"></div>';
  const $=selector=>element.querySelector(selector);
  const refreshButton=$('[data-iss-refresh]'),status=$('.iss-status'),rows=$('.iss-list'),meta=$('.iss-meta');
  function setStatus(message,error=false){status.textContent=message;status.hidden=!message;status.classList.toggle('is-error',error);}
  function paintRows(){
    if(!data){rows.innerHTML='';return;}
    let html='';
    if(data.state!=='ok'&&data.state!=='empty'){
      if(data.state==='error'){
        const error=data.error||{};
        html='<p class="iss-note is-error">Last poll failed: '+esc(error.message||error.kind||'unknown')
          +(error.at?' <span>'+esc(rel(error.at))+'</span>':'')+'</p>';
      }else html='<p class="iss-note">'+esc(EMPTY[data.state]||'No issues.')+'</p>';
    }else{
      const q=query.trim().toLowerCase();
      const filtered=data.issues.filter(issue=>(state==='all'||issue.state===state)&&(!q
        ||String(issue.title||'').toLowerCase().includes(q)||('#'+issue.number).includes(q)
        ||list(issue.labels).some(label=>String(label).toLowerCase().includes(q))
        ||list(issue.assignees).some(assignee=>String(assignee?.login||'').toLowerCase().includes(q))));
      html=filtered.length?filtered.map(issueRow).join(''):'<p class="iss-note">'+(q?'Nothing matches “'+esc(query.trim())+'”.'
        :state==='closed'?'No closed issues recorded. Space keeps an issue once it watches it close; issues closed before it started watching stay on GitHub.'
          :state==='all'?'No issues recorded.':'No open issues.')+'</p>';
    }
    // Preserve a focused issue link when a refresh returns identical content.
    if(rows.innerHTML!==html)rows.innerHTML=html;
  }
  function paintData(){
    meta.innerHTML=(data.repo?'<b>'+esc(data.repo)+'</b>':'')+'<span>'+count(data,'open')+' open'
      +(data.tracked?' · '+esc(data.tracked)+' tracked':'')+'</span>'
      +(data.fetched_at?'<span title="'+esc(data.fetched_at)+'">checked '+esc(rel(data.fetched_at))+'</span>':'<span>never checked</span>');
    for(const [key,label] of STATES){
      const button=$('[data-iss-state="'+key+'"]');button.textContent=label+' '+count(data,key);
      button.setAttribute('aria-pressed',String(state===key));
    }
    paintRows();
  }
  function load({force=false,refresh=false}={}){
    if(disposed)return Promise.resolve();
    if(pending&&(!force||forcing))return pending;
    if(data&&!force&&!refresh)return Promise.resolve(data);
    // A deliberate poll supersedes an older mirror read. Each request owns
    // its AbortSignal, so it cannot cancel another card's or view's request.
    const mine=++generation;readController?.abort();forcing=force;
    refreshButton.disabled=force;element.setAttribute('aria-busy','true');
    setStatus(force?'Asking GitHub…':'Loading issues…');
    readController=new AbortController();const controller=readController;
    let timer;
    const timeout=new Promise(resolve=>{timer=setTimeout(()=>{
      controller.abort();resolve({ok:false,error:'Issues took too long to load. Try Refresh issues.'});
    },timeoutMs);});
    pending=(async()=>{
      try{
        const response=await Promise.race([
          request(API_BASE+'/api/xo-projects/'+encodeURIComponent(projectId)+'/github/issues'+(force?'?refresh=1':''),{signal:controller.signal}),timeout,
        ]);
        if(disposed||mine!==generation)return;
        if(response.ok&&validData(response.data,projectId)){
          data=response.data;setStatus('');paintData();onData(data);return data;
        }
        setStatus(response.ok?'Issues could not be read. Try Refresh issues.':failText(response),true);
      }catch{
        if(!disposed&&mine===generation)setStatus('Issues could not be read. Try Refresh issues.',true);
      }finally{
        clearTimeout(timer);
        if(mine===generation){pending=null;readController=null;forcing=false;
          if(!disposed){refreshButton.disabled=false;element.removeAttribute('aria-busy');}}
      }
    })();
    return pending;
  }
  $('.iss-q').addEventListener('input',event=>{query=event.target.value;paintRows();});
  element.addEventListener('click',event=>{
    const button=event.target.closest('[data-iss-state]');
    if(button){state=button.dataset.issState;for(const node of element.querySelectorAll('[data-iss-state]'))node.setAttribute('aria-pressed',String(node===button));paintRows();}
    if(event.target.closest('[data-iss-refresh]'))load({force:true});
  });
  return {element,load,destroy(){disposed=true;readController?.abort();element.remove();}};
}
