/* Exercise retained data-loading races independently of the user-facing Refresh
   action. The production button reloads the entire document; these probes expose
   existing private controller loaders only inside intercepted fixture modules. */
export async function installRefreshProbes(context){
  const probes={
    'setup.js':'export const __refreshProbe=()=>{identity?.refresh();return loadAll();}; export const __refreshIdle=()=>!loading&&!refreshQueued;',
    'connectors.js':'export const __refreshProbe=refreshAll;',
    'inbox.js':'export const __refreshProbe=loadJobs;',
  };
  await context.route(/\/space\/js\/views\/(setup|connectors|inbox)\.js(?:\?.*)?$/,async route=>{
    const response=await route.fetch();
    const name=new URL(route.request().url()).pathname.split('/').at(-1);
    await route.fulfill({response,body:(await response.text())+'\n'+probes[name]+'\n'});
  });
}

export async function startDataRefresh(page,module='registry'){
  await page.evaluate(async module=>{
    const path='/js/'+(module==='registry'?'core/registry':'views/'+module)+'.js';
    const url=performance.getEntriesByType('resource').map(entry=>entry.name)
      .find(name=>new URL(name).pathname.endsWith(path));
    if(!url)throw new Error('Loaded module not found: '+module);
    const controller=await import(url);
    window.__dataRefresh=module==='registry'?controller.refreshCurrentView():controller.__refreshProbe();
  },module);
}

export async function waitForDataRefresh(page){
  await page.evaluate(()=>window.__dataRefresh);
}

export async function refreshData(page,module='registry'){
  await startDataRefresh(page,module);
  await waitForDataRefresh(page);
}

export async function waitForSetup(page){
  await page.waitForFunction(async()=>{
    const url=performance.getEntriesByType('resource').map(entry=>entry.name)
      .find(name=>new URL(name).pathname.endsWith('/js/views/setup.js'));
    return url&&(await import(url)).__refreshIdle();
  });
}
