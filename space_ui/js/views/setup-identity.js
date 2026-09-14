/* Setup identity is read-only. A stored credential is not a verified account;
   only the service's successful identity checks produce Connected labels. */
import {apiFetch} from '../core/api.js';

const esc=value=>String(value??'').replace(/[&<>"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
const text=value=>typeof value==='string'?value.trim():'';
const row=(label,value)=>'<div><dt>'+esc(label)+'</dt><dd>'+esc(value)+'</dd></div>';

export function mountIdentity(el){
  let revision=0,refreshing=false,refreshQueued=false;
  function render(data=null,{loading=false}={}){
    const space=data?.space;
    const spaceId=space?.status==='configured'?text(space.id):'';
    const spaceValue=loading?'Checking…':spaceId||
      (space?.status==='not_configured'?'Not configured':'Unavailable');
    function connection(kind,title){
      const state=data?.[kind];
      const identity=text(state?.[kind==='xo'?'user_id':'username']);
      const connected=state?.status==='connected'&&Boolean(identity);
      const status=loading?'Checking…':connected?'Connected'
        :state?.status==='not_configured'?'Not configured'
          :state?.status==='rejected'?'Authentication rejected':'Unable to verify';
      const tone=connected?'good':!loading&&state?.status==='rejected'?'error':'muted';
      let detail=loading?'':connected?(kind==='xo'?'User ID: '+identity:'@'+identity)
        :state?.status==='not_configured'?(kind==='xo'?'No XO account credential is configured.':'No GitHub credential is configured for project backups and sync.')
          :state?.status==='rejected'?'Check the saved credential.':'Refresh status to check again.';
      if(kind==='github'&&state?.source==='env')detail+=(detail?' · ':'')+'Environment (GITHUB_PAT)';
      else if(kind==='github'&&state?.source==='connector')detail+=(detail?' · ':'')+'Saved connection';
      return '<div class="setup-identity-connection" data-setup-identity="'+kind+'">'
        +'<div><h4>'+title+'</h4><span class="setup-identity-status is-'+tone+'" role="status">'+status+'</span></div>'
        +'<p class="setup-identity-detail">'+esc(detail)+'</p></div>';
    }
    el.innerHTML='<div class="setup-card-head"><h3>Workspace identity</h3></div>'
      +'<div class="setup-identity-body"><dl class="setup-identity-space">'
      +row('Space ID',spaceValue)
      +(text(space?.label)?row('Workspace name',space.label):'')
      +(text(space?.owner)?row('Workspace owner',space.owner):'')
      +'</dl>'+connection('xo','XO account')+connection('github','GitHub')
      +'</div>';
  }
  async function refresh(){
    const mine=++revision;
    render(null,{loading:true});
    if(refreshing){refreshQueued=true;return;}
    refreshing=true;
    const res=await apiFetch('/space/setup/status');
    refreshing=false;
    if(mine===revision)render(res.ok&&res.data&&typeof res.data==='object'?res.data:null);
    // apiFetch shares concurrent GETs. A refresh after a credential change
    // needs a new request once the older check finishes, not its old result.
    if(refreshQueued){refreshQueued=false;await refresh();}
  }
  render(null,{loading:true});
  return {refresh};
}
