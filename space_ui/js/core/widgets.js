/* Widget blocks: {"type":"widget","widget":"graph","data":"graph"} loads a
   module's own drawing code from GET /space/modules/<module>/ui/<widget>.js
   (routers/kernel.py serves only that folder) on the block's first show.

   The widget module exports:
     mount(el, ctx)   draw into el once; ctx = {apiFetch, kit, switchTo,
                      refresh, page, data}
     update(data)     the page re-read; data is the block's data expression
     destroy()        the page is leaving or the module switched off
   The shell keeps no other contract with it: no fetch, poll or navigation
   of its own is needed, the spec view feeds it. An unknown widget (no
   file, a load error, a missing mount) renders an alert naming it. */
import {API_BASE,apiFetch} from './api.js';
import {esc} from './ui.js';
import * as kit from './shadcn.js';

const NAME=/^[a-z][a-z0-9-]*$/;

export function widgetUrl(module,widget){
  return API_BASE+'/space/modules/'+encodeURIComponent(module)+'/ui/'+encodeURIComponent(widget)+'.js';
}

/* mountWidget(host, block, ctx) returns {update(data), destroy()}; the
   import happens on the first update (the first show), not at render. */
export function mountWidget(host,block,ctx){
  const name=String(block.widget||'');
  let loading=null,widget=null,dead=false,pending=undefined;
  const fail=reason=>{
    host.innerHTML=kit.alert({variant:'destructive',title:'Widget '+esc(name||'(unnamed)')+' is unavailable',
      description:esc(reason)});
  };
  async function load(){
    if(!NAME.test(name)){fail('The spec names no valid widget.');return null;}
    let mod;
    try{mod=await import(widgetUrl(ctx.page.module,name));}
    catch(err){fail('modules/'+ctx.page.module+'/ui/'+name+'.js did not load: '+(err?.message||err));return null;}
    if(typeof mod.mount!=='function'){fail('modules/'+ctx.page.module+'/ui/'+name+'.js exports no mount(el, ctx).');return null;}
    if(dead)return null;
    host.innerHTML='';
    const el=document.createElement('div');
    el.className='spec-widget';
    if(block.height)el.style.minHeight=block.height;
    host.appendChild(el);
    try{
      await mod.mount(el,{apiFetch,kit,switchTo:ctx.switchTo,refresh:ctx.refresh,page:ctx.page,data:pending});
    }catch(err){fail('mount failed: '+(err?.message||err));return null;}
    return mod;
  }
  return{
    update(data){
      pending=data;
      if(dead)return;
      if(!loading)loading=load().then(mod=>{widget=mod;return mod;});
      loading.then(mod=>{
        if(!mod||dead||typeof mod.update!=='function')return;
        try{mod.update(pending);}catch(err){console.error('widget "'+name+'" update failed:',err);}
      });
    },
    destroy(){
      dead=true;
      if(widget&&typeof widget.destroy==='function'){
        try{widget.destroy();}catch(err){console.error('widget "'+name+'" destroy failed:',err);}
      }
      widget=null;
    },
  };
}
