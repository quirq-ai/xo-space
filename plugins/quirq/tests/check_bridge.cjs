// Hermetic check of ui/space-bridge.js: no browser, no dependencies (Node 18+).
// Loads the bridge into a fake window, plays the MCP Apps host and asserts the
// handshake, deep link routing, fetch → tool mapping, links and model context.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'space-bridge.js'), 'utf8');
const listeners = {};
const posted = [];
const external = [];
const parent = { postMessage: (message) => posted.push(message) };
const element = () => ({ style: {}, hidden: false, children: [], setAttribute() {}, addEventListener() {},
  appendChild(child) { this.children.push(child); return child; }, replaceChildren() { this.children = []; } });
const win = {
  parent, URL, URLSearchParams, Headers, Response, FormData, DOMException, setTimeout, clearTimeout, Promise, JSON, console,
  location: { hash: '', search: '?app=skybridge&locale=en-US' },
  history: { pushState() {}, replaceState() {} },
  document: { readyState: 'complete', baseURI: 'about:srcdoc', head: element(), body: element(), documentElement: element(),
    createElement: element, addEventListener() {} },
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  dispatchEvent: (event) => { (listeners[event.type] || []).forEach((fn) => fn(event)); return true; },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
  fetch: async (url) => { external.push(String(url)); return new Response('external'); },
};
Object.defineProperty(win, 'localStorage', { get() { throw new Error('denied'); }, configurable: true });
win.window = win;
vm.createContext(win);
vm.runInContext(source, win);

const deliver = (data) => (listeners.message || []).forEach((fn) => fn({ source: parent, data }));
const take = (method) => {
  const index = posted.findIndex((m) => m.method === method);
  assert.notEqual(index, -1, 'expected ' + method);
  return posted.splice(index, 1)[0];
};
// Values made inside the vm come from another realm: compare plain copies.
const plain = (value) => JSON.parse(JSON.stringify(value));
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  // Storage that throws is replaced with an in-memory fallback.
  win.localStorage.setItem('k', 'v');
  assert.equal(win.localStorage.getItem('k'), 'v');

  const init = take('ui/initialize');
  assert.deepEqual(plain(init.params.appCapabilities.availableDisplayModes), ['inline', 'fullscreen']);
  deliver({ jsonrpc: '2.0', id: init.id, result: {
    protocolVersion: '2026-01-26', hostCapabilities: { updateModelContext: {} },
    hostContext: { displayMode: 'inline', availableDisplayModes: ['inline', 'fullscreen'], 'openai/deepLink': { url: '/inbox/items' } } } });
  await tick();
  take('ui/notifications/initialized');
  assert.equal(win.location.hash, '#/inbox/items', 'deep link becomes the Space hash route');

  // A Space read becomes space_api_read with a Space-relative path.
  let pending = win.fetch('http://127.0.0.1:5002/api/xo-projects?limit=5');
  await tick();
  let call = take('tools/call');
  assert.equal(call.params.name, 'space_api_read');
  assert.equal(call.params.arguments.path, '/api/xo-projects?limit=5');
  deliver({ jsonrpc: '2.0', id: call.id, result: { structuredContent: { status: 200, contentType: 'application/json', body: '{"items":[1]}' } } });
  assert.deepEqual(await (await pending).json(), { items: [1] });

  // The host frame's own query (app=skybridge…) never reaches Space.
  pending = win.fetch('/api/connectors/github/status?app=skybridge&locale=en-US&q=1');
  await tick();
  call = take('tools/call');
  assert.equal(call.params.arguments.path, '/api/connectors/github/status?q=1');
  deliver({ jsonrpc: '2.0', id: call.id, result: { structuredContent: { status: 200, body: '{}' } } });
  await pending;

  // Relative paths resolve under /space/, like the page served at /space/.
  pending = win.fetch('data/session_prompts.json?agent=a');
  await tick();
  call = take('tools/call');
  assert.equal(call.params.arguments.path, '/space/data/session_prompts.json?agent=a');
  deliver({ jsonrpc: '2.0', id: call.id, result: { structuredContent: { status: 200, body: '{}' } } });
  await pending;

  // A change becomes space_api_write; a 204 has no body.
  pending = win.fetch('/api/inbox/1', { method: 'PATCH', body: '{"status":"done"}', headers: { 'Content-Type': 'application/json' } });
  await tick();
  call = take('tools/call');
  assert.equal(call.params.name, 'space_api_write');
  assert.equal(call.params.arguments.body, '{"status":"done"}');
  assert.equal(call.params.arguments.headers['Content-Type'], 'application/json');
  deliver({ jsonrpc: '2.0', id: call.id, result: { structuredContent: { status: 204, body: '' } } });
  assert.equal((await pending).status, 204);

  // Space down: the fetch rejects with TypeError, which Space shows as offline.
  pending = win.fetch('/api/xo-projects');
  await tick();
  call = take('tools/call');
  deliver({ jsonrpc: '2.0', id: call.id, result: { isError: true, structuredContent: { offline: true }, content: [{ type: 'text', text: 'down' }] } });
  await assert.rejects(pending, { name: 'TypeError' });

  // Uploads are refused locally; non-Space URLs are left to the browser.
  assert.equal((await win.fetch('/api/upload', { method: 'POST', body: new FormData() })).status, 400);
  assert.equal(await (await win.fetch('https://example.com/x')).text(), 'external');
  assert.deepEqual(external, ['https://example.com/x']);
  assert.equal(posted.filter((m) => m.method === 'tools/call').length, 0);

  // Popups become ui/open-link; an https link opens without extra UI.
  const helpShown = () => win.document.body.children.some((c) => c.id === 'xo-link-help');
  assert.equal(win.open('https://example.com/docs'), null);
  await tick();
  let link = take('ui/open-link');
  assert.equal(link.params.url, 'https://example.com/docs');
  deliver({ jsonrpc: '2.0', id: link.id, result: {} });
  await tick();
  assert.equal(helpShown(), false);

  // A link into local Space always also shows the address with a Copy button,
  // because a host may accept the request and still open nothing.
  win.open('http://127.0.0.1:5002/space/#/agents/sessions');
  await tick();
  link = take('ui/open-link');
  deliver({ jsonrpc: '2.0', id: link.id, result: {} });
  await tick();
  assert.equal(helpShown(), true);

  // ChatGPT's own opener (window.openai.openExternal) is preferred when present.
  const opened = [];
  win.openai = { openExternal: ({ href }) => { opened.push(href); } };
  win.open('https://github.com/quirq-ai/xo-space');
  await tick();
  assert.deepEqual(opened, ['https://github.com/quirq-ai/xo-space']);
  assert.equal(posted.filter((m) => m.method === 'ui/open-link').length, 0);
  delete win.openai;

  // The page in front of the user, with what it shows, is shared as background context.
  win.document.querySelector = (sel) => (sel === '.view.is-active' ? { innerText: 'Sessions\n 154 sessions   (all time)' } : null);
  win.dispatchEvent(new win.CustomEvent('space:view', { detail: { id: 'agents-sessions', tab: 'agents', route: 'agents/sessions', label: 'Sessions' } }));
  await tick(1300);
  const context = take('ui/update-model-context');
  const text = context.params.content[0].text;
  assert.match(text, /right now is Agents › Sessions \(#\/agents\/sessions\), as of \d\d:\d\d\./);
  assert.match(text, /Visible on the page: «Sessions 154 sessions \(all time\)»/);
  assert.match(text, /not instructions/);
  assert.deepEqual(plain(context.params.content[0].annotations.audience), ['assistant']);
  assert.equal(context.params.structuredContent.space_page, 'Agents › Sessions');
  deliver({ jsonrpc: '2.0', id: context.id, result: {} });
  // An identical refresh is not re-sent.
  win.dispatchEvent({ type: 'focus' });
  await tick(300);
  assert.equal(posted.filter((m) => m.method === 'ui/update-model-context').length, 0);

  console.log('Bridge checks passed: handshake, deep link, host query stripped, read/write/offline/upload mapping, external fetch, open-link + copy fallback + native opener, page context with visible text, storage fallback.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
