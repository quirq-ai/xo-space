/* Wiki tab — versioned operating documentation for Space.

   The pages live beside the code they document, work offline, and describe
   the exact watcher/storage contracts shipped by this server version.
*/

const PAGES=[
  {
    id:'storage',
    section:'Start here',
    title:'Storage & data map',
    summary:'The boundary between runtime data, portable .xo metadata, and local .quirq state.'
  },
  {
    id:'installation',
    section:'Start here',
    title:'Install & run locally',
    summary:'Prerequisites, one-command development, configuration, verification, and packaging.'
  },
  {
    id:'watcher',
    section:'Runtime systems',
    title:'How the watcher works',
    summary:'The tick loop, normalized events, sinks, runtime coverage, and failure model.'
  },
  {
    id:'xo-data',
    section:'Data catalog',
    title:'Everything in .xo',
    summary:'Every project and workspace document, its fields, owner, lifecycle, and API use.'
  },
  {
    id:'quirq-data',
    section:'Data catalog',
    title:'Everything in .quirq',
    summary:'Onboarding, cursors, locks, and live presence that stay on one machine.'
  },
  {
    id:'flows',
    section:'Design guide',
    title:'Building useful flows',
    summary:'Practical read paths for live work, history, analytics, todos, and debugging.'
  },
  {
    id:'collaboration',
    section:'Design guide',
    title:'Collaborative version history',
    summary:'Google Docs-style live editing, durable versions, restore, audit, permissions, and safe data boundaries.'
  },
  {
    id:'tab-dashboard',
    section:'Tab guides',
    title:'Dashboard tab',
    summary:'See every XO project grouped by Engineering, Ops, Documentation, Research, or Marketing.'
  },
  {
    id:'tab-graph',
    section:'Tab guides',
    title:'Graph tab',
    summary:'Navigate the generated XO workspace map, focus nodes, and follow relationships.'
  },
  {
    id:'tab-timeline',
    section:'Tab guides',
    title:'Timeline tab',
    summary:'Scrub dated workspace artifacts and use the nested Six Degrees relationship tool.'
  },
  {
    id:'tab-sessions',
    section:'Tab guides',
    title:'Sessions tab',
    summary:'Compare Claude Code, Codex, and Cursor telemetry with source filters, honest cost states, pagination, and prompt turns.'
  },
  {
    id:'tab-projects',
    section:'Tab guides',
    title:'Projects tab',
    summary:'Inspect discovered projects, durable .xo history, todos, and live .quirq presence.'
  },
  {
    id:'tab-chat',
    section:'Tab guides',
    title:'Chat tab',
    summary:'Start and resume agent sessions, stream responses, choose projects, and abort runs.'
  },
  {
    id:'tab-wiki',
    section:'Tab guides',
    title:'Wiki tab',
    summary:'Use the versioned local operating manual and understand how its pages are maintained.'
  },
  {
    id:'tab-quirq',
    section:'Tab guides',
    title:'Quirq tab',
    summary:'Compare machine-local watcher state with portable project .xo outputs.'
  },
  {
    id:'tab-setup',
    section:'Tab guides',
    title:'Setup tab',
    summary:'Configure roots, agent runtime, watcher coverage, credentials, and managed restarts.'
  }
];

const ARTICLES={
  storage:storageArticle,
  installation:installationArticle,
  watcher:watcherArticle,
  'xo-data':xoDataArticle,
  'quirq-data':quirqDataArticle,
  flows:flowsArticle,
  collaboration:collaborationArticle,
  'tab-dashboard':()=>tabGuideArticle('dashboard'),
  'tab-graph':()=>tabGuideArticle('graph'),
  'tab-timeline':()=>tabGuideArticle('timeline'),
  'tab-sessions':()=>tabGuideArticle('sessions'),
  'tab-projects':()=>tabGuideArticle('projects'),
  'tab-chat':()=>tabGuideArticle('chat'),
  'tab-wiki':()=>tabGuideArticle('wiki'),
  'tab-quirq':()=>tabGuideArticle('quirq'),
  'tab-setup':()=>tabGuideArticle('setup')
};
const wikiEsc=value=>String(value??'').replace(
  /[&<>"]/g,
  char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char])
);

let root=null;
let activePage='storage';
let go=()=>{};

export default {
  id:'wiki',label:'Wiki',order:7,
  async mount(el,ctx){
    root=el;
    go=ctx.switchTo;
    renderShell();
  },
  show(){/* Preserve the selected page and article scroll position. */}
};

function renderShell(){
  root.innerHTML=
    '<div class="wiki-shell">'
      +'<aside class="wiki-nav">'
        +'<div class="wiki-nav-head">Quirq Wiki</div>'
        +'<p>Architecture and flow guides for the local control plane.</p>'
        +'<div class="wiki-nav-pages">'
          +PAGES.map(pageButton).join('')
        +'</div>'
      +'</aside>'
      +'<main class="wiki-main" id="wiki-main"></main>'
    +'</div>';
  root.querySelectorAll('[data-wiki-page]').forEach(button=>{
    button.addEventListener('click',()=>selectPage(button.dataset.wikiPage));
  });
  root.querySelector('#wiki-main').addEventListener('click',event=>{
    const button=event.target.closest('[data-open-tab]');
    if(button)go(button.dataset.openTab);
  });
  selectPage(activePage);
}

function pageButton(page){
  return'<button class="wiki-page-link" data-wiki-page="'+page.id+'">'
    +'<span>'+page.section+'</span>'
    +'<b>'+page.title+'</b>'
    +'<em>'+page.summary+'</em>'
    +'</button>';
}

function selectPage(id){
  if(!ARTICLES[id])return;
  activePage=id;
  root.querySelectorAll('[data-wiki-page]').forEach(button=>{
    const selected=button.dataset.wikiPage===id;
    button.classList.toggle('is-on',selected);
    button.setAttribute('aria-current',selected?'page':'false');
  });
  const main=root.querySelector('#wiki-main');
  main.innerHTML=ARTICLES[id]();
  main.scrollTop=0;
}

addEventListener('space:wiki-page',event=>{
  const id=String(event.detail||'');
  if(!ARTICLES[id])return;
  activePage=id;
  if(root)selectPage(id);
});

const TAB_GUIDES={
  dashboard:{
    tab:'dashboard',
    name:'Dashboard',
    kicker:'Tab guide · Project environments',
    title:'Dashboard: projects inside purpose environments',
    intro:'Dashboard follows main’s Inbox graph model. Each discovered XO project is one visible node. Engineering, Ops, Documentation, Research, and Marketing are not project nodes: each is a softly filled, dashed enclosure around the projects that belong to that environment.',
    facts:['one node per project','five enclosing environments','overlapping membership','read-only'],
    jobs:[
      ['Survey the workspace','Read each colored boundary as a collection of projects with a shared purpose, not as one aggregate node.'],
      ['See overlap','A project with several purposes remains one node and sits between the applicable environments; every applicable enclosure includes it.'],
      ['Read project form','Node glyphs describe project form independently of purpose: app, one-pager, docs, slides, or unknown.'],
      ['Trace an environment','Select its labeled anchor or a project and carry that focused set into Timeline or Six Degrees.']
    ],
    sources:[
      ['GET /space/data/dashboard.json','Builds the categorized graph from the same bounded live scan as the ordinary Graph tab.','Live primary source'],
      ['<XO root>/<project>/.xo/project.json','An optional manual category or saved multi-category classification takes precedence over inferred filename signals.','Portable project metadata'],
      ['Project paths and filenames','App manifests, infrastructure files, writing, research formats, decks, contracts, and asset ratios infer environment memberships and node form.','Derived heuristics']
    ],
    steps:[
      ['Enter Dashboard','It is the first top-level tab and the default Space route.'],
      ['Enter the map','Dismiss the introduction to reveal project nodes, five labeled anchors, and the dashed environment boundaries around their members.'],
      ['Read a boundary','The tinted area is the environment. Its small internal group point is only a layout/focus anchor; it is not the environment’s data representation.'],
      ['Focus','Click a project or environment anchor; double-click an anchor to expand or collapse its primary project set.'],
      ['Search','Press / and search projects by name.'],
      ['Compare Graph','Open Graph for the detailed project, folder, and artifact map. The atlas reloads once because each mode runs a separate simulation dataset.']
    ],
    checks:[
      ['Project missing','Confirm the XO root in Setup and verify GET /space/data/dashboard.json. The live scan is bounded and cached for up to 30 seconds.'],
      ['Unexpected environment','The classifier uses saved metadata first and filename signals second; ambiguous projects may need a manual category.'],
      ['Project sits between boundaries','That is intentional multi-environment membership. Strong secondary springs place the one shared node between its collections.'],
      ['Environment has no boundary','An empty environment keeps its label but has no project area to enclose.'],
      ['Graph switches with a reload','That reset is intentional so Dashboard and Graph never share stale physics or selection state.']
    ],
    note:'Dashboard environments are collections of project nodes. The dashed hull is the collection; the anchor only gives the physics, label, focus, and filtering controls a stable target. Dashboard and Graph are read-only and write neither project files, .xo, nor .quirq.'
  },
  graph:{
    tab:'graph',
    name:'Graph',
    kicker:'Tab guide · Workspace relationships',
    title:'Graph: explore the shape of XO',
    intro:'Graph turns the XO projects root into an interactive map of projects, clusters, artifacts, and explicit cross-links. It is a navigation and discovery surface, not a filesystem editor.',
    facts:['live generated map','30-second server cache','search + graph re-rooting','read-only'],
    jobs:[
      ['Find an artifact','Search by title, tag, project, or cluster and fly directly to the matching node.'],
      ['Understand relationships','Select a node to inspect its neighborhood and follow parent, cluster, and cross-project ties.'],
      ['Change perspective','Use Graph root to temporarily reorganize the layout around any node without changing the XO filesystem.']
    ],
    sources:[
      ['GET /space/data/space.json','Generated by build_space_data() from the XO projects root and portable project metadata.','Live primary source'],
      ['<XO root>/<project>/.xo/','Project identity and derived session/history metadata help label and connect work.','Watcher + adapter output'],
      ['space_ui/data/space.json','Bundled fallback used only when live graph generation fails.','Static fallback']
    ],
    steps:[
      ['Search','Press / or use the top-right search field.'],
      ['Focus','Click a node; double-click clusters to expand or collapse them.'],
      ['Inspect','Read the detail panel and relationship list.'],
      ['Follow time','Choose “Show on timeline” to carry the selected run into Timeline.']
    ],
    checks:[
      ['Empty graph','Confirm the XO root in Setup and verify GET /space/data/space.json.'],
      ['Unexpected root label','Graph root is an in-view lens, not the host XO directory configured in Setup.'],
      ['Stale result','The generated payload is cached for up to 30 seconds.'],
      ['Missing relationship','The link must exist in the generated project/artifact map; Graph does not infer conversation semantics.']
    ],
    note:'Graph reads derived metadata and project structure. It never writes project files, .xo, or .quirq.'
  },
  timeline:{
    tab:'time',
    name:'Timeline',
    kicker:'Tab guide · Time and relationships',
    title:'Timeline: when work happened',
    intro:'Timeline uses the same generated workspace map as Graph, but arranges dated artifacts into lanes. Six Degrees now lives inside this tab as a second lens for shortest-path exploration.',
    facts:['same graph dataset','date scrubber','playback mode','Six Degrees nested here'],
    jobs:[
      ['Replay growth','Scrub or play through the workspace to see artifacts appear in chronological order.'],
      ['Trace one cluster','Open a cluster from Graph and carry its related artifacts into a focused timeline trace.'],
      ['Connect two artifacts','Switch to Six Degrees and calculate the shortest relationship chain between two nodes.']
    ],
    sources:[
      ['GET /space/data/space.json','Provides dated leaves, categories, milestones, and relationship edges.','Live generated source'],
      ['<project>/.xo/timeline.jsonl','Durable normalized watcher history used by project APIs; it is related data but not the Atlas animation payload itself.','Portable project history'],
      ['#/six','Backward-compatible deep link for the Six Degrees child lens.','Route compatibility']
    ],
    steps:[
      ['Choose Timeline','Use the segmented control at the top of the view.'],
      ['Set a date','Drag the scrubber or press Play.'],
      ['Inspect a point','Hover or select an artifact to view its label and relationship context.'],
      ['Use Six Degrees','Pick From and To, connect them, then trace the path back on Timeline or Graph.']
    ],
    checks:[
      ['No dots','Artifacts require valid dates in the generated space payload.'],
      ['Trace missing','Open the cluster from Graph first or clear the existing trace and try again.'],
      ['No Six Degrees path','The two selected nodes are disconnected in the current graph dataset.'],
      ['Top tab stays Timeline','That is intentional: Six Degrees is a Timeline tool, not a separate top-level tab.']
    ],
    note:'Timeline’s visual artifact map and .xo/timeline.jsonl answer different questions: the former maps the workspace; the latter is the watcher’s durable event history.'
  },
  sessions:{
    tab:'sessions',
    name:'Sessions',
    kicker:'Tab guide · Multi-runtime telemetry',
    title:'Sessions: usage and runtime telemetry',
    intro:'Sessions combines the telemetry sources available on this machine. It summarizes tokens, cost availability, durations, models, tools, typed prompt exchanges, and trends without putting prompt text in the aggregate payload.',
    facts:['Claude Code + Codex + Cursor discovery','today / 7d / 30d / all','source filters + pagination','lazy prompt details'],
    jobs:[
      ['Measure usage','Compare fresh, output, cache-read, and cache-write tokens over a consistent date window.'],
      ['Inspect sessions','Filter and sort individual sessions, page through the newest rows, then open a focused telemetry summary.'],
      ['Follow exchanges','Open Prompts by turn to see each typed prompt with the replies and tool calls it initiated.'],
      ['Compare behavior','Review model mix, tool frequency, duration, available cost estimates, and trend heatmaps across runtimes.']
    ],
    sources:[
      ['GET /space/data/sessions.json','Discovers installed telemetry capabilities, merges healthy providers, and reports unavailable sources independently.','Aggregate metadata only'],
      ['GET /space/data/session_prompts.json','Reads one selected session transcript on demand; prompt text is never included in the aggregate response.','Lazy detail'],
      ['ARGUS_DB / CODEX_HOME / CURSOR_HOME','Optional runtime roots used by the Claude Code, Codex, and Cursor readers.','Read-only native data'],
      ['.xo and .quirq','Neither directory is the primary source for this tab.','Separate watcher stores']
    ],
    steps:[
      ['Choose sources','Toggle available runtimes. An unavailable badge means the native store could not be read, not that it contains zero sessions.'],
      ['Choose a window','Select Today, 7 days, 30 days, or All.'],
      ['Choose a lens','Use Overview, Sessions, Tools, Models, or Trends.'],
      ['Sort and select','Sort and page through session rows, then open the one that needs diagnosis.'],
      ['Inspect prompts','Prompt text loads only after you open a detail page and stays cached only for this browser tab.'],
      ['Refresh','Use Refresh after new telemetry has reached a native runtime store.']
    ],
    checks:[
      ['503 for all sources','Confirm at least one native runtime directory is mounted and readable inside the container.'],
      ['One source unavailable','The rest of the dashboard should keep working; verify that source’s configured root and mount.'],
      ['Zero vs unclassified','A runtime may report an authoritative session total without exposing the full fresh/output/cache breakdown.'],
      ['Cost unavailable','Codex and Cursor costs are shown as unavailable instead of a misleading $0; Argus values remain estimates, not invoices.'],
      ['Prompts unavailable','That runtime may not support prompt details, or its native transcript may have been cleaned up.'],
      ['New session missing','Wait for ingestion and the short server cache, then refresh.']
    ],
    note:'Sessions reads native runtime stores without modifying them. Projects remains the better tab for todos, live presence, and normalized .xo/.quirq history.'
  },
  projects:{
    tab:'projects',
    name:'Projects',
    kicker:'Tab guide · XO project state',
    title:'Projects: inspect durable work',
    intro:'Projects discovers folders under the configured XO root and combines portable .xo metadata with machine-local live presence. It is the most direct UI for watcher-derived project state.',
    facts:['filesystem discovery','portable .xo history','live .quirq presence','independent panels'],
    jobs:[
      ['Find a project','See every scaffolded and unscaffolded directory under the XO root.'],
      ['Review active work','Open a project to inspect todos and current sessions.'],
      ['Review recent history','Read the latest normalized timeline events without opening raw JSONL.']
    ],
    sources:[
      ['GET /api/xo-projects','Project identity and discovery from <XO root>/<project>/.xo/project.json.','Project list'],
      ['GET /api/xo-projects/{id}/todos','Reads portable <project>/.xo/todos.json.','Durable project work'],
      ['GET /api/xo-projects/{id}/timeline?limit=20','Reads portable <project>/.xo/timeline.jsonl.','Durable project history'],
      ['GET /api/xo-projects/{id}/activity','Reads .quirq/watcher/activity/projects/<id>.json.','Ephemeral live presence']
    ],
    steps:[
      ['Refresh projects','Fetch the latest root directory and project identity.'],
      ['Expand one row','Each drawer loads independently so one failed data source does not hide the others.'],
      ['Read lifecycle','Todos show pending, in-progress, completed, blocked, and cancelled work.'],
      ['Follow history','Use recent events for a bounded operational trail.']
    ],
    checks:[
      ['Project missing','Verify the XO root and ensure the folder is a direct child of it.'],
      ['Unscaffolded badge','The folder exists but lacks canonical project metadata.'],
      ['No open sessions','This is a valid live-presence zero, not proof that no historical work exists.'],
      ['Old activity.json','Legacy <project>/.xo/activity.json is ignored; current presence is in .quirq.']
    ],
    note:'The watcher owns .xo writes. Humans and agents should use documented APIs or native task tools rather than hand-editing these files.'
  },
  chat:{
    tab:'chat',
    name:'Chat',
    kicker:'Tab guide · Agent conversations',
    title:'Chat: run the active agent',
    intro:'Chat starts or resumes Plane-B agent sessions, streams assistant output, associates new work with a selected XO project, and keeps stop control close to the running request.',
    facts:['streaming SSE','new + resumed sessions','project-aware','abortable'],
    jobs:[
      ['Start a conversation','Choose a project and submit a prompt to the configured active backend.'],
      ['Resume work','Search or select an existing session and load its message history.'],
      ['Control a run','Watch streamed deltas and abort the active stream when needed.']
    ],
    sources:[
      ['GET /api/sessions and /api/sessions/search','Lists searchable runtime sessions.','Native runtime projection'],
      ['GET /api/messages/{id}','Loads message history for the selected native session.','Native runtime projection'],
      ['POST /api/chat/prompt + GET /api/chat/stream/{id}','Starts work and streams session-created, text, tool, and completion events.','Active agent'],
      ['GET /api/xo-projects','Supplies the project selector used for new work.','XO root']
    ],
    steps:[
      ['Check Setup','Select an agent backend and provide its required credentials or native login.'],
      ['Choose context','Pick a project or resume an existing session.'],
      ['Send','Submit a prompt and watch the event stream.'],
      ['Stop safely','Use Abort to terminate the active stream without inventing a new session.']
    ],
    checks:[
      ['No response','Setup may show a missing CLI, token, or provider credential.'],
      ['Session list empty','The active runtime may not have native sessions mounted.'],
      ['Project mismatch','Start the session from the intended project; project mapping drives watcher outputs.'],
      ['Stream interrupted','Reload the session list and inspect the session’s persisted message history.']
    ],
    note:'Chat messages remain in the runtime’s native store. The watcher derives compact project metadata; it does not copy full conversations into .xo.'
  },
  wiki:{
    tab:'wiki',
    name:'Wiki',
    kicker:'Tab guide · Local documentation',
    title:'Wiki: the operating manual',
    intro:'Wiki ships with the application and documents the exact storage, watcher, installation, flow, and tab contracts for this version. It works offline and requires no external documentation service.',
    facts:['versioned with code','offline','architecture + operations','one page per tab'],
    jobs:[
      ['Learn the boundaries','Start with Storage & data map before designing a new flow.'],
      ['Inspect a contract','Use the .xo and .quirq catalogs to identify writers, readers, and lifecycle.'],
      ['Operate a view','Open the matching Tab guide for its source APIs, controls, and troubleshooting.']
    ],
    sources:[
      ['space_ui/js/views/wiki.js','Contains the page catalog and rendered versioned articles.','Application source'],
      ['space_ui/css/wiki.css','Owns navigation, articles, tables, recipes, and responsive layout.','Application source'],
      ['Runtime APIs','Shown as documentation examples; Wiki itself does not fetch them.','Reference only']
    ],
    steps:[
      ['Choose a page','Use the left navigation grouped by Start here, Runtime systems, Data catalog, Design guide, and Tab guides.'],
      ['Follow locations','Paths explain ownership; API routes are the integration boundary.'],
      ['Cross-check a tab','Use Open tab at the bottom of a Tab guide.'],
      ['Keep docs current','Whenever a tab changes data source or behavior, update its guide in the same code change.']
    ],
    checks:[
      ['Page seems stale','Reload after a new container image; Wiki is versioned static application code.'],
      ['Path differs','Setup reports the actual host and container roots for this installation.'],
      ['Need raw secrets','Wiki intentionally documents secret handling without revealing saved values.'],
      ['Need a new guide','Add it to PAGES and ARTICLES so navigation and content remain coupled.']
    ],
    note:'Wiki explains contracts; live truth still comes from the relevant API and its freshness timestamps.'
  },
  quirq:{
    tab:'quirq',
    name:'Quirq',
    kicker:'Tab guide · Watcher storage',
    title:'Quirq: see both watcher destinations',
    intro:'Quirq is a privacy-aware operational map. It explicitly separates machine-local watcher state under .quirq from portable derived metadata under each XO project’s .xo directory.',
    facts:['two storage destinations','live refresh','values masked','filesystem structure only'],
    jobs:[
      ['See local control state','Inspect cursors, locks, and live activity under .quirq/watcher.'],
      ['See portable project output','Review which identity, session, todo, statistics, and timeline documents exist under project .xo directories.'],
      ['Diagnose storage drift','Spot legacy .xo/activity.json files and confirm current presence lives only in .quirq.']
    ],
    sources:[
      ['GET /api/quirq','Returns safe file metadata, watcher status, activity counts, root status, and .xo output contracts.','Privacy-aware catalog'],
      ['<Quirq root>/watcher/','Machine-local offsets, locks, and live activity snapshots.','Ephemeral watcher state'],
      ['<XO root>/<project>/.xo/','Portable identity, session indexes, todos, statistics, and history.','Durable watcher output'],
      ['<XO root>/.xo/','Workspace aggregates rebuilt from project-level .xo files.','Durable workspace rollup']
    ],
    steps:[
      ['Read the split map','Compare the blue machine-local side with the green portable project side.'],
      ['Check freshness','Use updated times and watcher status before treating a snapshot as current.'],
      ['Open project data','Jump to Projects for API-rendered todos, presence, and recent events.'],
      ['Inspect structure','Use State tree for current .quirq files; contents remain protected.']
    ],
    checks:[
      ['Legacy activity warning','The old file is residue; current watcher code does not write it.'],
      ['Missing .xo output','The project may be new, unscaffolded, or not yet mapped to a native session.'],
      ['No offsets.json','Some sources use other cursor types, or no supported records have been tailed yet.'],
      ['Credential count only','Values are deliberately write-only and remain masked.']
    ],
    note:'Use Quirq to understand where data lives. Use Projects to consume project state and Setup to change runtime behavior.'
  },
  setup:{
    tab:'secrets',
    name:'Setup',
    kicker:'Tab guide · Local runtime configuration',
    title:'Setup: configure this installation',
    intro:'Setup controls host storage roots, the active chat backend, watcher coverage and cadence, native runtime mounts, write-only credentials, and managed process restarts.',
    facts:['typed settings','write-only secrets','root-aware','restart truthful'],
    jobs:[
      ['Choose storage','View and configure the host XO projects root and machine-local .quirq root.'],
      ['Choose runtime behavior','Select the active agent, enable the watcher, set source coverage, and tune the tick interval.'],
      ['Connect credentials','Set, replace, or remove environment values without reading saved plaintext back.']
    ],
    sources:[
      ['GET/PUT /api/runtime-config','Reads and validates agent and watcher settings in .quirq/runtime.env.','Non-secret configuration'],
      ['PUT /api/runtime-config/roots','Writes desired host roots to .quirq/roots.env.','Installer configuration'],
      ['GET/PATCH/DELETE /api/secrets','Returns names/status and writes values to .quirq/secrets.env.','Write-only credentials'],
      ['POST /api/runtime-config/restart','Restarts only an installer-managed local container.','Managed process control']
    ],
    steps:[
      ['Confirm paths','Compare the configured roots with what the running server reports.'],
      ['Save roots','If roots differ, copy and run the one-command installer — roots are applied when the server starts.'],
      ['Save runtime','Review the pending-restart banner before applying process-time changes.'],
      ['Add credentials','Choose a manifest-recommended key, save it, then restart when requested.']
    ],
    checks:[
      ['Root not applied','Saving queues it; rerun the displayed installer to restart with the new roots.'],
      ['CLI unavailable','A manifest may support bootstrap, but required credentials must be present first.'],
      ['Sessions missing','Confirm the native runtime directory is mounted and watcher coverage includes it.'],
      ['Secret value hidden','That is intentional; replace the value or remove the variable.']
    ],
    note:'XO root changes select a project collection and never move project files. An empty new .quirq root receives a safe state copy; a non-empty root is never merged.'
  }
};

function tabGuideArticle(id){
  const guide=TAB_GUIDES[id];
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">${guide.kicker}</div>
        <h1>${guide.title}</h1>
        <p>${guide.intro}</p>
        <div class="wiki-facts">${guide.facts.map(fact=>`<span>${fact}</span>`).join('')}</div>
      </header>

      <section class="wiki-section">
        <h2>What this tab is for</h2>
        <div class="wiki-decision-list">
          ${guide.jobs.map(([title,text])=>`<div><b>${title}</b><p>${text}</p></div>`).join('')}
        </div>
      </section>

      <section class="wiki-section">
        <h2>Data sources and ownership</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table">
            <thead><tr><th>Source or location</th><th>What it supplies</th><th>Role</th></tr></thead>
            <tbody>${guide.sources.map(([source,what,role])=>`<tr><td><code>${wikiEsc(source)}</code></td><td>${what}</td><td>${role}</td></tr>`).join('')}</tbody>
          </table>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Recommended workflow</h2>
        <ol class="wiki-steps">
          ${guide.steps.map(([title,text])=>`<li><b>${title}</b><p>${text}</p></li>`).join('')}
        </ol>
      </section>

      <section class="wiki-section">
        <h2>Troubleshooting and interpretation</h2>
        <div class="wiki-check-grid">
          ${guide.checks.map(([title,text])=>`<div><b>${title}</b><p>${text}</p></div>`).join('')}
        </div>
      </section>

      <aside class="wiki-callout wiki-tab-callout">
        <div><b>Boundary to remember</b><p>${guide.note}</p></div>
        <button type="button" data-open-tab="${guide.tab}">Open ${guide.name} tab</button>
      </aside>
    </article>`;
}

function storageArticle(){
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">Start here · Storage architecture</div>
        <h1>One system, three data layers</h1>
        <p>Quirq does not replace an agent runtime’s conversation store. It
        derives a compact operational model from that store, keeps portable
        project knowledge in <code>.xo</code>, and keeps machine-specific
        control state in <code>~/.quirq</code>.</p>
        <div class="wiki-facts">
          <span>runtime = source of truth</span>
          <span>.xo = portable metadata</span>
          <span>.quirq = local control state</span>
          <span>APIs = safe projection</span>
        </div>
      </header>

      <section class="wiki-section">
        <h2>The full data path</h2>
        <div class="wiki-flow wiki-flow-five" aria-label="End-to-end data path">
          <div><small>01</small><b>Native runtime</b><span>Full messages, runtime session records, and tool payloads remain in Claude Code, OpenClaw, Hermes, or Antigravity storage.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>02</small><b>Source adapter</b><span>Reads only new records, assigns a project, and normalizes supported observations.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>03</small><b>Watcher sinks</b><span>Reduce events into indexes, counters, todos, timelines, and live presence.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>04</small><b>.xo + .quirq</b><span>Portable knowledge and local process state are written to different ownership boundaries.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>05</small><b>BFF APIs</b><span>Return stable frontend shapes and strip private paths or accumulator fields.</span></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>What belongs where</h2>
        <div class="wiki-layer-grid">
          <div class="wiki-layer-card">
            <small>Layer A · Runtime native</small>
            <h3>Conversation source of truth</h3>
            <code>~/.claude/ · ~/.openclaw/ · ~/.hermes/ · agy storage</code>
            <p>Full message text, native session identity, provider-specific
            tool payloads, and resume state. Owned by the runtime. Quirq reads
            supported records but does not relocate them.</p>
          </div>
          <div class="wiki-layer-card is-xo">
            <small>Layer B · Portable</small>
            <h3>Project and workspace metadata</h3>
            <code>&lt;project&gt;/.xo/ · ~/xo-projects/.xo/</code>
            <p>Identity, session indexes, derived counters, todos, timelines,
            peer/sync state, capabilities, and workspace rollups. It describes
            the work without becoming a second transcript store.</p>
          </div>
          <div class="wiki-layer-card is-quirq">
            <small>Layer C · Machine-local</small>
            <h3>Quirq control state</h3>
            <code>~/.quirq/</code>
            <p>Onboarding state, typed runtime configuration, watcher read
            cursors, advisory lock files, live presence, and credentials saved
            through Setup. It is meaningful only on this machine and
            must not be synced with a project.</p>
          </div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>The decision rule</h2>
        <div class="wiki-decision-list">
          <div><b>Does it explain the project later?</b><p>Put the derived,
          shareable representation in <code>.xo</code>: session index,
          outcome event, todo, or aggregate.</p></div>
          <div><b>Does it only coordinate this installation?</b><p>Put it in
          <code>.quirq</code>: byte cursor, process presence, lock, or
          installation preference. User-provided environment secrets also stay
          here because they belong to this installation, never a project.</p></div>
          <div><b>Is it the actual conversation or provider state?</b><p>Leave
          it in the runtime’s native store and expose it through the runtime
          adapter when needed.</p></div>
          <div><b>Will a browser or external client consume it?</b><p>Read it
          through an API. Do not make the frontend construct filesystem paths
          or depend on private on-disk fields.</p></div>
        </div>
      </section>

      <section class="wiki-section wiki-grid">
        <div>
          <h2>Privacy boundary</h2>
          <p>The watcher’s normalized events intentionally omit raw prompts,
          assistant prose, tool arguments, command text, and file contents.
          File activity is reduced to a project-relative path. Todo content is
          retained because it is itself the shared work contract.</p>
        </div>
        <div>
          <h2>Portability boundary</h2>
          <p><code>.xo</code> can contain project metadata and local absolute
          paths used internally by adapters. Public API presenters suppress
          those paths. <code>.quirq</code> is stricter: the directory itself
          never belongs in project backup, peer sync, or source control.</p>
        </div>
      </section>

      <aside class="wiki-callout">
        <b>Short mental model</b>
        <p>The runtime remembers the conversation. <code>.xo</code> remembers
        what the work means. <code>.quirq</code> remembers how this machine is
        keeping up.</p>
      </aside>
    </article>`;
}

function installationArticle(){
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">Start here · Docker installation</div>
        <h1>Run Quirq locally with Docker</h1>
        <p>Run one command, then open one URL. The launcher builds the image,
        mounts durable host data, starts Docker in the background, and waits
        until the API and Space UI are healthy.</p>
        <div class="wiki-facts">
          <span>Docker only</span>
          <span>no clone or checkout</span>
          <span>fixed localhost:5003</span>
          <span>host data persists</span>
        </div>
      </header>

      <section class="wiki-section">
        <h2>Prerequisites</h2>
        <div class="wiki-check-grid">
          <div><b>Docker</b><p>Docker Desktop or Docker Engine must be running.</p></div>
          <div><b>curl</b><p>Used once to stream the small installer into
          your shell.</p></div>
          <div><b>A coding runtime</b><p>Claude Code, OpenClaw, Hermes, or
          Antigravity is needed only for chat. The API, Wiki, and project
          views start without one.</p></div>
          <div><b>A projects directory</b><p>Defaults to
          <code>~/xo-projects</code>. It contains individual project folders
          and the workspace-level <code>.xo</code>.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>First installation</h2>
        <ol class="wiki-steps">
          <li><b>Pick a workspace directory.</b>
          <p>Run the installer from the directory you want as your
          workspace — the checkout lands beside your projects. Git is the
          only prerequisite.</p></li>
          <li><b>Run the installer.</b>
          <code>curl -fsSL https://www.quirq.ai/install | sh</code>
          <p>The command clones the server, prepares a Python environment,
          starts the server in the background, waits for health, and prints
          the browser URL.</p></li>
          <li><b>Open the workspace.</b>
          <code>http://localhost:5002/space/</code>
          <p>The same address is used every time. Re-run the same installer
          command whenever you want to update or restart.</p></li>
        </ol>
      </section>

      <section class="wiki-section">
        <h2>What the one command does</h2>
        <div class="wiki-flow wiki-flow-five" aria-label="Local installation flow">
          <div><small>01</small><b>Check Docker</b><span>Require a running
          Docker engine.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>02</small><b>Use port 5003</b><span>Publish container
          port 5002 only on host loopback at localhost:5003.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>03</small><b>Pull the image</b><span>Download the
          published amd64 or arm64 Quirq image from GHCR.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>04</small><b>Mount host data</b><span>Bind projects,
          <code>~/.quirq</code>, and manifest-declared native runtime stores
          to separate container paths.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>05</small><b>Wait for health</b><span>Run in the
          background and return only when Quirq is ready.</span></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Host ↔ container storage map</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table">
            <thead><tr><th>Host path</th><th>Container path</th><th>Purpose</th></tr></thead>
            <tbody>
              <tr><td>$XO_PROJECTS_ROOT or ~/xo-projects</td><td>/workspace/xo-projects</td><td>Project files and watcher-owned portable .xo metadata</td></tr>
              <tr><td>~/.quirq</td><td>/root/.quirq</td><td>Machine-local runtime configuration, credentials, onboarding, cursors, locks, and live presence</td></tr>
              <tr><td>Manifest-declared agent directories</td><td>Matching paths under /root</td><td>Native login, configuration, and session records; the installer does not mount the rest of the home directory</td></tr>
            </tbody>
          </table>
        </div>
        <p class="wiki-note">The API reports
        <code>/workspace/xo-projects</code>. That is the correct container-side
        path for the selected host project root.</p>
      </section>

      <section class="wiki-section">
        <h2>One stable local address</h2>
        <div class="wiki-decision-list">
          <div><b>Browser address</b><p>Always open
          <code>http://localhost:5003/space/</code>.</p></div>
          <div><b>Container address</b><p>The API still listens on port
          <code>5002</code> inside Docker. Compose maps host 5003 to it.</p></div>
          <div><b>Port 5002 is already busy</b><p>Nothing changes. Docker does
          not publish or take over host port 5002.</p></div>
          <div><b>Port 5003 is already busy</b><p>Startup fails clearly so it
          never replaces or stops another local service.</p></div>
        </div>
        <p>Native contributor mode remains separate and can still resolve its
        own development port.</p>
      </section>

      <section class="wiki-section">
        <h2>Verify the running container</h2>
        <div class="wiki-recipe">
          <div class="wiki-recipe-step"><small>1</small><b>Health</b>
          <code>curl http://127.0.0.1:5003/health</code>
          <p>Use the selected port and expect
          <code>"status":"healthy"</code>.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>2</small><b>XO root</b>
          <code>curl http://127.0.0.1:5003/api/config/workspace</code>
          <p><code>roots[default]</code> should be
          <code>/workspace/xo-projects</code>.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>3</small><b>Projects</b>
          <code>curl http://127.0.0.1:5003/api/xo-projects</code>
          <p>Confirm the expected project ids are discovered.</p></div>
        </div>
      </section>

      <section class="wiki-section wiki-grid">
        <div>
          <h2>Stop and clean up</h2>
          <code>docker stop quirq</code>
          <p>This stops the installed container. Bind-mounted projects and
          <code>~/.quirq</code> remain on the host.</p>
        </div>
        <div>
          <h2>Native contributor mode</h2>
          <code>./cowork-api.sh dev</code>
          <p>This alternate path creates a host <code>venv</code>, installs
          dependencies, enables reload, and uses the same port fallback.</p>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Coding-runtime boundary</h2>
        <p>The image installs the API, not every agent CLI. Space, project
        APIs, the Wiki, and watcher metadata work in Docker. CLI-backed chat
        requires the runtime inside the container. An HTTP gateway running on
        the host must be addressed through
        <code>host.docker.internal</code>, not container loopback.</p>
        <p>Host credential directories are deliberately not mounted by
        default. Adding that access should be an explicit security decision,
        not an installation side effect.</p>
      </section>

      <aside class="wiki-callout">
        <b>Versioned source document</b>
        <p>The complete Docker workflow and troubleshooting guide is maintained
        in <code>INSTALLATION.md</code>. The XO projects directory and
        <code>~/.quirq</code> remain separate host mounts by design.</p>
      </aside>
    </article>`;
}

function watcherArticle(){
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">Runtime systems · Watcher</div>
        <h1>How the watcher works</h1>
        <p>The watcher is a configurable, non-fatal projection loop. It can
        tail only the selected runtime or combine every mounted supported
        runtime, converts records into a small event vocabulary, and fans those
        events into independently owned documents.</p>
        <div class="wiki-facts">
          <span>0.25–60 second polling loop</span>
          <span>active or all mounted sources</span>
          <span>atomic snapshots</span>
          <span>append-only timelines</span>
        </div>
      </header>

      <section class="wiki-section">
        <h2>One tick, in order</h2>
        <ol class="wiki-steps">
          <li><b>Drain configured sources.</b><p><code>AGENT_NAME</code>
          still chooses the chat backend. The Setup tab separately chooses
          active-only or all-mounted watcher mode; each source tails native
          files or polls a database and emits normalized events only for
          sessions mapped to XO projects.</p></li>
          <li><b>Refresh the model cache.</b><p><code>UsageObserved</code>
          events update an in-memory session-to-model map. Presence uses this
          map because the activity schema requires an agent/model identity.</p></li>
          <li><b>Group by project.</b><p>Events without a resolved
          <code>project_id</code> do not enter project sinks. This prevents
          unrelated runtime conversations from polluting project history.</p></li>
          <li><b>Run project sinks.</b><p>Identity is filled first; session
          augmentation, todos, stats, and timeline follow. Each sink owns its
          document and uses atomic replacement or append-only JSONL.</p></li>
          <li><b>Refresh presence.</b><p>The source takes a fresh process
          snapshot. Every discovered project gets a new activity file, even
          when empty, so exited sessions disappear promptly.</p></li>
          <li><b>Rebuild workspace views.</b><p>The watcher writes project
          discovery and unions project sessions, augment data, stats, and live
          activity. Timeline events are tagged with <code>project_id</code> as
          they are emitted.</p></li>
        </ol>
      </section>

      <section class="wiki-section">
        <h2>Normalized events and their destinations</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table">
            <thead><tr><th>Observation</th><th>Retained data</th><th>Destinations</th></tr></thead>
            <tbody>
              <tr><td>SessionFirstSeen</td><td>time, runtime, native session id, project</td><td>session augment, todos session bucket, stats timing, timeline</td></tr>
              <tr><td>MessageObserved</td><td>role and time; no message text</td><td>message counters, daily message buckets, activity timing</td></tr>
              <tr><td>UsageObserved</td><td>input/output/cache tokens, model, optional response latency</td><td>stats, per-model rollups, in-memory presence model cache</td></tr>
              <tr><td>ToolUseObserved</td><td>tool name only; no arguments</td><td>tool call counters and per-tool analytics</td></tr>
              <tr><td>TaskCreated / changed</td><td>id, content, description, active form, status</td><td>todos, task counters, added/completed timeline events</td></tr>
              <tr><td>FileTouched</td><td>project-relative path and created/edited flag</td><td>unique-file stats and file timeline events</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Runtime coverage is intentionally honest</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table wiki-matrix">
            <thead><tr><th>Runtime</th><th>Messages</th><th>Tokens</th><th>Tools</th><th>Files</th><th>Tasks</th><th>Presence</th></tr></thead>
            <tbody>
              <tr><td>Claude Code</td><td>yes</td><td>yes</td><td>yes</td><td>yes</td><td>native task pairing</td><td>PID session files</td></tr>
              <tr><td>OpenClaw</td><td>yes</td><td>yes</td><td>yes</td><td>not yet</td><td>todo API</td><td>not yet</td></tr>
              <tr><td>Hermes</td><td>yes</td><td>not exposed</td><td>yes</td><td>not yet</td><td>todo API</td><td>not available</td></tr>
              <tr><td>Antigravity</td><td>yes</td><td>separate usage capability</td><td>yes</td><td>supported write tools</td><td>todo API</td><td>short-lived process</td></tr>
            </tbody>
          </table>
        </div>
        <p class="wiki-note">An empty value is preferable to invented telemetry.
        Pages and flows should display “not available” separately from a real
        numeric zero.</p>
      </section>

      <section class="wiki-section wiki-grid">
        <div>
          <h2>Atomicity and coordination</h2>
          <p>JSON snapshots are written to a temporary sibling, flushed, and
          replaced. Timelines append complete JSON lines. Because both the
          watcher and todo API can update <code>todos.json</code>, they share
          advisory locks under <code>~/.quirq/watcher/locks/</code>.</p>
        </div>
        <div>
          <h2>Failure behavior</h2>
          <p>A source, project sink batch, presence sink, or workspace
          aggregation can fail without stopping FastAPI. The error is logged
          and the next tick retries. Readers therefore need to tolerate a
          temporarily stale snapshot.</p>
        </div>
      </section>

      <aside class="wiki-callout">
        <b>Ownership rule</b>
        <p>Agents do not edit watcher files. Use native task tools or the todo
        API for mutations, and use the visualizer APIs for reads.</p>
      </aside>
    </article>`;
}

function xoDataArticle(){
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">Data catalog · Portable metadata</div>
        <h1>Everything in <code>.xo</code></h1>
        <p>There are two tiers: one <code>.xo</code> inside each project and
        one at the projects root. The project tier describes one body of work;
        the workspace tier is a materialized cross-project view.</p>
        <div class="wiki-facts">
          <span>service-owned</span>
          <span>project tier</span>
          <span>workspace tier</span>
          <span>not a transcript store</span>
        </div>
      </header>

      <section class="wiki-section">
        <h2>Project tier · <code>&lt;project&gt;/.xo/</code></h2>
        <div class="wiki-file-list">
          <article class="wiki-file">
            <header><code>project.json</code><span>scaffold + watcher</span></header>
            <p>Stable project identity. The template starts with
            <code>_template: true</code>; first watcher discovery fills
            <code>schema</code>, UUID <code>pid</code>, <code>name</code>,
            <code>owner_user_id</code>, and <code>created_at</code>.</p>
            <dl><div><dt>Used for</dt><dd>project discovery, ownership, display identity</dd></div><div><dt>Lifecycle</dt><dd>one-time fill; name may later be changed through product flows</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>agent.json</code><span>optional · adapter-owned</span></header>
            <p>Backend-specific agent attachment for adapters that model an
            agent as an XO project. Common values include id, display name,
            description, backend, and creation time.</p>
            <dl><div><dt>Used for</dt><dd>agent sidebar and agent detail routes</dd></div><div><dt>Present when</dt><dd>a supporting adapter creates or attaches an agent</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>sessions/sessionslist.json</code><span>adapter-owned</span></header>
            <p>A flat map keyed by a composite cowork session key. Each row
            carries <code>sessionId</code>, <code>nativeSessionId</code>,
            absolute <code>directory</code>, <code>backend</code>,
            <code>updatedAt</code>, and optional cumulative token/cost usage.</p>
            <dl><div><dt>Used for</dt><dd>session discovery, resume lookup, usage summaries, mapping runtime logs to projects</dd></div><div><dt>API safety</dt><dd>the absolute directory is not exposed by visualizer presenters</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>sessions/sessions-augment.json</code><span>watcher-owned</span></header>
            <p>Fields the adapter index does not own: message totals and
            role split, tool calls, task counts by status, first/last
            activity, <code>ended_at</code>, and episodic memory references.
            A private <code>_task_states</code> map preserves correct task
            transitions across restarts.</p>
            <dl><div><dt>Join key</dt><dd>the same composite key as sessionslist whenever available</dd></div><div><dt>Read behavior</dt><dd>BFF merges base and augment rows; unmatched augment rows are dropped</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>todos.json</code><span>watcher + todo API</span></header>
            <p>Session buckets containing runtime, optional native source
            path, session start time, and todos. Todo fields include id,
            content, status, optional description, and optional active form.</p>
            <dl><div><dt>Status values</dt><dd>pending, in_progress, completed, cancelled, blocked</dd></div><div><dt>API safety</dt><dd>source_file is always returned as null</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>stats.json</code><span>watcher-owned</span></header>
            <p>Rolling 7-day and 30-day totals plus
            <code>by_session</code>, <code>by_runtime</code>, and up to about
            35 UTC days in <code>by_day</code>. Tracks tokens, models, tool
            counts, files, durations, messages, cache tokens, and bounded
            response-latency samples when the runtime provides them.</p>
            <dl><div><dt>Private fields</dt><dd>_session_totals and _by_day_totals make incremental updates restart-safe</dd></div><div><dt>API safety</dt><dd>presenters project only named public fields</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>timeline.jsonl</code><span>watcher-owned</span></header>
            <p>Append-only, one JSON object per line. Current watcher events
            include session started, todo added/completed, and file
            created/edited. Each record has time, type, session id, runtime,
            and event-specific safe fields.</p>
            <dl><div><dt>Retention</dt><dd>rotates at 8 MB; keeps five timestamped project rotations</dd></div><div><dt>Read pattern</dt><dd>newest-first API pagination with optional type filters</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>peers.json</code><span>collaboration-owned</span></header>
            <p>Human collaborator roster: user id, owner/collaborator/viewer
            role, add time, and optional endpoint and label. An empty list
            means the project is currently solo.</p>
            <dl><div><dt>Not derived from</dt><dd>runtime logs</dd></div><div><dt>Do not</dt><dd>invent peers from open sessions</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>sync.json</code><span>sync service-owned</span></header>
            <p>Per-peer synchronization state: vector clock, manifest hash,
            last pull/push timestamps, pending outbox count, and overall last
            sync time.</p>
            <dl><div><dt>Used for</dt><dd>conflict-aware project synchronization</dd></div><div><dt>Not activity</dt><dd>it describes replication progress, not current presence</dd></div></dl>
          </article>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Workspace tier · <code>~/xo-projects/.xo/</code></h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table">
            <thead><tr><th>File</th><th>What it contains</th><th>How it is produced</th></tr></thead>
            <tbody>
              <tr><td><code>workspace.json</code></td><td>schema, update time, projects root, sorted discovered project ids</td><td>rebuilt every watcher tick</td></tr>
              <tr><td><code>sessions/sessionslist.json</code></td><td>union of every project’s adapter session rows</td><td>rebuilt every tick</td></tr>
              <tr><td><code>sessions/sessions-augment.json</code></td><td>union of watcher session enrichments</td><td>rebuilt every tick</td></tr>
              <tr><td><code>stats.json</code></td><td>summed project windows, runtimes, sessions, days, models, tools, and latency</td><td>recomputed from project stats</td></tr>
              <tr><td><code>timeline.jsonl</code></td><td>project events plus <code>project_id</code></td><td>appended during each project sink batch; no workspace rotation yet</td></tr>
              <tr><td><code>xo.json</code></td><td>active agent capability flags and supported live model/channel status</td><td>written at server startup and patched by status probes</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="wiki-section wiki-grid">
        <div>
          <h2>Legacy compatibility</h2>
          <p>Some session readers accept the former
          <code>sessions/sessions.json</code> index when
          <code>sessionslist.json</code> is absent. New writes target
          <code>sessionslist.json</code>. Project and workspace
          <code>.xo/activity.json</code> files are no longer scaffolded,
          written, or read.</p>
        </div>
        <div>
          <h2>What is not here</h2>
          <p>Full prompt/response transcripts, file contents, tool arguments,
          watcher byte cursors, lock state, and process heartbeats do not
          belong in project <code>.xo</code>.</p>
        </div>
      </section>

      <aside class="wiki-callout">
        <b>Read, do not hand-edit</b>
        <p>The service, adapters, todo API, collaboration service, and sync
        service own these documents. Direct edits can be overwritten or break
        writer coordination.</p>
      </aside>
    </article>`;
}

function quirqDataArticle(){
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">Data catalog · Machine-local state</div>
        <h1>Everything in <code>~/.quirq</code></h1>
        <p>This directory helps one Quirq installation operate safely and
        resume efficiently. It is not project memory and is never a source for
        backup, collaboration, or cross-machine history.</p>
        <div class="wiki-facts">
          <span>local only</span>
          <span>contains secrets</span>
          <span>no transcripts</span>
          <span>never project-synced</span>
        </div>
      </header>

      <section class="wiki-section">
        <h2>Directory map</h2>
        <pre class="wiki-tree">~/.quirq/
├── state.json
├── runtime.env                 # mode 0600; typed restart-time controls
├── roots.env                   # mode 0600; next host bind-mount roots
├── secrets.env                 # mode 0600; write-only credentials from Setup
└── watcher/
    ├── offsets.json
    ├── hermes-offsets.json       # only when Hermes is active
    ├── locks/
    │   └── todos.json.&lt;hash&gt;.lock
    └── activity/
        ├── projects/
        │   └── &lt;project-id&gt;.json
        └── workspace.json</pre>
      </section>

      <section class="wiki-section">
        <h2>File-by-file catalog</h2>
        <div class="wiki-file-list">
          <article class="wiki-file">
            <header><code>state.json</code><span>installation state</span></header>
            <p>Currently stores <code>onboarding_completed</code> and
            <code>onboarding_completed_at</code>. Disk persistence prevents
            first-run onboarding from returning after browser storage is
            cleared or an incognito window is used.</p>
            <dl><div><dt>Writer</dt><dd>onboarding API</dd></div><div><dt>Scope</dt><dd>one machine / one local service user</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>roots.env</code><span>installer root configuration</span></header>
            <p>Stores the absolute host paths selected for the XO projects
            root and machine-local Quirq state root. These values are
            intentionally separate from process runtime settings: the running
            server cannot change its own roots, so Setup marks them pending
            until the one-command installer restarts it with the new
            values.</p>
            <dl><div><dt>Writer</dt><dd>typed root configuration API</dd></div><div><dt>Migration</dt><dd>empty state targets receive a copy; project roots are selected, never moved</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>runtime.env</code><span>validated process configuration</span></header>
            <p>Stores only allowlisted non-secret controls selected in Setup:
            active agent backend, whether the watcher runs, whether it combines
            every mounted source, and the watcher tick interval. The page
            compares saved values with the running process and shows a pending
            restart instead of claiming they applied live.</p>
            <dl><div><dt>Writer</dt><dd>typed runtime configuration API</dd></div><div><dt>Apply</dt><dd>loaded before agent adapters at the next process start</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>secrets.env</code><span>sensitive environment values</span></header>
            <p>Stores the key/value pairs saved through the Setup tab. List
            APIs return names and masked status only; the tab never reads saved
            plaintext back. The file is written atomically with owner-only
            permissions and loaded when Quirq starts.</p>
            <dl><div><dt>Writer</dt><dd>curated secrets API</dd></div><div><dt>Delete effect</dt><dd>removes the value from disk and from new child-process environments</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>watcher/offsets.json</code><span>JSONL cursor store</span></header>
            <p>A map from absolute native log path to
            <code>{offset, inode}</code>. The byte offset says where the next
            tail starts; inode detects rotation or replacement. It contains
            file locations, not log contents.</p>
            <dl><div><dt>Why it matters</dt><dd>prevents re-reading and double-counting old runtime events after restart</dd></div><div><dt>Recovery</dt><dd>missing/corrupt means replay from byte zero; sinks provide partial idempotency</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>watcher/hermes-offsets.json</code><span>Hermes cursor store</span></header>
            <p>Maps <code>&lt;profile&gt;:&lt;session-id&gt;</code> to the most
            recent SQLite message row id. Hermes uses database row cursors
            because its native history is SQLite rather than JSONL.</p>
            <dl><div><dt>Present when</dt><dd>the Hermes visualizer source observes mapped sessions</dd></div><div><dt>Contains</dt><dd>cursor integers, not messages or token data</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>watcher/locks/*.lock</code><span>coordination sentinels</span></header>
            <p>Empty advisory lock files for data with multiple writers,
            currently project <code>todos.json</code>. The filename combines
            the guarded basename with an eight-character hash of its absolute
            path, keeping different projects separate.</p>
            <dl><div><dt>Lifetime</dt><dd>files may remain; the kernel releases the actual lock when the descriptor closes</dd></div><div><dt>Timeout</dt><dd>bounded wait prevents a stalled writer from wedging an API call</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>watcher/activity/projects/&lt;id&gt;.json</code><span>live project presence</span></header>
            <p>A heartbeat snapshot with schema and update time plus open
            sessions. Each row contains native session id, runtime, model,
            local user id, opened time, last activity time, and optional host.</p>
            <dl><div><dt>Refresh</dt><dd>every watcher tick for every discovered project</dd></div><div><dt>Meaning</dt><dd>“observably open now,” not historical work or a durable audit record</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>watcher/activity/workspace.json</code><span>live workspace presence</span></header>
            <p>The union of all project activity rows. It uses the same
            activity schema and adds <code>project_id</code> to each session
            row so workspace UIs can group live work.</p>
            <dl><div><dt>Read API</dt><dd>GET /api/xo-projects/activity</dd></div><div><dt>Project API</dt><dd>GET /api/xo-projects/{project_id}/activity</dd></div></dl>
          </article>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Rename and migration behavior</h2>
        <div class="wiki-decision-list">
          <div><b>All new writes use <code>~/.quirq</code>.</b><p>Activity,
          locks, onboarding, shared JSONL offsets, and Hermes offsets no
          longer target <code>~/.xo-cowork</code>.</p></div>
          <div><b>Important cursors migrate safely.</b><p>If the new file is
          absent, valid legacy onboarding and offset state is read once and
          rewritten under <code>.quirq</code>. This avoids onboarding resets
          and accidental runtime-log replay.</p></div>
          <div><b>Ephemeral state is regenerated.</b><p>Presence and advisory
          lock files are created fresh in <code>.quirq</code>; old copies are
          not authoritative.</p></div>
          <div><b>The old directory is not auto-deleted.</b><p>Leaving it
          untouched makes rollback safe. Once the new installation has run
          successfully, it is merely legacy residue.</p></div>
        </div>
      </section>

      <section class="wiki-section wiki-grid">
        <div>
          <h2>What it does collect</h2>
          <p>Local onboarding flags, runtime-log file paths, byte/inode or row
          cursors, hashed lock identifiers, session/runtime/model/user
          identity, live timing metadata, and environment values explicitly
          saved by the user through Setup.</p>
        </div>
        <div>
          <h2>What it does not collect</h2>
          <p>Prompt text, assistant responses, file contents, tool arguments,
          project plans, durable todo history, peer rosters, or sync manifests.
          It does not discover or copy credentials from other applications.</p>
        </div>
      </section>

      <aside class="wiki-callout">
        <b>Reset semantics</b>
        <p>Deleting <code>.quirq</code> discards local onboarding and watcher
        progress, so the watcher may replay native records. It also permanently
        deletes runtime configuration and credentials saved through Setup. It does not delete project work
        or the runtime’s original conversations.</p>
      </aside>
    </article>`;
}

function collaborationArticle(){
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">Design guide · Collaboration architecture</div>
        <h1>Collaborative version history</h1>
        <p>Build Google Docs-style collaboration around logical Quirq
        documents, not by synchronizing the entire <code>~/.quirq</code>
        directory. Real-time merge, named versions, restore, attribution,
        comments, permissions, presence, and disaster recovery are related
        features, but each needs its own storage and lifecycle.</p>
        <div class="wiki-facts">
          <span>CRDT live merge</span>
          <span>named snapshots</span>
          <span>server attribution</span>
          <span>local state stays local</span>
        </div>
      </header>

      <aside class="wiki-callout">
        <b>Do not version the directory</b>
        <p><code>.quirq</code> contains machine paths, process locks, replay
        cursors, presence heartbeats, and credentials. Synchronizing it as a
        file tree could leak secrets, cause one machine to skip watcher input,
        or make another machine use invalid paths. Create a shared
        collaboration store for selected human-authored documents and leave
        <code>.quirq</code> as reconstructable local control state.</p>
      </aside>

      <section class="wiki-section">
        <h2>What “Google Docs-style” means</h2>
        <div class="wiki-check-grid">
          <div><b>Concurrent editing</b><p>Several people edit the same
          logical document at once without locking a whole file or resolving
          Git conflict markers.</p></div>
          <div><b>Live awareness</b><p>Avatars, cursors, selection ranges, and
          connection status show who is present now. This state expires when
          a collaborator disconnects.</p></div>
          <div><b>Automatic history</b><p>Edits are autosaved and grouped into
          understandable revisions rather than presenting every keystroke as
          a separate user-facing version.</p></div>
          <div><b>Named versions</b><p>A user can mark meaningful states such
          as “Approved flow” or “Release configuration” and retain them longer
          than routine autosaves.</p></div>
          <div><b>Review and restore</b><p>Users preview, compare, copy, and
          restore earlier versions while preserving the history that followed
          them.</p></div>
          <div><b>Collaboration controls</b><p>Viewer, commenter, and editor
          permissions combine with comments, assignments, attribution, and a
          later suggestion workflow.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Classify current Quirq data first</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table wiki-matrix">
            <thead><tr><th>Current data</th><th>Version collaboratively?</th><th>Correct treatment</th></tr></thead>
            <tbody>
              <tr><td><code>state.json</code></td><td>No</td><td>Local
              onboarding state. A machine backup may retain it, but peers
              should not merge it.</td></tr>
              <tr><td><code>runtime.env</code></td><td>Not directly</td><td>
              Keep applied values local. A separate safe runtime-profile
              document may provide shared defaults, with explicit machine
              overrides.</td></tr>
              <tr><td><code>roots.env</code></td><td>No</td><td>Absolute host
              paths are meaningful only on the machine that owns them.</td></tr>
              <tr><td><code>secrets.env</code></td><td>Never in document
              history</td><td>Put shared values in Vault or another secret
              manager. Collaborative configuration stores secret reference IDs
              only, never plaintext credentials.</td></tr>
              <tr><td><code>watcher/*offsets.json</code></td><td>No</td><td>
              Byte, inode, or row cursors prevent local replay. Sharing them
              can cause missed or duplicated ingestion.</td></tr>
              <tr><td><code>watcher/locks/*.lock</code></td><td>No</td><td>
              Ephemeral process coordination. The kernel lock, not file
              history, is authoritative.</td></tr>
              <tr><td><code>watcher/activity/**</code></td><td>Presence only</td><td>
              Broadcast through awareness with a short TTL. Do not create a
              durable revision from every watcher heartbeat.</td></tr>
              <tr><td>Historical activity</td><td>Yes, as events</td><td>
              Append meaningful session start, end, todo, and file events to
              the durable timeline or event store.</td></tr>
              <tr><td>Wiki, flows, todos, safe metadata</td><td>Yes</td><td>
              Model these as independent collaborative documents with live
              merge, versions, comments, validation, and permissions.</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Recommended architecture</h2>
        <div class="wiki-flow wiki-flow-five" aria-label="Collaborative document architecture">
          <div><small>01</small><b>Browser or agent</b><span>Edits a Yjs
          document and keeps an offline IndexedDB copy.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>02</small><b>FastAPI authorization</b><span>Issues a
          short-lived token scoped to workspace, document, user, and
          capability.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>03</small><b>Hocuspocus WebSocket</b><span>Authenticates,
          merges, broadcasts, and attributes Yjs binary updates.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>04</small><b>PostgreSQL</b><span>Persists documents,
          incremental updates, named snapshots, comments, membership, and
          audit events.</span></div>
          <i aria-hidden="true">→</i>
          <div><small>05</small><b>.xo materializer</b><span>Validates current
          state and atomically writes compatible project read models for
          existing APIs and tools.</span></div>
        </div>
        <p class="wiki-note">Redis is optional and distributes awareness and
        updates between multiple Hocuspocus instances. It is not durable
        storage. Secrets remain in a secret manager, while local device
        configuration and watcher infrastructure remain in
        <code>~/.quirq</code>.</p>
      </section>

      <section class="wiki-section">
        <h2>Why Yjs + Hocuspocus + PostgreSQL</h2>
        <div class="wiki-file-list">
          <article class="wiki-file">
            <header><code>Yjs</code><span>conflict-free document model</span></header>
            <p>Yjs updates are compact binary messages that can be applied in
            any order and more than once. <code>Y.Text</code> and
            <code>Y.XmlFragment</code> fit wiki content, while
            <code>Y.Map</code> and <code>Y.Array</code> fit flow nodes, edges,
            todos, and structured metadata. State vectors exchange only
            missing changes.</p>
            <dl><div><dt>Offline</dt><dd>IndexedDB keeps previously opened
            documents available and uploads missing changes after
            reconnect.</dd></div><div><dt>Anchors</dt><dd>Relative positions
            keep cursors and comment ranges attached through concurrent text
            edits.</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>Hocuspocus</code><span>self-hosted synchronization</span></header>
            <p>A dedicated TypeScript WebSocket service handles Yjs protocol
            traffic, awareness, authentication hooks, read-only clients,
            persistence hooks, and later horizontal scaling. FastAPI remains
            the source of truth for users, projects, roles, and document
            authorization.</p>
            <dl><div><dt>Persistence rule</dt><dd>Store the exact Yjs
            <code>Uint8Array</code>. Recreating a Yjs document from exported
            JSON starts a different history and can duplicate merged
            content.</dd></div><div><dt>Deployment</dt><dd>Run it beside the
            existing FastAPI service in the one-command Docker stack.</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>PostgreSQL</code><span>durable control plane</span></header>
            <p>Stores opaque CRDT updates and snapshots together with trusted
            relational metadata: document identity, actor, membership,
            retention, audit, version labels, and materialization status.</p>
            <dl><div><dt>Isolation</dt><dd>Workspace-scoped authorization can
            be reinforced with row-level security.</dd></div><div><dt>Recovery</dt><dd>
            Database backups and write-ahead-log recovery protect
            infrastructure independently from product version history.</dd></div></dl>
          </article>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Keep three different histories</h2>
        <div class="wiki-file-list">
          <article class="wiki-file">
            <header><code>collab_updates</code><span>Synchronization history</span></header>
            <p>An append-only sequence of Yjs binary updates used for merge,
            reconnect, and forensic recovery. Each server envelope records
            document id, server sequence, authenticated actor, connection
            session, receive time, schema version, client version, and
            checksum.</p>
            <dl><div><dt>Audience</dt><dd>Synchronization service and
            operators</dd></div><div><dt>Not suitable for</dt><dd>A human
            revision list; individual transactions are too granular and
            binary diffs are not semantic explanations.</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>collab_versions</code><span>User-visible version history</span></header>
            <p>Periodic and named document snapshots used by the History
            drawer. Store the through-sequence, binary snapshot, state vector,
            name, reason, creator, contributors, creation time, document
            schema, and checksum.</p>
            <dl><div><dt>Automatic policy</dt><dd>Group active changes into
            useful intervals and always snapshot before import, migration,
            large agent edit, or restore.</dd></div><div><dt>Named policy</dt><dd>
            Retain named milestones longer than routine automatic
            revisions.</dd></div></dl>
          </article>

          <article class="wiki-file">
            <header><code>database backups</code><span>Operational disaster recovery</span></header>
            <p>Encrypted backups and PostgreSQL point-in-time recovery protect
            the entire service after deletion, corruption, or infrastructure
            failure. They are operator tools, not versions exposed in the
            document editor.</p>
            <dl><div><dt>Restore scope</dt><dd>Database or service
            infrastructure</dd></div><div><dt>Why separate</dt><dd>A product
            version should restore one logical document without rolling back
            unrelated users or workspaces.</dd></div></dl>
          </article>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Restore and attribution semantics</h2>
        <div class="wiki-decision-list">
          <div><b>Restore as a new latest version.</b><p>First preserve the
          current state, then create a new head containing the selected older
          content. Never erase the versions that occurred after it.</p></div>
          <div><b>Preview without joining the live document.</b><p>Load a
          selected snapshot into a temporary read-only document so browsing
          history cannot modify current collaborators’ state.</p></div>
          <div><b>Authenticate attribution on the server.</b><p>Bind every
          update to the token subject and connection context. Do not trust a
          user id sent inside a client update or awareness payload.</p></div>
          <div><b>Produce semantic diffs.</b><p>Materialize two versions to
          rich text or typed JSON. Compare wiki text as editor content and
          flows or todos by stable object id and field, not as opaque binary
          updates.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Suggested collaboration tables</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table wiki-matrix">
            <thead><tr><th>Store</th><th>Responsibility</th><th>Important fields</th></tr></thead>
            <tbody>
              <tr><td><code>collab_documents</code></td><td>One row per
              independently authorized artifact.</td><td>workspace, project,
              kind, schema version, created by, timestamps</td></tr>
              <tr><td><code>collab_updates</code></td><td>Append-only CRDT
              synchronization envelope.</td><td>document, sequence, binary
              update, actor, session, received at, checksum</td></tr>
              <tr><td><code>collab_versions</code></td><td>Automatic and named
              history snapshots.</td><td>through sequence, snapshot, state
              vector, name, reason, contributors</td></tr>
              <tr><td><code>collab_comments</code></td><td>Threads and
              assignments anchored to content.</td><td>relative anchor or
              object id, author, replies, resolved state, mentions</td></tr>
              <tr><td><code>collab_members</code></td><td>Document or
              workspace authorization.</td><td>user or group, owner/editor/
              commenter/viewer role</td></tr>
              <tr><td><code>collab_audit_events</code></td><td>Security and
              administrative actions.</td><td>share, export, import, restore,
              delete, permission change, actor, time</td></tr>
              <tr><td><code>collab_materializations</code></td><td>Tracks
              compatible <code>.xo</code> read models.</td><td>document,
              source version, output path, checksum, status, error</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Document boundaries</h2>
        <div class="wiki-grid">
          <div>
            <h2>Use small logical documents</h2>
            <p>Create one document per wiki page, one per flow, one
            project-level todo document, one editable project-metadata
            document, and one safe shared runtime profile. This keeps loading,
            authorization, history, retention, and restore independent.</p>
          </div>
          <div>
            <h2>Avoid a workspace-sized document</h2>
            <p>Do not place the whole workspace or filesystem in one Yjs
            document. A monolith makes every client load unrelated content,
            couples permissions, and turns a page restore into a workspace
            restore.</p>
          </div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Relationship with <code>.xo</code></h2>
        <ol class="wiki-steps">
          <li><b>Edit the logical document</b><p>A browser or authorized agent
          changes a Yjs wiki, flow, todo, or metadata document rather than
          writing the filesystem directly.</p></li>
          <li><b>Persist and broadcast</b><p>The collaboration service saves
          the binary update with trusted attribution and sends it to connected
          peers.</p></li>
          <li><b>Validate resolved state</b><p>A domain materializer checks
          schema and business invariants. CRDT convergence prevents merge
          conflicts but does not guarantee valid status values, ownership, or
          product rules.</p></li>
          <li><b>Write a compatible read model</b><p>The service atomically
          produces the current <code>.xo</code> representation for existing
          APIs and local tools, including its source collaboration
          revision.</p></li>
          <li><b>Keep writer ownership explicit</b><p>Collaborative documents
          and watcher-derived files must not compete for the same fields.
          Session indexes, stats, and workspace aggregates remain
          service-derived.</p></li>
        </ol>
        <p class="wiki-note"><code>timeline.jsonl</code> remains an append-only
        event history. <code>stats.json</code>, session indexes, and workspace
        aggregates remain derived and recomputable rather than collaboratively
        edited.</p>
      </section>

      <section class="wiki-section">
        <h2>History and collaboration UI</h2>
        <div class="wiki-check-grid">
          <div><b>Sync state</b><p>Show saved locally, syncing, synced, and
          offline states without blocking local edits.</p></div>
          <div><b>People</b><p>Show connected avatars, cursors, selections,
          and a collaborator list sourced from ephemeral awareness.</p></div>
          <div><b>History drawer</b><p>Group versions by date and editing
          session, show contributors, and filter to named versions.</p></div>
          <div><b>Version actions</b><p>Provide name, preview, compare, restore,
          make a copy, and permission-controlled delete or retention
          actions.</p></div>
          <div><b>Comments</b><p>Anchor threads to wiki ranges or stable flow
          and todo ids; support replies, mentions, assignments, resolve, and
          reopen.</p></div>
          <div><b>Suggestions later</b><p>Store proposed operations separately
          from the main document. Accepting applies them as one attributed
          transaction; rejecting closes them without changing live state.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Technology options</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table wiki-matrix">
            <thead><tr><th>Option</th><th>Strength</th><th>Limitation</th><th>Fit</th></tr></thead>
            <tbody>
              <tr><td>Yjs + Hocuspocus + PostgreSQL</td><td>Self-hosted,
              structured and rich-text CRDTs, offline cache, awareness, broad
              editor ecosystem.</td><td>Quirq must build snapshot grouping,
              semantic diff, audit envelopes, and History UI.</td><td>Best
              overall fit</td></tr>
              <tr><td>Liveblocks + Yjs</td><td>Managed WebSockets, presence,
              permissions, comments, and version snapshots with APIs and UI
              building blocks.</td><td>External service dependency, hosting,
              pricing, and vendor-policy considerations.</td><td>Fastest
              managed path</td></tr>
              <tr><td>Tiptap Platform</td><td>Strong rich-text editing,
              comments, version metadata, snapshot preview, and compare
              tooling.</td><td>Advanced history features use platform/private
              extensions and the product is editor-centric.</td><td>Best when
              collaborative wiki editing dominates</td></tr>
              <tr><td>Automerge Repo</td><td>JSON-like local-first state with
              native change history, old versions, branches, and automatic
              offline merge.</td><td>More custom work for Google Docs-style
              presence, comments, permissions, and operational backend
              integration.</td><td>Best when offline branching dominates</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Implementation sequence</h2>
        <ol class="wiki-steps">
          <li><b>Declare data ownership</b><p>Document local, collaborative,
          derived, secret, and presence classes. Add schema versions and
          forbid cross-boundary writes.</p></li>
          <li><b>Add the collaboration foundation</b><p>Run Hocuspocus and
          PostgreSQL in the Docker stack, issue document-scoped tokens from
          FastAPI, and connect Yjs with IndexedDB in the Space UI.</p></li>
          <li><b>Start with wiki pages and flows</b><p>Deliver live editing,
          presence, autosave, named versions, preview, diff, and restore on
          data that is genuinely human-authored.</p></li>
          <li><b>Add todos and safe metadata</b><p>Materialize compatible
          project <code>.xo</code> JSON while keeping watcher-owned fields
          derived.</p></li>
          <li><b>Add review workflows</b><p>Introduce anchored comments,
          assignments, notifications, and later suggestion accept/reject
          semantics.</p></li>
          <li><b>Harden operations</b><p>Add update compaction, retention,
          quotas, audit export, database backup/PITR, row-level isolation,
          metrics, and recovery tests.</p></li>
        </ol>
      </section>

      <section class="wiki-section">
        <h2>Deployment and one-command installation</h2>
        <div class="wiki-decision-list">
          <div><b>Local single-user mode</b><p>The installer can launch the
          API, collaboration service, and local persistence together. Browser
          tabs on the same machine can collaborate immediately.</p></div>
          <div><b>Multi-machine collaboration</b><p>A service bound only to
          <code>localhost:5003</code> is not reachable by peers. Deploy or
          expose the collaboration endpoint through TLS and configure the
          local Quirq client with its <code>wss://</code> URL.</p></div>
          <div><b>Keep one command</b><p>The human-facing installer can still
          be one command even if Docker starts an API container, collaboration
          container, PostgreSQL, and optional Redis behind it.</p></div>
          <div><b>Keep local state replaceable</b><p>A local collaboration
          cache may live under <code>.quirq</code>, but the shared server
          remains authoritative and the cache must be safe to delete and
          rebuild.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Hard rules</h2>
        <div class="wiki-decision-list">
          <div><b>Never synchronize plaintext secrets.</b><p>Use a secret
          manager and share references plus access policy.</p></div>
          <div><b>Never trust client attribution.</b><p>Derive user identity
          and authorization from a verified, short-lived server token.</p></div>
          <div><b>Never store presence as document history.</b><p>Use
          awareness with expiry; promote only meaningful lifecycle events to
          durable history.</p></div>
          <div><b>Never confuse undo, restore, and backup.</b><p>Local undo
          changes an editor session, version restore creates a new document
          head, and database recovery restores infrastructure.</p></div>
          <div><b>Never recreate Yjs history from JSON.</b><p>Persist and
          restore the original binary Yjs state; use JSON only as a validated
          read model or semantic-diff representation.</p></div>
          <div><b>Never allow two owners for one field.</b><p>Separate
          collaborative documents from watcher-derived outputs and make every
          materialization direction explicit.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Primary research sources</h2>
        <div class="wiki-table-wrap">
          <table class="wiki-table wiki-matrix wiki-sources">
            <thead><tr><th>Source</th><th>What it establishes</th></tr></thead>
            <tbody>
              <tr><td><a href="https://support.google.com/docs/answer/190843" target="_blank" rel="noreferrer">Google Docs version history</a></td><td>Grouped
              versions, named versions, contributor display, copy, preview,
              and restore behavior.</td></tr>
              <tr><td><a href="https://support.google.com/docs/answer/6033474" target="_blank" rel="noreferrer">Google Docs suggestions</a></td><td>Suggestions
              are proposed changes that require an accept or reject
              decision.</td></tr>
              <tr><td><a href="https://docs.yjs.dev/api/document-updates" target="_blank" rel="noreferrer">Yjs document updates</a></td><td>Binary updates,
              state vectors, merge properties, incremental synchronization,
              and compaction considerations.</td></tr>
              <tr><td><a href="https://docs.yjs.dev/getting-started/adding-awareness" target="_blank" rel="noreferrer">Yjs awareness and presence</a></td><td>
              Presence is ephemeral and intentionally excluded from persisted
              document state.</td></tr>
              <tr><td><a href="https://docs.yjs.dev/getting-started/allowing-offline-editing" target="_blank" rel="noreferrer">Yjs offline support</a></td><td>
              IndexedDB persistence and synchronization of missing changes
              after reconnect.</td></tr>
              <tr><td><a href="https://tiptap.dev/docs/hocuspocus/server/extensions/database" target="_blank" rel="noreferrer">Hocuspocus persistence</a></td><td>
              Store exact Yjs binary data in a generic database such as
              PostgreSQL; do not rebuild history from JSON.</td></tr>
              <tr><td><a href="https://tiptap.dev/docs/collaboration/documents/snapshot" target="_blank" rel="noreferrer">Tiptap snapshots</a></td><td>
              Automatic and named versions, contributor metadata, preview,
              and restore as a new latest version.</td></tr>
              <tr><td><a href="https://liveblocks.io/docs/api-reference/rest-api-endpoints" target="_blank" rel="noreferrer">Liveblocks REST API</a></td><td>
              Managed Yjs document access and version snapshots containing
              structured storage and Yjs state.</td></tr>
              <tr><td><a href="https://automerge.org/docs/hello/" target="_blank" rel="noreferrer">Automerge design</a></td><td>Local-first JSON-like CRDTs,
              automatic offline merge, version inspection, branching, and
              merging.</td></tr>
              <tr><td><a href="https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2" target="_blank" rel="noreferrer">Vault KV v2</a></td><td>
              Permissioned secret versions, check-and-set writes, soft
              deletion, recovery, and retention.</td></tr>
              <tr><td><a href="https://www.postgresql.org/docs/current/ddl-rowsecurity.html" target="_blank" rel="noreferrer">PostgreSQL row security</a></td><td>
              Database-enforced per-workspace visibility and modification
              policies.</td></tr>
              <tr><td><a href="https://www.postgresql.org/docs/current/continuous-archiving.html" target="_blank" rel="noreferrer">PostgreSQL point-in-time recovery</a></td><td>
              Write-ahead-log archiving and infrastructure recovery separate
              from product versions.</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <aside class="wiki-callout">
        <b>Final boundary</b>
        <p><code>.quirq</code> remembers how this machine operates. The
        collaboration database remembers what the team created.
        <code>.xo</code> exposes portable and materialized project state. A
        secret manager protects credentials. Git remains useful for code and
        deliberate exports, but it is not the live collaboration engine.</p>
      </aside>
    </article>`;
}

function flowsArticle(){
  return`
    <article class="wiki-article">
      <header class="wiki-hero">
        <div class="wiki-kicker">Design guide · Read paths</div>
        <h1>Building useful flows</h1>
        <p>Good flows begin with a question, choose the smallest authoritative
        API, and make freshness and missing telemetry visible. These recipes
        keep UI code independent of disk layout.</p>
        <div class="wiki-facts">
          <span>question first</span>
          <span>API over paths</span>
          <span>index before detail</span>
          <span>zero ≠ unavailable</span>
        </div>
      </header>

      <section class="wiki-section">
        <h2>Flow 1 · “What is happening right now?”</h2>
        <div class="wiki-recipe">
          <div class="wiki-recipe-step"><small>1</small><b>Workspace presence</b><code>GET /api/xo-projects/activity</code><p>Get all observably open sessions with project ids.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>2</small><b>Group and label</b><p>Group by project, show runtime/model, and display the response’s update timestamp.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>3</small><b>Drill into project</b><code>GET /api/xo-projects/{id}/activity</code><p>Use the project endpoint when the selected scope changes.</p></div>
        </div>
        <p class="wiki-note">Do not infer “idle” for a runtime that does not
        support presence. Show “presence unavailable” when runtime coverage is
        absent.</p>
      </section>

      <section class="wiki-section">
        <h2>Flow 2 · “What happened, and in what order?”</h2>
        <div class="wiki-recipe">
          <div class="wiki-recipe-step"><small>1</small><b>Fetch recent events</b><code>GET /api/xo-projects/{id}/timeline?limit=100</code><p>Render newest-first with event-specific labels.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>2</small><b>Filter intentionally</b><code>?types=session.started,file.edited</code><p>Use server filtering for focused audit views.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>3</small><b>Page by cursor</b><code>?before=&lt;next_cursor&gt;</code><p>Continue without loading an unbounded JSONL history.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Flow 3 · “Which session should I inspect?”</h2>
        <div class="wiki-recipe">
          <div class="wiki-recipe-step"><small>1</small><b>Start from the index</b><code>GET /api/xo-projects/{id}/usage/sessions</code><p>Sort by last activity and show runtime, totals, and task/message counters.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>2</small><b>Select one identity</b><p>Keep the composite key as the stable list identity; native session id is also accepted for lookup.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>3</small><b>Load detail</b><code>GET /api/xo-projects/{id}/usage/sessions/{session_id}</code><p>Fetch the heavier summary only after selection.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Flow 4 · “How is work trending?”</h2>
        <div class="wiki-recipe">
          <div class="wiki-recipe-step"><small>1</small><b>Choose scope</b><code>/api/xo-projects/usage/analytics</code><p>Use the workspace route or insert <code>/{id}</code> for one project.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>2</small><b>Choose a window</b><code>?days=7</code><p>Keep tokens, models, tools, messages, and latency on the same time window.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>3</small><b>Explain gaps</b><p>Label unavailable token or latency telemetry by runtime rather than silently treating it as zero.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Flow 5 · “What work is in flight?”</h2>
        <div class="wiki-recipe">
          <div class="wiki-recipe-step"><small>1</small><b>Read project todos</b><code>GET /api/xo-projects/{id}/todos</code><p>Group todo lists by session and runtime.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>2</small><b>Mutate through one lane</b><p>Claude Code uses native task tools; other runtimes use POST/PATCH/DELETE todo endpoints.</p></div>
          <i>→</i>
          <div class="wiki-recipe-step"><small>3</small><b>Reflect lifecycle</b><p>Make pending, in-progress, completed, blocked, and cancelled visually distinct.</p></div>
        </div>
      </section>

      <section class="wiki-section">
        <h2>Flow quality checklist</h2>
        <div class="wiki-check-grid">
          <div><b>Authority</b><p>Is this API the source for the question, or
          are you deriving the answer from a weaker proxy?</p></div>
          <div><b>Scope</b><p>Is the user looking at one project or the
          workspace aggregate? Keep the distinction visible.</p></div>
          <div><b>Freshness</b><p>Show <code>updated_at</code> for snapshots;
          do not present a stale heartbeat as live truth.</p></div>
          <div><b>Identity</b><p>Preserve project id, composite session key,
          native session id, runtime, and model as different concepts.</p></div>
          <div><b>Availability</b><p>Separate missing runtime support from a
          valid zero and from a temporarily empty state.</p></div>
          <div><b>Privacy</b><p>Use BFF responses. Never expose internal
          directories, native source paths, or private accumulator keys.</p></div>
          <div><b>Bounded reads</b><p>Use windows, limits, filters, and cursors
          instead of loading whole timelines or every session detail.</p></div>
          <div><b>Mutation lane</b><p>Use the one documented writer for a
          change so watcher and API state do not diverge.</p></div>
        </div>
      </section>

      <aside class="wiki-callout">
        <b>Debugging sequence</b>
        <p>If a flow looks wrong: check API health, inspect the response’s
        update time, confirm runtime coverage, compare project and workspace
        scope, then inspect server watcher logs. Filesystem inspection is a
        diagnostic last step, not an application integration.</p>
      </aside>
    </article>`;
}
