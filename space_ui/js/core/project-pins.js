/* Browser-local project preferences shared by Data and Manage. */
import {API_BASE} from './api.js';

const pinKey='space.projects.pins.v1:'+String(API_BASE||location.origin||'local');
const subscribers=new Set();
const validId=id=>typeof id==='string'&&id.length<=200;
let pinned=new Set();

function decode(value){
  const saved=JSON.parse(value||'[]');
  if(!Array.isArray(saved))return null;
  return new Set(saved.filter(validId).slice(0,1000));
}

try{pinned=decode(localStorage.getItem(pinKey))||new Set();}
catch{/* Storage is optional; pins still work for this visit. */}

function publish(source,persisted){
  for(const callback of [...subscribers]){
    try{callback({source,persisted});}
    catch(error){console.error('Project pin listener failed:',error);}
  }
}

export function isProjectPinned(id){return pinned.has(id);}

export function toggleProjectPin(id){
  if(!validId(id))return {pinned:false,persisted:false};
  const next=!pinned.has(id);
  if(next)pinned.add(id);else pinned.delete(id);
  let persisted=true;
  try{localStorage.setItem(pinKey,JSON.stringify([...pinned]));}
  catch{persisted=false;}
  publish('local',persisted);
  return {pinned:next,persisted};
}

/* Callbacks reread IDs; a storage event can change several projects at once. */
export function subscribeProjectPins(callback){
  subscribers.add(callback);
  return ()=>subscribers.delete(callback);
}

addEventListener('storage',event=>{
  if(event.key!==pinKey&&event.key!==null)return;
  try{
    if(event.storageArea&&event.storageArea!==localStorage)return;
    const next=decode(event.key===null?null:event.newValue);
    if(!next)return;
    pinned=next;
  }catch{return;}
  publish('storage',true);
});
