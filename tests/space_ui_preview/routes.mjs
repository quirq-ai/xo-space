/* Intent names used by the older feature fixtures. "projects" here means
   the List page; primary-section navigation is tested separately. */
export const PAGE_ROUTES={dashboard:'projects/overview',projects:'projects/data/list',
  graph:'projects/data/graph',tree:'projects/data/tree',sharing:'inbox/sharing',time:'projects/timeline',manage:'projects/manage',
  agents:'agents/overview',inbox:'inbox/items',setup:'setup/workspace',
  connectors:'setup/connections',secrets:'setup/secrets',quirq:'setup/server/details'};
export const routeFor=id=>'#/'+(PAGE_ROUTES[id]||id);
export const projectPageId=id=>id==='projects'?'project-list':id==='manage'?'project-manage':id;
export const projectPageSelector=id=>['project-list','graph','tree'].includes(projectPageId(id))
  ?'.view.is-active .data-views [data-data-mode="'+projectPageId(id)+'"]'
  :'#section-nav [data-section-page="'+projectPageId(id)+'"]';
export async function openProjectPage(page,id){
  const mode=['project-list','graph','tree'].includes(projectPageId(id));
  const scope=page.locator(mode?'#section-nav [data-section-page="data"]':projectPageSelector(id));
  if(!await scope.isVisible())await page.locator(id==='sharing'?'#tab-inbox':'#tab-projects').click();
  if(mode){
    if(!/^#\/projects\/data\/(list|graph|tree)$/.test(new URL(page.url()).hash))await scope.click();
    await page.waitForFunction(()=>[...document.querySelectorAll('.view.is-active .data-views [aria-current=page]')]
      .some(link=>link.getClientRects().length&&link.getAttribute('href')===location.hash));
  }
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
