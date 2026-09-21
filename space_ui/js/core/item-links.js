/* What Open in Space does for an Inbox item's link (the fact's `link`, as
   the feeder wrote it): a file link previews it in the Data list; a sharing
   item lands on Sharing with its project selected; a view link jumps there;
   a bare project link lands on the Data list. Used by the item page.
   switchTo is not awaited: its tab and event side effects are synchronous,
   and the previewer closes on any non-Data view, so the switch must happen
   before the preview event. Unknown view ids are ignored by the registry
   itself. Returns whether the link led anywhere. */
const PROJ_RE=/^[A-Za-z0-9_:.\-]{1,200}$/;
/* A hand-edited fact.json reaches the page as-is; never hand the previewer a
   path the file API would refuse anyway. */
export const safePath=p=>typeof p==='string'&&p.length>0&&p.length<=500
  &&!p.startsWith('/')&&!p.includes('\\')&&!p.split('/').includes('..');
export const hasLink=it=>!!it&&!!it.link&&typeof it.link==='object'&&!!(it.link.view||it.link.project);

export function openItemLink(switchTo,it){
  const l=it&&it.link;
  if(!l||typeof l!=='object')return false;
  const project=typeof l.project==='string'&&PROJ_RE.test(l.project)?l.project:'';
  if(project&&safePath(l.path)){
    switchTo('projects/data/list');
    dispatchEvent(new CustomEvent('space:preview-file',{detail:{project,path:l.path}}));
    return true;
  }
  /* Sharing events open Sharing on their project, where the fetched commits
     and Apply live. Facts stored before the feeder linked there still carry
     view "projects"; their sharing.* kind routes them. */
  if(l.view==='sharing'||String(it.kind||'').startsWith('sharing.')){
    const target=project||(typeof it.project_id==='string'&&PROJ_RE.test(it.project_id)?it.project_id:'');
    switchTo('inbox/sharing');
    if(target)dispatchEvent(new CustomEvent('space:sharing-focus',{detail:target}));
    return true;
  }
  if(typeof l.view==='string'&&l.view){switchTo(l.view==='projects'?'projects/data/list':l.view);return true;}
  if(project){switchTo('projects/data/list');return true;}
  return false;
}
