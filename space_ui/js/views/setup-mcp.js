/* MCP credentials are displayed only when issued and kept in memory. Status
   reads never contain a token; a page reload requires a new token to copy it. */
import {apiFetch} from '../core/api.js';
import {toast} from '../core/ui.js';

export function mountMcpServer(el){
  let status=null,token='',writing=false,reading=false,revision=0,refreshQueued=false,pendingEnabled=null;
  el.innerHTML=`<div class="setup-card-head"><h3 id="setup-mcp-title">MCP server</h3><i id="mcp-status" role="status">Not checked</i></div>
    <div class="setup-mcp-body">
      <div class="setup-check-row"><label class="setup-switch" for="mcp-enabled"><input id="mcp-enabled" type="checkbox" role="switch" aria-labelledby="mcp-label" aria-describedby="mcp-description" disabled><span></span></label>
        <div><b id="mcp-label">Enable MCP server</b><p id="mcp-description">Let MCP apps read this Space’s project documents, tasks, and Inbox. Changes apply immediately.</p></div></div>
      <div id="mcp-error" class="setup-form-error" role="alert" hidden></div>
      <button id="mcp-retry" class="setup-secondary" type="button" hidden>Retry status</button>
      <div id="mcp-connection" hidden>
        <label for="mcp-url">Server URL <span>Streamable HTTP</span></label>
        <div class="setup-mcp-field"><input id="mcp-url" type="text" readonly spellcheck="false"><button id="mcp-copy-url" class="setup-secondary" type="button">Copy URL</button></div>
        <p>Clients need a reachable URL. Use HTTPS for remote connections.</p>
        <label for="mcp-token">Access token</label>
        <div class="setup-mcp-field"><input id="mcp-token" type="password" readonly autocomplete="off" spellcheck="false" placeholder="Saved token is hidden"><button id="mcp-show-token" class="setup-secondary" type="button">Show</button><button id="mcp-copy-token" class="setup-secondary" type="button">Copy token</button></div>
        <p id="mcp-token-hint"></p>
        <div class="setup-actions"><button id="mcp-copy-config" class="setup-primary" type="button">Copy client config</button><button id="mcp-rotate" class="setup-secondary" type="button">Generate new token</button></div>
        <p>Generating a new token or turning the server off disconnects existing clients.</p>
      </div>
      <p id="mcp-disabled-note">Off by default. Turn it on to connect an MCP app.</p>
    </div>`;
  const find=id=>el.querySelector('#'+id);
  const enabled=find('mcp-enabled'),badge=find('mcp-status'),connection=find('mcp-connection');
  const error=find('mcp-error'),retry=find('mcp-retry'),urlInput=find('mcp-url'),tokenInput=find('mcp-token');

  function setError(message=''){
    error.textContent=message;error.hidden=!message;
  }
  function render(){
    const on=status?.enabled===true,unavailable=!status,busy=writing||reading;
    enabled.checked=pendingEnabled??on;enabled.disabled=busy||unavailable;
    badge.textContent=writing?'Saving…':reading?'Checking…':unavailable?'Unavailable':on?'On':'Off';
    badge.className=on&&!busy?'is-good':'';
    connection.hidden=!on;
    find('mcp-disabled-note').hidden=on||unavailable||busy;
    retry.hidden=!unavailable||busy;
    // Match the same-origin API routing; never copy page session query values.
    urlInput.value=on?new URL(status.endpoint_path,location.origin).href:'';
    tokenInput.value=on?token:'';
    if(!token){tokenInput.type='password';find('mcp-show-token').textContent='Show';}
    find('mcp-token-hint').textContent=token
      ?'Copy this token now. It is only shown when issued and will disappear when you reload the page.'
      :'The saved token cannot be shown again. Use your copied token, or generate a new one.';
    for(const id of ['mcp-copy-url','mcp-copy-config','mcp-rotate'])find(id).disabled=busy||!on;
    for(const id of ['mcp-show-token','mcp-copy-token'])find(id).disabled=busy||!on||!token;
  }
  function accept(data){
    status=data;
    if(!status.enabled)token='';
  }
  async function refresh(){
    if(writing||reading){refreshQueued=true;return;}
    const mine=++revision;reading=true;render();
    const res=await apiFetch('/api/mcp-server');
    if(mine!==revision)return;
    reading=false;
    if(res.ok){accept(res.data);setError();}
    else{status=null;token='';setError(res.error||'Could not check the MCP server.');}
    render();
    if(refreshQueued){refreshQueued=false;await refresh();}
  }
  async function write({rotate=false}={}){
    if(writing||reading||!status)return;
    const next=enabled.checked;
    pendingEnabled=rotate?null:next;
    writing=true;revision++;setError();render();
    const res=await apiFetch(rotate?'/api/mcp-server/rotate-token':'/api/mcp-server',
      rotate?{method:'POST'}:{method:'PUT',body:{enabled:next}});
    writing=false;pendingEnabled=null;
    if(res.ok){
      accept(res.data);
      if(typeof res.data.token==='string'){token=res.data.token;tokenInput.type='password';find('mcp-show-token').textContent='Show';}
      toast(rotate?'New MCP token generated':status.enabled?'MCP server enabled':'MCP server disabled');
    }else{
      setError(res.error||'Could not update the MCP server.');
    }
    render();
    if(refreshQueued){refreshQueued=false;await refresh();}
  }
  async function copy(value,field,message){
    try{await navigator.clipboard.writeText(value);toast(message);}
    catch(_error){
      if(field){field.focus();field.select();toast('Copy the selected value');}
      else setError('Clipboard access is unavailable. Add the server URL and Bearer access token in your MCP app.');
    }
  }
  enabled.addEventListener('change',()=>write());
  retry.addEventListener('click',refresh);
  find('mcp-rotate').addEventListener('click',()=>write({rotate:true}));
  find('mcp-show-token').addEventListener('click',()=>{
    tokenInput.type=tokenInput.type==='password'?'text':'password';
    find('mcp-show-token').textContent=tokenInput.type==='password'?'Show':'Hide';
  });
  find('mcp-copy-url').addEventListener('click',()=>copy(urlInput.value,urlInput,'MCP server URL copied'));
  find('mcp-copy-token').addEventListener('click',()=>copy(token,tokenInput,'MCP access token copied'));
  find('mcp-copy-config').addEventListener('click',()=>{
    const config={mcpServers:{space:{type:'http',url:urlInput.value,headers:{Authorization:'Bearer '+(token||'YOUR_ACCESS_TOKEN')}}}};
    copy(JSON.stringify(config,null,2),null,token?'MCP client config copied':'Config copied — replace YOUR_ACCESS_TOKEN');
  });
  return {refresh};
}
