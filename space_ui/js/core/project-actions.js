/* Cross-page actions go through the registry. Only a completed, still-current
   navigation may open a form; a slow mount must not overwrite a later choice. */
export async function openProjectAdd(switchTo){
  if((await switchTo('setup/projects'))===true&&location.hash==='#/setup/projects'){
    dispatchEvent(new CustomEvent('space:add-project'));
  }
}
