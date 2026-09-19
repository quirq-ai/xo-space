"""The shell: js/shell.js boots from GET /api/ui and renders module pages from
their specs (space_ui/js/core/{spec-view,render,expr,actions,stream,widgets}.js).

Source-level pins run everywhere; the behavioural probes run the real modules
under Node against a small DOM stub and are skipped when node is missing."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"
MODULES = ROOT / "modules"
DASHES = re.compile("[\\u2013\\u2014]")

NEW_FILES = ("js/shell.js", "js/core/spec-view.js", "js/core/render.js", "js/core/expr.js",
             "js/core/actions.js", "js/core/stream.js", "js/core/widgets.js", "js/core/section-nav.js",
             "js/core/registry.js", "css/shell.css", "index.html", "README.md")


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def block_types_implemented() -> set[str]:
    src = read("js/core/render.js")
    listed = re.search(r"BLOCK_TYPES=Object\.freeze\(\[(.*?)\]\)", src, re.S).group(1)
    types = set(re.findall(r"'([a-z]+)'", listed))
    # each listed type has a renderer or a persistent controller
    renderers = set(re.findall(r"\b([a-z]+):render[A-Z]\w+", src))
    for kind in types - renderers:
        assert kind in ("stream", "widget"), kind
        assert f"block.type==='{kind}'" in src, kind
    return types


def walk_blocks(blocks: list) -> list:
    out = []
    for block in blocks:
        out.append(block)
        if isinstance(block.get("expand"), dict):
            out.extend(walk_blocks([block["expand"]]))
    return out


class ShellSourceTests(unittest.TestCase):
    def test_index_loads_the_shell_and_app_js_is_gone(self) -> None:
        index = read("index.html")
        self.assertIn('<script type="module" src="js/shell.js"></script>', index)
        self.assertNotIn("app.js", index)
        self.assertFalse((UI / "js" / "app.js").exists(), "js/app.js must be deleted; js/shell.js is the entry")
        self.assertIn('<link rel="stylesheet" href="css/shell.css">', index)
        self.assertNotIn("?v=", index)

    def test_shell_boots_from_api_ui_and_falls_back(self) -> None:
        shell = read("js/shell.js")
        self.assertIn("'/api/ui'", shell)
        self.assertIn("import {specView,MODULES_ROUTE} from './core/spec-view.js';", shell)
        # the legacy views register first, exactly as app.js did; spec pages after them
        legacy_last = shell.index("createSetupViews(connectorsView).forEach(registerView);")
        self.assertLess(shell.index("registerView(dashboardView);"), legacy_last)
        self.assertLess(legacy_last, shell.index("const tabs=applyUi(ui);"), "spec pages register after the legacy views")
        apply_ui = shell[shell.index("function applyUi(ui){"):shell.index("let syncing=null;")]
        self.assertIn("registerView(specView(page))", apply_ui)
        # the fallback: navigation.js tabs and the legacy views alone, then a retry on server-up
        self.assertIn("startRegistry({tabs:PRIMARY_TABS,defaultView:'projects'})", shell)
        self.assertIn("import {initServerWidget,onServerState} from './core/server-widget.js';", shell)
        self.assertIn("retryUiWhenUp()", shell)
        self.assertIn("export function onServerState(", read("js/core/server-widget.js"))
        # a modules write re-reads /api/ui so a page leaves navigation live
        self.assertIn("startsWith('/api/modules')", shell)
        self.assertIn("unregisterView", shell)
        self.assertIn("setNavigation", shell)
        self.assertNotIn("?v=", shell)

    def test_registry_exports_the_shell_needs(self) -> None:
        registry = read("js/core/registry.js")
        for name in ("unregisterView", "setNavigation", "listViews"):
            self.assertIn(f"export function {name}(", registry)
        self.assertIn("space:pages", registry)
        self.assertIn("const activeSection=v.section||v.id", registry)
        self.assertIn("if(/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName||''))return;", registry)

    def test_render_escapes_everything_and_markdown_goes_through_markdown_js(self) -> None:
        render = read("js/core/render.js")
        self.assertIn("import {esc,toast} from './ui.js';", render)
        self.assertIn("import {mdToHtml} from './markdown.js';", render)
        self.assertIn("mdToHtml(source)", render)
        # links only ever open http(s)
        self.assertIn("const HTTP=/^https?:\\/\\//i;", render)
        self.assertIn('rel="noopener noreferrer"', render)
        actions = read("js/core/actions.js")
        self.assertIn("const HTTP=/^https?:\\/\\//i;", actions)
        self.assertIn("window.open(url,'_blank','noopener')", actions)
        self.assertIn("button.disabled=true", actions)
        self.assertIn("toast(failText(res))", actions)

    def test_spec_view_contract(self) -> None:
        src = read("js/core/spec-view.js")
        self.assertIn("const id=page.module+'/'+page.id;", src)
        self.assertIn("parent:page.tab", src)
        self.assertIn("setSlottedInterval('spec:'+id", src)
        self.assertIn("clearSlottedInterval('spec:'+id)", src)
        self.assertIn("res.code==='module_disabled'", src)
        self.assertIn("#/'+MODULES_ROUTE", src)
        self.assertIn("toolbar:search?{search}:null", src)
        self.assertIn("failText(res)", src)

    def test_stream_and_widget_contracts(self) -> None:
        stream = read("js/core/stream.js")
        self.assertIn("Last-Event-ID", stream)
        self.assertIn("lines.unshift(line)", stream)
        self.assertIn("DEFAULT_LIMIT=200", stream)
        self.assertIn("controller.abort()", stream)
        widgets = read("js/core/widgets.js")
        self.assertIn("/space/modules/'", widgets)
        self.assertIn("mod.mount(el,{apiFetch,kit,switchTo:ctx.switchTo,refresh:ctx.refresh,page:ctx.page,data:pending})", widgets)
        self.assertIn("typeof mod.update!=='function'", widgets)
        self.assertIn("is unavailable", widgets)

    def test_every_spec_uses_only_implemented_block_types(self) -> None:
        implemented = block_types_implemented()
        specs = sorted(MODULES.glob("*/pages/*.json"))
        self.assertTrue(specs)
        for path in specs:
            spec = json.loads(path.read_text(encoding="utf-8"))
            for block in walk_blocks(spec["blocks"]):
                with self.subTest(spec=str(path.relative_to(ROOT)), block=block.get("type")):
                    self.assertIn(block["type"], implemented)

    def test_the_modules_page_spec(self) -> None:
        spec = json.loads((MODULES / "settings" / "pages" / "modules.json").read_text(encoding="utf-8"))
        self.assertEqual((spec["tab"], spec["route"], spec["label"], spec["order"], spec["read"]),
                         ("setup", "setup/modules", "Modules", 90, "/api/modules"))
        manifest = json.loads((MODULES / "settings" / "module.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["pages"]["modules"], {"enabled": True})
        self.assertEqual(manifest["folder"], "settings")
        rows = [b for b in spec["blocks"] if b["type"] == "list"][0]
        self.assertEqual(rows["key"], "name")
        toggles = rows["expand"]
        self.assertEqual(toggles["type"], "toggles")
        self.assertEqual(toggles["write"], "PUT /api/modules/{name}")
        paths = [t["path"] for t in toggles["toggles"]]
        for path in ("enabled", "api", "stream", "listeners", "commands", "tasks", "tasks.{key}", "pages", "pages.{key}"):
            self.assertIn(path, paths)
        per_item = [t for t in toggles["toggles"] if "rows" in t]
        self.assertEqual([t["rows"] for t in per_item],
                         ["capabilities.tasks.items|entries", "capabilities.pages.items|entries"])
        self.assertIn("Modules", read("js/views/setup-shell.js"))
        self.assertIn('href="#/setup/modules"', read("js/views/setup-shell.js"))

    def test_toggles_schema_addition_is_optional(self) -> None:
        schema = json.loads((ROOT / "services" / "schema" / "page.schema.json").read_text(encoding="utf-8"))
        item = schema["definitions"]["block"]["properties"]["toggles"]["items"]
        self.assertEqual(item["required"], ["label", "value"])
        self.assertIn("rows", item["properties"])
        self.assertIn("when", item["properties"])

    def test_readme_documents_the_shell(self) -> None:
        readme = read("README.md")
        self.assertIn("## The shell and page specs", readme)
        for word in ("`js/shell.js`", "`js/core/spec-view.js`", "`js/core/render.js`", "`js/core/expr.js`",
                     "`js/core/actions.js`", "`js/core/stream.js`", "`js/core/widgets.js`", "/api/ui", "PRIMARY_TABS"):
            self.assertIn(word, readme)
        self.assertNotIn("`js/app.js`", readme)

    def test_no_dashes_or_stamps_in_the_shell_files(self) -> None:
        for rel in NEW_FILES:
            with self.subTest(file=rel):
                src = read(rel)
                self.assertIsNone(DASHES.search(src), rel)
                self.assertNotIn("?v=", src)
        for rel in ("modules/settings/module.json", "modules/settings/pages/modules.json",
                    "modules/settings/__init__.py", "services/schema/page.schema.json"):
            with self.subTest(file=rel):
                self.assertIsNone(DASHES.search((ROOT / rel).read_text(encoding="utf-8")), rel)


PRELUDE = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
globalThis.location={pathname:'/space/',search:'',hash:''};
const events=new Map();
globalThis.addEventListener=(type,fn)=>{const l=events.get(type)||[];l.push(fn);events.set(type,l);};
globalThis.dispatchEvent=e=>{for(const fn of events.get(e.type)||[])fn(e);};
globalThis.CustomEvent=class{constructor(type,options={}){this.type=type;this.detail=options.detail;}};
globalThis.CSS={escape:s=>String(s).replace(/["\\]/g,'\\$&')};
const base=pathToFileURL(process.cwd()+'/space_ui/js/core/');
"""

EXPR_PROBE = PRELUDE + r"""
const {evaluate,fill,pathObject,truthy}=await import(new URL('expr.js',base));
const p={connections:[{toolkit:'gmail',enabled:true,events_total:3,last_error:null},{toolkit:'cal',enabled:false,events_total:2,last_error:'x'}],
  signed_in:true,caps:{tasks:{items:{poller:{enabled:true,description:'Polls'}}}}};
assert.equal(evaluate('connections|where:enabled|count',p),1);
assert.equal(evaluate('connections|sum:events_total',p),5);
assert.equal(evaluate('signed_in|map',p,undefined,{true:'yes',false:'no'}),'yes');
assert.equal(evaluate('last_error|map',p,p.connections[0],{null:'ok','*':'error'}),'ok');
assert.equal(evaluate('last_error|map',p,p.connections[1],{null:'ok','*':'error'}),'error');
assert.equal(evaluate('missing.deep.path',p),null);
assert.equal(evaluate('events_total|plural:event',p,p.connections[0]),'3 events');
assert.equal(evaluate('events_total|plural:event',p,{events_total:1}),'1 event');
assert.deepEqual(evaluate('caps.tasks.items|entries',p).map(e=>e.key),['poller']);
assert.equal(evaluate('page.signed_in',p,{signed_in:false}),true,'page.x is always the payload');
assert.equal(evaluate('signed_in',p,{signed_in:false}),false,'the row first');
assert.equal(evaluate('x|join:, ',p,{x:['a',null,'b']}),'a, b');
assert.equal(evaluate('ts|rel',p,{ts:new Date(Date.now()-120000).toISOString()}),'2m ago');
assert.equal(evaluate('nothing|rel',p),'');
assert.equal(evaluate('nothing|date',p),'');
assert.equal(evaluate('rows|count',p,{rows:null}),0);
assert.deepEqual(pathObject('tasks.poller.enabled',false),{tasks:{poller:{enabled:false}}});
assert.deepEqual(pathObject('enabled',true),{enabled:true});
assert.equal(fill('POST /api/connections/{toolkit}/poll',p,{toolkit:'a b'},true),'POST /api/connections/a%20b/poll');
assert.equal(fill('Task {key}',p,{key:'poller'}),'Task poller');
assert.equal(truthy([]),false);assert.equal(truthy({}),false);assert.equal(truthy(0),false);assert.equal(truthy('x'),true);
"""

RENDER_PROBE = PRELUDE + r"""
const {createRenderer,text,mapped,BLOCK_TYPES}=await import(new URL('render.js',base));
assert.deepEqual([...BLOCK_TYPES],['stats','list','table','cards','detail','form','toggles','timeline','calendar','chart','stream','text','widget']);
assert.equal(text(null),'');assert.equal(text(true),'yes');assert.equal(text(['a','b']),'a, b');
assert.equal(mapped('enabled',{true:'on',false:'off'},{},{enabled:false}),'off','a map applies without the |map pipe');
class Fake{constructor(){this.innerHTML='';this.hidden=false;this.children=new Map();}
  querySelector(sel){const m=/data-block="(\d+)"/.exec(sel);if(m){if(!this.children.has(m[1]))this.children.set(m[1],new Fake());return this.children.get(m[1]);}
    if(sel==='.spec-block-body'){this.body=this.body||new Fake();return this.body;}return null;}
  querySelectorAll(){return[];}addEventListener(){}removeEventListener(){}}
const host=new Fake();
const page={module:'m',id:'p',spec:{blocks:[
  {type:'stats',items:[{label:'<b>N</b>',value:'rows|count'}]},
  {type:'list',items:'rows',key:'id',row:{title:'title',detail:'detail',meta:['note'],link:'url',badge:{value:'state',map:{ok:'fine','*':'bad'}}},
    actions:[{label:'Go <i>',call:'POST /api/x/{id}',when:'flag'}],expand:{type:'text',text:'detail'}},
  {type:'table',items:'rows',key:'id',columns:[{label:'Title',value:'title',link:'url'},{label:'Note',value:'note',mono:true}]},
  {type:'toggles',write:'PUT /api/modules/{name}',toggles:[{label:'Enabled',value:'enabled',path:'enabled'},{label:'Task {key}',value:'enabled',path:'tasks.{key}',rows:'tasks|entries',help:'{description}'},{label:'Hidden',value:'enabled',path:'x',when:'nope'}]},
  {type:'text',text:'md'},
  {type:'cards',items:'rows',key:'id',row:{title:'title'}},
  {type:'detail',columns:[{label:'Name',value:'name'}]},
  {type:'list',items:'rows',when:'nope',empty:'never'},
  {type:'list',items:'none',empty:'Empty <x>'},
]}};
const r=createRenderer(host,page,{switchTo:()=>{},refresh:()=>{}});
await r.render({name:'<script>n</script>',enabled:true,md:'# Hi <script>x</script>\n\n[ok](https://a.example) [bad](javascript:alert(1))',
  tasks:{poller:{enabled:true,description:'<em>polls</em>'}},
  rows:[{id:'<a>',title:'<script>alert(1)</script>',detail:'d&d',note:'"q"',url:'javascript:alert(1)',state:'ok',flag:true},
        {id:'two',title:'Two',detail:'',note:'',url:'https://example.com/x?a=1&b=2',state:'meh',flag:false}]});
const html=i=>host.children.get(String(i)).body.innerHTML;
assert.ok(html(0).includes('&lt;b&gt;N&lt;/b&gt;'),'stat labels are escaped');
assert.ok(html(0).includes('>2<'),'the count renders');
assert.ok(!html(1).includes('<script>'),'row titles are escaped');
assert.ok(html(1).includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
assert.ok(html(1).includes('d&amp;d'));
assert.ok(!html(1).includes('href="javascript:'),'a javascript: link never becomes an anchor');
assert.ok(html(1).includes('href="https://example.com/x?a=1&amp;b=2" target="_blank" rel="noopener noreferrer"'),'http(s) links open in a new tab');
assert.ok(html(1).includes('>fine<')&&html(1).includes('>bad<'),'badge maps apply');
assert.ok(html(1).includes('Go &lt;i&gt;'),'action labels are escaped');
assert.equal((html(1).match(/data-act="0"/g)||[]).length,1,'when hides an action per row');
assert.equal((html(1).match(/data-expand /g)||[]).length,2,'expand renders a toggle per row');
assert.ok(html(1).includes('data-row-key="&lt;a&gt;"'),'row keys are escaped in attributes');
assert.ok(html(2).includes('<th data-slot="table-head">Title</th>'));
assert.ok(html(2).includes('class="spec-mono">&quot;q&quot;<'),'cells are escaped, mono columns marked');
assert.ok(html(2).includes('data-expand-row')===false);
const toggles=html(3);
assert.equal((toggles.match(/role="switch"/g)||[]).length,2,'one switch per static toggle and per enumerated item; when hides one');
assert.ok(toggles.includes('data-path="enabled"')&&toggles.includes('data-path="tasks.poller"'),'paths fill from the item');
assert.ok(toggles.includes('<b>Task poller</b>'),'labels fill from the item');
assert.ok(toggles.includes('&lt;em&gt;polls&lt;/em&gt;'),'help text is escaped');
assert.ok(html(4).startsWith('<div class="spec-text md"><h1>Hi &lt;script&gt;x&lt;/script&gt;</h1>'),'text blocks go through markdown.js, escaped first');
assert.ok(html(4).includes('href="https://a.example"')&&!html(4).includes('href="javascript:'),'markdown links are http(s) only');
assert.ok(html(5).includes('data-slot="card"'));
assert.ok(html(6).includes('<dt>Name</dt><dd>&lt;script&gt;n&lt;/script&gt;</dd>'));
assert.equal(host.children.get('7').hidden,true,'when hides a block');
assert.ok(html(8).includes('Empty &lt;x&gt;'),'empty text goes through the kit, escaped');
"""

NAV_PROBE = PRELUDE + r"""
const {pagesFor}=await import(new URL('section-nav.js',base));
const views=[
  {id:'inbox-items',route:'inbox/items',label:'Items',order:0,parent:'inbox',secondary:true,sectionNav:true},
  {id:'connections/connections',route:'inbox/connections',label:'Connections',order:20,parent:'inbox',secondary:true,sectionNav:true,spec:true},
  {id:'timeline/live',route:'inbox/activity',label:'Activity',order:25,parent:'inbox',secondary:true,sectionNav:true,spec:true},
  {id:'jobs/jobs',route:'inbox/jobs',label:'Jobs',order:30,parent:'inbox',secondary:true,sectionNav:true,spec:true},
  {id:'extra/page',route:'inbox/extra',label:'Extra',order:55,parent:'inbox',secondary:true,sectionNav:true,spec:true},
  {id:'inbox-sharing-activity',route:'inbox/sharing-activity',label:'Sharing activity',order:0,parent:'inbox',secondary:true,sectionNav:true},
  {id:'sharing',route:'inbox/sharing',label:'Sharing',order:0,parent:'inbox',secondary:true,sectionNav:true},
  {id:'quirq',route:'setup/server/details',label:'Quirq',order:8,parent:'setup',secondary:false,sectionNav:false},
  {id:'setup/workspace',route:'setup/workspace',label:'Workspace',order:10,parent:'setup',secondary:true,sectionNav:false},
  {id:'settings/modules',route:'setup/modules',label:'Modules',order:90,parent:'setup',secondary:true,sectionNav:true,spec:true},
  {id:'dashboard',route:'projects/overview',label:'Overview',order:0,parent:'projects',secondary:true,sectionNav:true},
  {id:'project-list',route:'projects/data/list',label:'List',order:0,parent:'projects',secondary:true,sectionNav:true},
  {id:'time',route:'projects/timeline',label:'Timeline',order:0,parent:'projects',secondary:true,sectionNav:true},
  {id:'project-manage',route:'projects/manage',label:'Manage',order:0,parent:'projects',secondary:true,sectionNav:true},
];
assert.deepEqual(pagesFor('inbox',views).map(e=>[e.id,e.route]),[
  ['inbox-items','inbox/items'],['connections/connections','inbox/connections'],['timeline/live','inbox/activity'],
  ['jobs/jobs','inbox/jobs'],['inbox-sharing-activity','inbox/sharing-activity'],['extra/page','inbox/extra'],['sharing','inbox/sharing']],
  'legacy order, spec pages taking over their routes, new pages by order');
assert.deepEqual(pagesFor('setup',views).map(e=>e.id),['setup/workspace','settings/modules'],'secondary:false is never listed');
assert.deepEqual(pagesFor('projects',views).map(e=>e.id),['dashboard','data','time','project-manage'],'Data stands for List, Graph and Tree');
assert.deepEqual(pagesFor('inbox',[]).map(e=>e.id),['inbox-items','inbox-connections','inbox-jobs','inbox-activity','inbox-sharing-activity','sharing'],'the legacy tables alone before /api/ui');
// the connections module switches off: its spec view goes, an "off" note (secondary:false) takes the route, and the entry leaves
const off=views.filter(v=>v.id!=='connections/connections').concat([{id:'connections/connections',route:'inbox/connections',label:'Connections',order:20,parent:'inbox',secondary:false,sectionNav:true}]);
assert.deepEqual(pagesFor('inbox',off).map(e=>e.id),['inbox-items','timeline/live','jobs/jobs','inbox-sharing-activity','extra/page','sharing'],'a page whose module is off leaves navigation');
"""

REGISTRY_PROBE = PRELUDE + r"""
globalThis.history={pushState:(_s,_t,hash)=>{location.hash=hash;},replaceState:(_s,_t,hash)=>{location.hash=hash;}};
globalThis.requestAnimationFrame=fn=>fn();
const elements=new Map();
class El{constructor(id,tag='DIV'){this.id=id;this.tagName=tag;this.children=[];this.classes=new Set();this.attributes={};this.listeners={};this.removed=false;
  this.classList={toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n),add:n=>this.classes.add(n),contains:n=>this.classes.has(n)};if(id)elements.set(id,this);}
  addEventListener(t,fn){this.listeners[t]=fn;}setAttribute(n,v){this.attributes[n]=v;}removeAttribute(n){delete this.attributes[n];}
  appendChild(c){this.children.push(c);if(c.id)elements.set(c.id,c);}replaceChildren(...c){this.children=[];c.forEach(x=>this.appendChild(x));}
  scrollIntoView(){}remove(){this.removed=true;elements.delete(this.id);}}
const stage=new El('stage'),tabs=new El('tabs');
globalThis.document={activeElement:null,getElementById:id=>elements.get(id)||null,createElement:tag=>new El('',tag.toUpperCase()),
  querySelector:s=>s==='.tabs'?tabs:null,querySelectorAll:s=>s.startsWith('.tabs ')?tabs.children:[]};
const registry=await import(new URL('registry.js',base));
const pagesEvents=[];
addEventListener('space:pages',e=>pagesEvents.push(e.detail.views.map(v=>v.id)));
const view=(id,route,parent,extra={})=>({id,route,parent,label:id,nav:false,mount:async()=>{},show(){},hide(){},...extra});
registry.registerView(view('inbox-items','inbox/items','inbox'));
registry.registerView(view('inbox-connections','inbox/connections','inbox'));
registry.registerView(view('setup/workspace','setup/workspace','setup',{sectionNav:false}));
registry.registerView(view('connections/connections','inbox/connections','inbox',{spec:true,order:20,section:'spec-connections-connections'}));
const tabsIn=[{id:'inbox',label:'Inbox',defaultView:'inbox/items'},{id:'setup',label:'Setup',defaultView:'setup/workspace'}];
registry.startRegistry({tabs:tabsIn,defaultView:'inbox/items'});
assert.deepEqual(tabs.children.map(t=>t.id),['tab-inbox','tab-setup']);
assert.equal(pagesEvents.length,1,'the page list is announced once at start');
assert.ok(elements.has('view-spec-connections-connections'),'the registry creates the spec section');
await registry.switchTo('inbox/connections');
assert.equal(location.hash,'#/inbox/connections');
assert.ok(elements.get('view-spec-connections-connections').classes.has('is-active'),'the spec page wins the route it shares with a legacy view');
const listed=registry.listViews();
assert.deepEqual(listed.find(v=>v.id==='setup/workspace').sectionNav,false);
assert.equal(listed.find(v=>v.id==='connections/connections').spec,true);
// the module switches off: the page goes, the person lands on the tab default
assert.equal(registry.unregisterView('connections/connections'),true);
assert.equal(registry.unregisterView('connections/connections'),false);
assert.equal(elements.has('view-spec-connections-connections'),false,'a section the registry created is removed with its view');
assert.ok(pagesEvents.length>=2,'the page list is announced again');
assert.ok(!pagesEvents.at(-1).includes('connections/connections'));
// the route resolves to the legacy view again, so the person stays on the same route
assert.equal(location.hash,'#/inbox/connections');
registry.unregisterView('inbox-connections');
assert.equal(location.hash,'#/inbox/items','no view left on the route: the tab default');
// navigation can be re-applied later without stacking listeners
registry.setNavigation([{id:'inbox',label:'Inbox',defaultView:'inbox/items'},{id:'setup',label:'Set up',defaultView:'setup/workspace'},{id:'more',label:'More',defaultView:'more/x'}]);
assert.deepEqual(tabs.children.map(t=>t.textContent),['Inbox','Set up','More']);
assert.ok(tabs.children[0].classes.has('is-on'),'the active tab stays marked');
registry.registerView(view('more/x','more/x','more',{spec:true,section:'spec-more-x'}));
assert.ok(elements.has('view-spec-more-x'),'a view registered after the start gets its section');
await registry.switchTo('more/x');
assert.equal(location.hash,'#/more/x');
"""

STREAM_PROBE = PRELUDE + r"""
const {parseSse}=await import(new URL('stream.js',base));
const state={rest:''};
let parsed=parseSse(state,'id: 2026-01-01T00:00:00Z\nevent: file.edited\ndata: {"ts":"t1","type":"file.edited"}\n\n: keepalive\n\nid: 2\nevent: x\ndata: {"a":');
assert.deepEqual(parsed,[{id:'2026-01-01T00:00:00Z',event:'file.edited',data:'{"ts":"t1","type":"file.edited"}'}]);
parsed=parseSse(state,'1}\n\n');
assert.deepEqual(parsed,[{id:'2',event:'x',data:'{"a":1}'}]);
assert.equal(state.rest,'');
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ShellNodeTests(unittest.TestCase):
    def run_probe(self, source: str) -> None:
        result = subprocess.run(["node", "--input-type=module", "-e", source],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_expr_pipes(self) -> None:
        self.run_probe(EXPR_PROBE)

    def test_render_escapes_once_and_honours_when_map_expand_and_rows(self) -> None:
        self.run_probe(RENDER_PROBE)

    def test_section_nav_lists_the_tab_pages_from_the_registry(self) -> None:
        self.run_probe(NAV_PROBE)

    def test_registry_unregisters_and_reapplies_navigation(self) -> None:
        self.run_probe(REGISTRY_PROBE)

    def test_stream_parses_server_sent_events(self) -> None:
        self.run_probe(STREAM_PROBE)


if __name__ == "__main__":
    unittest.main()
