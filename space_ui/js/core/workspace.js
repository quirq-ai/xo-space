/* Workspace-wide rollups, in one request.

   The Files List needs a file and folder count per project. Asking each
   project for its tree would be one request per row; `.xo/space.json` — the
   same bounded, server-cached payload the Graph and Tree lenses read — already
   carries every mapped file as "<project>/<relative path>", so the whole
   column costs one fetch no matter how many projects there are.

   Lives in core/ so views can share the index contract without importing one another.
   New graphs carry each hub's index_counts, captured before the 400/project
   and 1500/workspace display limits. Those counts cover indexed files and
   their ancestor folders; capped means the scan itself was incomplete.
   Older graphs only have retained leaves, so their counts are lower bounds
   when display limits may have applied. Zero under an old workspace cap is
   unknown, never evidence of an empty project. */
import {API_BASE,apiFetch} from './api.js';

const validCounts=value=>value&&Number.isSafeInteger(value.files)&&value.files>=0
  &&Number.isSafeInteger(value.folders)&&value.folders>=0&&typeof value.capped==='boolean';

export async function workspaceCounts(){
  const res=await apiFetch(API_BASE+'/xo/space.json');
  if(!res.ok)return{ok:false,error:res.error,offline:res.offline,byProject:new Map(),
    totals:{projects:0,files:0,folders:0}};

  const byProject=new Map(),reported=new Map(),invalid=new Set();
  const legacyWorkspaceCap=(res.data.leaves||[]).length>=1500;
  const ensure=id=>{
    if(!byProject.has(id))byProject.set(id,{files:0,folders:0,dirs:new Set()});
    return byProject.get(id);
  };
  for(const hub of res.data.hubs||[]){
    const id=hub.id.replace(/^p_/,'');
    ensure(id);
    if(validCounts(hub.index_counts))reported.set(id,hub.index_counts);
    else if(hub.index_counts!==undefined)invalid.add(id);
  }
  for(const leaf of res.data.leaves||[]){
    const parts=String(leaf.path||'').split('/').filter(Boolean);
    if(parts.length<2)continue;
    const p=ensure(parts[0]);
    p.files++;
    /* every ancestor directory of this file, deduped */
    for(let i=1;i<parts.length-1;i++)p.dirs.add(parts.slice(1,i+1).join('/'));
  }
  let files=0,folders=0,totalsCapped=res.data.meta?.index_counts_complete===false;
  for(const [id,p] of byProject){
    const count=reported.get(id);
    if(count){
      p.files=count.files;p.folders=count.folders;p.capped=count.capped;p.known=true;
    }else{
      p.folders=p.dirs.size;
      /* Older documents cannot identify which projects lost leaves in the
         global trim. Positive counts remain useful lower bounds. */
      p.capped=legacyWorkspaceCap||p.files>=400;
      p.known=!invalid.has(id)&&(p.files>0||!legacyWorkspaceCap);
    }
    delete p.dirs;
    totalsCapped=totalsCapped||p.capped||!p.known;
    files+=p.files;folders+=p.folders;
  }
  return{ok:true,byProject,
    totals:{projects:byProject.size,files,folders},
    totalsCapped};
}
