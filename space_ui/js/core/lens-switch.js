/* The Files lens switch.

   Files is one tab with four lenses — List, Graph, Tree and Sharing — and
   the switch between them belongs to the tab, not to any one lens. It used
   to be rendered per lens: an overlay pinned to the canvas in Graph, and a
   header child in List and in Tree. Three renderers meant three positions,
   so the control jumped as you used it.

   Here it is one element in the stage (index.html), shown whenever the active
   view belongs to the Files tab and hidden otherwise.

   It navigates by hash rather than importing the registry's switchTo. With no
   bundler, `registry.js` and `registry.js?v=123` are two different modules to
   the browser, each with its own view table — app.js imports the stamped
   specifier, so importing the bare one here would hand us an empty registry
   whose switchTo silently does nothing. The registry already listens for
   hashchange, and the hash is the app's real route. */

const LENSES = ['projects', 'graph', 'tree', 'sharing'];

export function initLensSwitch(){
  const el = document.getElementById('fileslens');
  if(!el) return;

  el.addEventListener('click', e => {
    const button = e.target.closest('[data-files-lens]');
    if(button) location.hash = '#/' + button.dataset.filesLens;
  });

  const sync = (id, tab) => {
    /* Files owns the tab id 'projects'; Graph and Tree are its nav-less
       children and report the same tab. */
    const mine = tab === 'projects' || LENSES.includes(id);
    el.hidden = !mine;
    if(!mine) return;
    el.querySelectorAll('[data-files-lens]').forEach(button => {
      const on = button.dataset.filesLens === id;
      button.classList.toggle('is-on', on);
      button.setAttribute('aria-current', on ? 'true' : 'false');
    });
  };

  addEventListener('space:view', e => {
    const {id, tab} = e.detail || {};
    sync(id, tab);
  });
  /* A deep link paints before any switch is announced. */
  const initial = location.hash.replace(/^#\//, '');
  if(LENSES.includes(initial)) sync(initial, 'projects');
}
