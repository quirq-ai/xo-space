/* Cross-page actions go through the registry. Only a completed, still-current
   navigation may open a form; a slow mount must not overwrite a later choice. */
export async function openProjectAdd(switchTo){
  if((await switchTo('projects/manage'))===true&&location.hash==='#/projects/manage'){
    dispatchEvent(new CustomEvent('space:add-project'));
  }
}

/* Historical callers used a Setup panel event before management had its own
   page. Register once in the shell so it also works before Setup mounts. */
export function initProjectActions(switchTo){
  addEventListener('space:setup-section',event=>{
    if(event.detail?.panel==='projects')switchTo('projects/manage');
  });
}

export async function openProjectActivity(switchTo,projectId){
  const id=typeof projectId==='string'?projectId.trim():'';
  if(!id)return;
  if((await switchTo('inbox/activity'))===true&&location.hash==='#/inbox/activity'){
    dispatchEvent(new CustomEvent('space:activity-project',{detail:{project_id:id}}));
  }
}
