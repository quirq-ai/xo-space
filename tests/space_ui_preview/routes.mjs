/* Intent names used by the older feature fixtures. "projects" here means
   the List page; primary-section navigation is tested separately. */
export const PAGE_ROUTES={dashboard:'projects/overview',projects:'projects/list',
  graph:'projects/graph',tree:'projects/tree',sharing:'projects/sharing',time:'projects/timeline',
  agents:'agents/overview',inbox:'inbox/items',setup:'setup/workspace',
  connectors:'setup/connectors',secrets:'setup/secrets',quirq:'setup/server/details'};
export const routeFor=id=>'#/'+(PAGE_ROUTES[id]||id);
export const projectPageId=id=>id==='projects'?'project-list':id;
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
