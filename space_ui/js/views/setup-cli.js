/* CLI access has its own credential and switch. Issued tokens remain in this
   page's memory; configuration commands never include credentials. */
import {apiFetch,withPageQuery} from '../core/api.js';
import {toast} from '../core/ui.js';

const shellQuote=value=>"'"+String(value).replace(/'/g,"'\"'\"'")+"'";

export function mountCliAccess(el){
  let status=null,token='',writing=false,reading=false,revision=0,refreshQueued=false,pendingEnabled=null;
  el.innerHTML=`<div class="setup-card-head"><h3 id="setup-cli-title">Command line</h3><i id="cli-status" role="status">Not checked</i></div>
    <div class="setup-cli-body">
      <div class="setup-check-row"><label class="setup-switch" for="cli-enabled"><input id="cli-enabled" type="checkbox" role="switch" aria-labelledby="cli-label" aria-describedby="cli-description" disabled><span></span></label>
        <div><b id="cli-label">Enable CLI access</b><p id="cli-description">Read this Space’s project documents, tasks, and Inbox from your terminal. Changes apply immediately, independently of MCP.</p></div></div>
      <div id="cli-error" class="setup-form-error" role="alert" hidden></div>
      <button id="cli-retry" class="setup-secondary" type="button" hidden>Retry status</button>
      <div id="cli-connection" hidden>
        <div class="setup-actions"><a id="cli-download" class="setup-secondary" download="space">Download CLI</a></div>
        <p>Requires Python 3.10+. Open a terminal in the download folder and run the setup command below. Enabling access does not install a shell command.</p>
        <label for="cli-command">Set up this Space</label>
        <div class="setup-cli-field"><input id="cli-command" type="text" readonly spellcheck="false"><button id="cli-copy-command" class="setup-primary" type="button">Copy setup command</button></div>
        <p>The command prompts for your access token with input hidden. Space must be running; use a reachable HTTPS URL for remote connections.</p>
        <label for="cli-token">Access token</label>
        <div class="setup-cli-field"><input id="cli-token" type="password" readonly autocomplete="off" spellcheck="false" placeholder="Saved token is hidden"><button id="cli-show-token" class="setup-secondary" type="button">Show</button><button id="cli-copy-token" class="setup-secondary" type="button">Copy token</button></div>
        <p id="cli-token-hint"></p>
        <div class="setup-actions"><button id="cli-rotate" class="setup-secondary" type="button">Generate new token</button></div>
        <p>Generating a new token or turning CLI access off revokes existing CLI access. MCP connections are unchanged.</p>
        <details class="setup-details setup-cli-examples"><summary>Example commands</summary><pre>python3 ./space projects
python3 ./space document PROJECT PLAN.md
python3 ./space todos PROJECT
python3 ./space inbox --json</pre></details>
      </div>
      <p id="cli-disabled-note">Off by default. Turn it on to download and connect the CLI.</p>
    </div>`;
  const find=id=>el.querySelector('#'+id);
  const enabled=find('cli-enabled'),badge=find('cli-status'),connection=find('cli-connection');
  const error=find('cli-error'),retry=find('cli-retry'),commandInput=find('cli-command'),tokenInput=find('cli-token'),download=find('cli-download');

  function setError(message=''){
    error.textContent=message;error.hidden=!message;
  }
  function render(){
    const on=status?.enabled===true,unavailable=!status,busy=writing||reading;
    enabled.checked=pendingEnabled??on;enabled.disabled=busy||unavailable;
    badge.textContent=writing?'Saving…':reading?'Checking…':unavailable?'Unavailable':on?'On':'Off';
    badge.className=on&&!busy?'is-good':'';
    connection.hidden=!on;
    find('cli-disabled-note').hidden=on||unavailable||busy;
    retry.hidden=!unavailable||busy;
    commandInput.value=on?'python3 ./space configure --url '+shellQuote(location.origin):'';
    download.setAttribute('aria-disabled',String(busy||!on));
    if(on&&!busy){
      const path=new URL(status.download_path,location.origin);
      if(path.origin===location.origin)download.href=withPageQuery(path.pathname+path.search);
      else download.removeAttribute('href');
    }else download.removeAttribute('href');
    tokenInput.value=on?token:'';
    if(!token){tokenInput.type='password';find('cli-show-token').textContent='Show';}
    find('cli-token-hint').textContent=token
      ?'Copy this token now. It is only shown when issued and will disappear when you reload the page.'
      :'The saved token cannot be shown again. Use your copied token, or generate a new one.';
    for(const id of ['cli-copy-command','cli-rotate'])find(id).disabled=busy||!on;
    for(const id of ['cli-show-token','cli-copy-token'])find(id).disabled=busy||!on||!token;
  }
  function accept(data){
    status=data;
    if(!status.enabled)token='';
  }
  async function refresh(){
    if(writing||reading){refreshQueued=true;return;}
    const mine=++revision;reading=true;render();
    const res=await apiFetch('/api/cli-access');
    if(mine!==revision)return;
    reading=false;
    if(res.ok){accept(res.data);setError();}
    else{status=null;token='';setError(res.error||'Could not check CLI access.');}
    render();
    if(refreshQueued){refreshQueued=false;await refresh();}
  }
  async function write({rotate=false}={}){
    if(writing||reading||!status)return;
    const next=enabled.checked;
    pendingEnabled=rotate?null:next;
    writing=true;revision++;setError();render();
    const res=await apiFetch(rotate?'/api/cli-access/rotate-token':'/api/cli-access',
      rotate?{method:'POST'}:{method:'PUT',body:{enabled:next}});
    writing=false;pendingEnabled=null;
    if(res.ok){
      accept(res.data);
      if(typeof res.data.token==='string'){token=res.data.token;tokenInput.type='password';find('cli-show-token').textContent='Show';}
      toast(rotate?'New CLI token generated':status.enabled?'CLI access enabled':'CLI access disabled');
    }else setError(res.error||'Could not update CLI access.');
    render();
    if(refreshQueued){refreshQueued=false;await refresh();}
  }
  async function copy(value,field,message){
    try{await navigator.clipboard.writeText(value);toast(message);}
    catch(_error){field.focus();field.select();toast('Copy the selected value');}
  }
  enabled.addEventListener('change',()=>write());
  retry.addEventListener('click',refresh);
  download.addEventListener('click',event=>{if(writing||reading||!status?.enabled)event.preventDefault();});
  find('cli-rotate').addEventListener('click',()=>write({rotate:true}));
  find('cli-show-token').addEventListener('click',()=>{
    tokenInput.type=tokenInput.type==='password'?'text':'password';
    find('cli-show-token').textContent=tokenInput.type==='password'?'Show':'Hide';
  });
  find('cli-copy-command').addEventListener('click',()=>copy(commandInput.value,commandInput,'CLI setup command copied'));
  find('cli-copy-token').addEventListener('click',()=>copy(token,tokenInput,'CLI access token copied'));
  return {refresh};
}
