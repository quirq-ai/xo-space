/* Workspace preference, independent of branding and browser-local storage. */
import {API_BASE,apiFetch} from './api.js';

export const DEFAULT_THEME='space';
export const THEMES=Object.freeze(['space','quirq','midnight','graphite','linen']);
let current=DEFAULT_THEME,revision=0;

function applyTheme(theme){
  current=theme;
  if(document.documentElement.dataset.theme===theme)return;
  document.documentElement.dataset.theme=theme;
  dispatchEvent(new CustomEvent('space:theme',{detail:{theme}}));
}

function validated(res){
  if(res.ok&&!THEMES.includes(res.data?.theme))return{ok:false,error:'The saved theme is not supported. Refresh to try again.'};
  return res;
}

export async function loadTheme(){
  const mine=revision;
  const res=validated(await apiFetch(API_BASE+'/space/theme'));
  if(mine!==revision)return{ok:true,data:{theme:current}};
  if(res.ok)applyTheme(res.data.theme);
  return res;
}

export async function saveTheme(theme){
  ++revision;
  const res=validated(await apiFetch(API_BASE+'/space/theme',{method:'PUT',body:{theme}}));
  ++revision;
  if(res.ok)applyTheme(res.data.theme);
  return res;
}
