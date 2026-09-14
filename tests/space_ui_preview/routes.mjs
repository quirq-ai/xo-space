/* Intent names used by the older feature fixtures. "projects" here means
   the List page; primary-section navigation is tested separately. */
export const PAGE_ROUTES={dashboard:'projects/overview',projects:'projects/files/list',
  graph:'projects/files/graph',tree:'projects/files/tree',sharing:'projects/sharing',time:'projects/timeline',
  agents:'agents/overview',inbox:'inbox/items',setup:'setup/workspace',
  connectors:'setup/connectors',secrets:'setup/secrets',quirq:'setup/server/details'};
export const routeFor=id=>'#/'+(PAGE_ROUTES[id]||id);
export const projectPageId=id=>id==='projects'?'project-list':id;
export const projectPageSelector=id=>['project-list','graph','tree'].includes(projectPageId(id))
  ?'#section-nav [data-file-mode="'+projectPageId(id)+'"]'
  :'#section-nav [data-section-page="'+projectPageId(id)+'"]';
export async function openProjectPage(page,id){
  const mode=['project-list','graph','tree'].includes(projectPageId(id));
  const scope=page.locator(mode?'#section-nav [data-section-page="files"]':projectPageSelector(id));
  if(!await scope.isVisible())await page.locator('#tab-projects').click();
  if(mode&&!await page.locator(projectPageSelector(id)).isVisible())await scope.click();
  await page.locator(projectPageSelector(id)).click();
  await page.waitForURL('**/'+routeFor(id));
}
export async function openProjectList(page){
  await openProjectPage(page,'projects');
  await page.waitForFunction(()=>document.querySelector('#view-projects')?.classList.contains('is-active')
    &&document.querySelector('.topbar')?.dataset.toolbar==='search'
    &&document.querySelector('#view-search')?.placeholder==='Filter projects…'
    &&!document.querySelector('#view-search').disabled);
}
