/* Workspace branding is shared by the shell and Setup. A late read must
   never replace a newer save. Names are always inserted as text. */
import {API_BASE,apiFetch,withPageQuery} from './api.js';

export const DEFAULT_BRANDING=Object.freeze({name:'Space',logo_url:null});
let current=DEFAULT_BRANDING,revision=0;

export function brandingLogoURL(path){
  // Only the workspace's own raster-image endpoint may supply a saved logo.
  return typeof path==='string'&&/^\/space\/branding\/logo(?:\?v=[a-f0-9]+)?$/.test(path)
    ?withPageQuery(API_BASE+path):null;
}

export function defaultBrandMark(){
  return document.querySelector('.brand .mark svg')?.cloneNode(true)||document.createTextNode('XO');
}

function applyBranding(data){
  current={name:typeof data?.name==='string'&&data.name.trim()?data.name:DEFAULT_BRANDING.name,
    logo_url:brandingLogoURL(data?.logo_url)?data.logo_url:null};
  const name=document.querySelector('.brand b');
  if(name){name.textContent=current.name;name.title=current.name;}
  const mark=document.querySelector('.brand .mark');
  if(mark){
    mark.querySelector('img')?.remove();
    const svg=mark.querySelector('svg'),url=brandingLogoURL(current.logo_url);
    if(svg)svg.style.display=url?'none':'';
    mark.classList.toggle('has-custom-logo',Boolean(url));
    if(url){
      const img=document.createElement('img');img.alt='';img.src=url;
      img.addEventListener('error',()=>{
        if(!mark.contains(img))return; // Ignore a replaced image's late failure.
        img.remove();if(svg)svg.style.display='';mark.classList.remove('has-custom-logo');
      });
      mark.append(img);
    }
  }
  document.title=current.name==='Space'?'XO Space':current.name;
}

export async function loadBranding(){
  const mine=revision;
  const res=await apiFetch(API_BASE+'/space/branding');
  if(mine!==revision)return{ok:true,data:current};
  if(res.ok)applyBranding(res.data);
  return res;
}

export async function saveBranding(body){
  ++revision;
  const res=await apiFetch(API_BASE+'/space/branding',{method:'PUT',body});
  ++revision;
  if(res.ok)applyBranding(res.data);
  return res;
}
