/* Intent names used by the older feature fixtures. "projects" here means
   the List page; primary-section navigation is tested separately. */
export const PAGE_ROUTES={dashboard:'projects/overview',projects:'projects/list',
  graph:'projects/graph',tree:'projects/tree',sharing:'projects/sharing',time:'projects/timeline',
  agents:'agents/sessions',inbox:'inbox/items',setup:'setup/workspace',
  connectors:'setup/connectors',secrets:'setup/secrets',quirq:'setup/server/details'};
export const routeFor=id=>'#/'+(PAGE_ROUTES[id]||id);
export const projectPageId=id=>id==='projects'?'project-list':id;
export const projectPageSelector=id=>['dashboard','graph','tree'].includes(id)
  ?'[data-view-mode="'+(id==='tree'?'tree':'graph')+'"]':'[data-section-page="'+projectPageId(id)+'"]';
export async function openProjectPage(page,id){
  if(id==='dashboard')await page.evaluate(hash=>{location.hash=hash;},routeFor(id));
  else{
    if(!await page.locator('#section-nav[data-section="projects"] '+projectPageSelector(id)).isVisible())await page.locator('#tab-projects').click();
    await page.locator('#section-nav '+projectPageSelector(id)).click();
  }
  await page.waitForURL('**/'+routeFor(id));
  await page.locator('#section-nav '+projectPageSelector(id)+'[aria-current="page"]').waitFor();
}
export async function openProjectList(page){
  if(!await page.locator('#section-nav [data-section-page="project-list"]').isVisible())
    await page.locator('#tab-projects').click();
  await page.locator('#section-nav [data-section-page="project-list"]').click();
  await page.waitForURL('**/#/projects/list');
  await page.waitForFunction(()=>document.querySelector('#view-projects')?.classList.contains('is-active')
    &&document.querySelector('.topbar')?.dataset.toolbar==='search'
    &&document.querySelector('#view-search')?.placeholder==='Filter projects…'
    &&!document.querySelector('#view-search').disabled);
}
