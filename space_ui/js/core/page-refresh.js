/* One explicit full-page refresh for the header and command palette. The
   browser keeps the current URL (including route and query-string auth) and
   reloads the shell, data and mounted views together. */
export function refreshPage(){
  location.reload();
}

export function initPageRefresh(){
  const button=document.getElementById('space-refresh');
  if(!button||button.dataset.initialized)return;
  button.dataset.initialized='true';
  button.addEventListener('click',refreshPage);
}
