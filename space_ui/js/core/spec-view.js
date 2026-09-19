/* specView(page): a registry view (core/registry.js contract) for one page
   of GET /api/ui ({module, id, route, tab, label, order, aliases, spec}).

   id is "<module>/<id>", the route and aliases are the spec's, the parent
   is the spec's tab and the section is the page's own. mount renders the
   spec's blocks into that section from one read (spec.read, through
   apiFetch), refresh re-reads, show starts the poll (a slotted interval
   every poll_s, only while shown) and hide stops it and closes the page's
   streams. search: true exposes the shell's page search (toolbar.js) over
   the rendered rows. A failed read renders the kit's alert with failText;
   a 404 whose code is module_disabled says the page is off in Setup and
   links to #/setup/modules. */
import {API_BASE,apiFetch,failText} from './api.js';
import {setSlottedInterval,clearSlottedInterval} from './store.js';
import {esc} from './ui.js';
import * as kit from './shadcn.js';
import {createRenderer} from './render.js';

export const MODULES_ROUTE='setup/modules';

export function specView(page){
  const spec=page.spec&&typeof page.spec==='object'?page.spec:{};
  const id=page.module+'/'+page.id;
  const section='spec-'+page.module+'-'+page.id;
  let root=null,renderer=null,alertBox=null,go=async()=>false;
  let query='',shown=false,loading=null,payload=null,generation=0;
  const search=spec.search?{
    placeholder:'Search this page…',
    label:'Search '+page.label,
    getValue:()=>query,
    setValue(value){query=String(value??'');renderer?.search(query);},
  }:null;

  function showFailure(res){
    if(!alertBox)return;
    const off=res.status===404&&res.code==='module_disabled';
    alertBox.hidden=false;
    alertBox.innerHTML=off
      ?kit.alert({icon:kit.icons.alert,title:esc(page.label)+' is off in Setup',
          description:esc(res.error||'This page belongs to a module that is switched off.')
            +' <a href="#/'+MODULES_ROUTE+'">Open Setup, Modules</a> to turn it on.'})
      :kit.alert({variant:'destructive',icon:kit.icons.alert,title:'Could not load '+esc(page.label),
          description:esc(failText(res))});
  }

  function load(){
    if(loading)return loading;
    loading=(async()=>{
      const rev=++generation;
      let data={};
      if(spec.read){
        const res=await apiFetch(API_BASE+spec.read);
        if(rev!==generation||!root||!renderer)return false;
        if(!res.ok){showFailure(res);return false;}
        data=res.data&&typeof res.data==='object'?res.data:{};
      }
      if(alertBox){alertBox.hidden=true;alertBox.innerHTML='';}
      payload=data;
      await renderer.render(payload);
      renderer.search(query);
      return true;
    })().finally(()=>{loading=null;});
    return loading;
  }

  return{
    id,route:page.route,aliases:[...(page.aliases||[])],label:page.label,order:page.order||0,
    nav:false,parent:page.tab,section,spec:true,module:page.module,
    toolbar:search?{search}:null,
    async mount(el,ctx){
      root=el;go=ctx.switchTo;
      el.classList.add('spec-view');
      el.innerHTML='<div class="spec-page"><header class="spec-head"><h1>'+esc(page.label)+'</h1></header>'
        +'<div class="spec-alert" data-slot="spec-alert" hidden></div><div class="spec-blocks"></div></div>';
      alertBox=el.querySelector('[data-slot="spec-alert"]');
      renderer=createRenderer(el.querySelector('.spec-blocks'),page,{switchTo:go,refresh:()=>load(),page});
      /* The first read is not awaited: the section shows at once with the
         block skeletons and fills as the read lands, like every other page. */
      load();
    },
    show(){
      shown=true;
      renderer?.show();
      if(spec.poll_s)setSlottedInterval('spec:'+id,()=>{if(shown)load();},Number(spec.poll_s)*1000);
    },
    hide(){
      shown=false;
      clearSlottedInterval('spec:'+id);
      renderer?.hide();
    },
    refresh(){return load();},
    destroy(){
      clearSlottedInterval('spec:'+id);
      renderer?.destroy();renderer=null;
      if(root)root.innerHTML='';
      root=null;alertBox=null;
    },
    get payload(){return payload;},
  };
}
