from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def view_contract(view: str) -> str:
    """The `export default {...}` head of a Space view module.

    Nav assertions have to be scoped to this slice, not run against the whole
    file: wiki.js is mostly documentation prose that legitimately quotes the
    view contract (``nav:false``, ``parent:'secrets'``) while describing how
    the app is wired, and a whole-file assertNotIn would fail on the docs
    rather than on the behaviour it means to pin.
    """
    source = (
        ROOT / "space_ui" / "js" / "views" / f"{view}.js"
    ).read_text(encoding="utf-8")
    head = source.split("export default", 1)[1]
    return head[: head.index("mount(")]


class SpaceWikiTests(unittest.TestCase):
    def test_wiki_view_is_registered_and_styled(self) -> None:
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")

        self.assertIn("import wikiView from './views/wiki.js?v=", app)
        self.assertIn("registerView(wikiView);", app)
        self.assertIn('href="css/wiki.css?v=', index)
        # The wiki is a top-level tab of its own. Order 7 is load-bearing:
        # it keeps Wiki between Sessions (4) and Setup (9), which is the nav
        # slot — and therefore the number hotkey — Quirq used to hold.
        contract = view_contract("wiki")
        self.assertIn("id:'wiki',label:'Wiki',order:7,", contract)
        self.assertNotIn("nav:false", contract)
        self.assertNotIn("parent:", contract)
        # The intro overlays are gone from Graph and Dashboard.
        self.assertNotIn('id="intro"', index)
        dashboard_builder = (
            ROOT / "services" / "cowork_agent" / "visualizer"
            / "categorized_graph.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("introTitle", dashboard_builder)
        self.assertNotIn("Every project has a purpose", dashboard_builder)

    def test_secrets_view_is_registered_and_never_reveals_saved_values(self) -> None:
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")
        secrets = (
            ROOT / "space_ui" / "js" / "views" / "secrets.js"
        ).read_text(encoding="utf-8")

        self.assertIn("import secretsView from './views/secrets.js?v=", app)
        self.assertIn("registerView(secretsView);", app)
        self.assertIn('href="css/secrets.css?v=', index)
        self.assertIn("id:'secrets',label:'Setup'", secrets)
        self.assertIn("type=\"password\"", secrets)
        self.assertIn("method:'PATCH'", secrets)
        self.assertIn("method:'DELETE'", secrets)
        self.assertIn("/api/runtime-config", secrets)
        self.assertIn("Apply &amp; restart", secrets)
        self.assertNotIn("/reveal", secrets)
        registry = (
            ROOT / "space_ui" / "js" / "core" / "registry.js"
        ).read_text(encoding="utf-8")
        self.assertIn("scrollIntoView", registry)

    def test_quirq_view_registered_and_six_degrees_removed(self) -> None:
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")
        quirq = (
            ROOT / "space_ui" / "js" / "views" / "quirq.js"
        ).read_text(encoding="utf-8")
        atlas = (
            ROOT / "space_ui" / "js" / "views" / "atlas.js"
        ).read_text(encoding="utf-8")

        self.assertIn("registerView(quirqView);", app)
        self.assertIn('href="css/quirq.css?v=', index)
        self.assertIn("id:'quirq'", quirq)
        self.assertIn("/api/quirq", quirq)
        # Quirq has no top-level tab: it opens from the Setup header button,
        # and Setup's tab stays lit while it is open.
        contract = view_contract("quirq")
        self.assertIn("nav:false", contract)
        self.assertIn("parent:'secrets'", contract)
        # The button id and the handler that reads it must move together: an
        # unguarded querySelector on a renamed id throws inside mount(), and
        # the registry bulkheads the whole Setup view behind its error card —
        # taking the only in-app route to Quirq down with it.
        secrets = (
            ROOT / "space_ui" / "js" / "views" / "secrets.js"
        ).read_text(encoding="utf-8")
        self.assertIn('id="setup-quirq"', secrets)
        self.assertIn("Open Quirq state", secrets)
        self.assertIn("querySelector('#setup-quirq')", secrets)
        self.assertIn("switchTo('quirq')", secrets)
        self.assertNotIn("setup-wiki", secrets)
        # Six Degrees was removed: no child lens, no lens switch, no view.
        self.assertNotIn("data-atlas-lens", index)
        self.assertNotIn("view-six", index)
        self.assertNotIn("SIX DEGREES", atlas)
        self.assertNotIn("sixView", atlas)
        # Graph and Projects merged into one Files tab that lands on the
        # List lens; the Graph is the nav-less second lens behind the pill.
        # The dept-filter chips row is gone from the canvas.
        # the lens switch is one element in the shell, not a copy per lens
        self.assertIn('id="fileslens"', index)
        self.assertNotIn('fileslens-graph', index)
        self.assertNotIn('id="chips"', index)
        self.assertIn("nav:false,parent:'projects'", atlas)
        projects = (
            ROOT / "space_ui" / "js" / "views" / "projects.js"
        ).read_text(encoding="utf-8")
        self.assertIn("id:'projects',label:'Files',order:1", projects)
        self.assertIn("space:focus-project", projects)
        self.assertIn("space:focus-project", atlas)

    def test_wiki_documents_the_storage_boundary_and_flow_pages(self) -> None:
        wiki = (ROOT / "space_ui" / "js" / "views" / "wiki.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("Storage & data map", wiki)
        self.assertIn("Install & run locally", wiki)
        self.assertIn("raw.githubusercontent.com", wiki)
        self.assertIn("no clone or checkout", wiki)
        self.assertIn("localhost:5002", wiki)
        self.assertIn("./cowork-api.sh dev", wiki)
        self.assertIn("127.0.0.1:5002", wiki)
        self.assertIn("How the watcher works", wiki)
        self.assertIn("Everything in .xo", wiki)
        self.assertIn("Everything in .quirq", wiki)
        self.assertIn("secrets.env", wiki)
        self.assertIn("Building useful flows", wiki)
        self.assertIn("Collaborative version history", wiki)
        self.assertIn(
            "watcher/activity/projects/&lt;id&gt;.json",
            wiki,
        )
        self.assertIn("GET /api/xo-projects/{id}/activity", wiki)
        self.assertIn("GET /api/xo-projects/{id}/timeline?limit=100", wiki)

    def test_wiki_documents_collaborative_version_control_design(self) -> None:
        wiki = (ROOT / "space_ui" / "js" / "views" / "wiki.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("id:'collaboration'", wiki)
        self.assertIn("collaboration:collaborationArticle", wiki)
        self.assertIn("Do not version the directory", wiki)
        self.assertIn("Yjs + Hocuspocus + PostgreSQL", wiki)
        self.assertIn("Synchronization history", wiki)
        self.assertIn("User-visible version history", wiki)
        self.assertIn("Operational disaster recovery", wiki)
        self.assertIn("Restore as a new latest version", wiki)
        self.assertIn("watcher/activity/**", wiki)
        self.assertIn("secret reference IDs", wiki)
        self.assertIn("Automerge Repo", wiki)
        self.assertIn("Liveblocks + Yjs", wiki)
        self.assertIn("docs.yjs.dev/api/document-updates", wiki)
        self.assertIn("support.google.com/docs/answer/190843", wiki)
        # The stylesheet must be cache-busted, but pinning the literal stamp
        # turns every legitimate bump into a red test (see the app.js stamp
        # test above for the same reasoning): assert the shape, not the value.
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")
        self.assertRegex(index, r"css/wiki\.css\?v=\d{8}-[a-z0-9]+")

    def test_wiki_documents_space_walk_session_replay(self) -> None:
        wiki = (ROOT / "space_ui" / "js" / "views" / "wiki.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("id:'spacewalk'", wiki)
        self.assertIn("spacewalk:spaceWalkArticle", wiki)
        self.assertIn("Space Walk session replay", wiki)
        self.assertIn("Sessions replayed as light", wiki)
        self.assertIn("bin/spacewalk serve --port 8765", wiki)
        # The local-except-the-judge carve-out the upstream README also makes.
        self.assertIn("Replay is entirely local", wiki)
        self.assertIn("The one exception is the optional", wiki)
        self.assertNotIn("Everything runs locally", wiki)
        # The touch lattice and its data-encoding palette (hexes live in the
        # token table only, so the lattice table cannot drift from them).
        self.assertIn("edit &gt; read &gt; hit", wiki)
        self.assertIn("#6a6700", wiki)
        self.assertIn("#5399d1", wiki)
        self.assertIn("#78a31e", wiki)
        self.assertNotIn("validated for color-vision", wiki)
        # Geometry facts verified against the Space Walk source tree.
        self.assertIn("squarified-treemap-v1", wiki)
        self.assertIn("sqrt(max(lines, bytes/4096, 16))", wiki)
        self.assertIn("160-bucket histogram", wiki)
        # Per-session observability grading, not per-harness.
        self.assertIn("session, not per harness", wiki)
        # Boundaries: a trace is transcript-adjacent, and nothing is written back.
        self.assertIn("A trace is not <code>.xo</code>-safe", wiki)
        self.assertIn("~/.spacewalk/judge", wiki)
        # Failure handling, the idiom every other long article carries.
        self.assertIn("Interpretation and troubleshooting", wiki)
        self.assertIn("Tilde on the error count", wiki)
        # HTTP surface, CLI, and the judge report cache.
        self.assertIn("GET /api/sessions/{selector}/snapshot", wiki)
        self.assertIn("spacewalk analyze &lt;session&gt;", wiki)
        self.assertIn("~/.spacewalk/reports/&lt;sessionKey&gt;.json", wiki)
        # Quirq recipes cross-link the Quirq view (opened from Setup) and
        # the upstream project. data-open-tab routes through ctx.switchTo,
        # which reaches nav:false views just as well as tabs.
        self.assertIn('data-open-tab="quirq"', wiki)
        self.assertIn("Recipe 5 · Is inference changing S₁ at all?", wiki)
        self.assertIn("https://github.com/cosmtrek/mindwalk", wiki)
        # Calculator routes are callable as printed, and the quirq citations
        # match the corpus: Definition 4 attributes rescue, O alone corrects.
        self.assertIn(
            "GET /api/repo/compare?path=&lt;repo&gt;&amp;from=u42-s0&amp;to=u42-s1",
            wiki,
        )
        self.assertIn("human rescue attributed to the rescued unit", wiki)
        self.assertIn("QER*(T) = QER(T)·(1 − O(T))", wiki)
        self.assertIn("$65/h loaded rate", wiki)
        self.assertNotIn("that A2 says must be", wiki)
        # The cache-bust chain itself is checked structurally in
        # test_cache_bust_chain_is_intact, not pinned to a literal here.

    def test_wiki_has_a_dedicated_guide_for_every_reachable_view(self) -> None:
        wiki = (ROOT / "space_ui" / "js" / "views" / "wiki.js").read_text(
            encoding="utf-8"
        )

        # Every navbar tab, plus Quirq — which has no tab of its own but is
        # reachable from Setup's header and by #/quirq, so it still earns a
        # guide. Adding a navbar tab without a guide should fail here.
        for page_id in (
            "tab-dashboard",
            "tab-files",
            "tab-timeline",
            "tab-sessions",
            "tab-wiki",
            "tab-quirq",
            "tab-setup",
        ):
            self.assertIn(f"id:'{page_id}'", wiki)
            self.assertIn(f"'{page_id}':", wiki)
        # Chat is hidden from the tab bar and unregistered, so it gets no
        # guide; Graph and Projects merged into the Files tab and its guide.
        self.assertNotIn("id:'tab-chat'", wiki)
        self.assertNotIn("id:'tab-graph'", wiki)
        self.assertNotIn("id:'tab-projects'", wiki)
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("registerView(chatView)", app)
        self.assertIn("newest at the top", wiki)
        self.assertNotIn("Six Degrees", wiki)
        self.assertIn("one page per tab", wiki)
        self.assertIn("space:wiki-page", wiki)

    def test_dashboard_todos_are_ui_state_not_graph_data(self) -> None:
        """Clicking a Dashboard project shows its todos on the map and in the
        panel — without ever putting them in the graph model.

        The model is built once from the dataset and read as a snapshot by
        LEAVES-derived counts, the timeline's lanes and axis, search and the
        re-root walk. A todo pushed in there would invent a timeline lane and
        inflate "N projects". The assertions below are the machine-checkable
        half of that rule: after the model is built, nothing may push a node
        or edge or add a byId key.
        """
        atlas = (
            ROOT / "space_ui" / "js" / "views" / "atlas.js"
        ).read_text(encoding="utf-8")

        # wired: fetch, lifecycle, both surfaces
        self.assertIn("/api/xo-projects/", atlas)
        self.assertIn("/todos", atlas)
        self.assertIn("syncSats(n)", atlas)      # follows the selection
        self.assertIn("clearSats()", atlas)      # and leaves with it
        self.assertIn('id="panel-todos"', atlas)  # the list
        self.assertIn("drawSatDots", atlas)       # the map
        # dashboard only: in space.json a leaf is a file, not a project
        self.assertIn("bootDataset==='dashboard'", atlas)

        after_model = atlas.split("const byId=new Map(", 1)[1]
        for mutation in ("NODES.push(", "EDGES.push(", "byId.set(", "byId.delete("):
            self.assertNotIn(
                mutation,
                after_model,
                f"{mutation} after the model is built: graph data must come "
                "from the dataset, not from a view interaction",
            )

    def test_tree_lens_is_the_third_files_lens(self) -> None:
        """Tree is a lens of the Files tab, not a tab of its own, and both
        pills offer all three lenses. A pill that lists a lens the registry
        does not know about is a dead button."""
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")
        projects = (
            ROOT / "space_ui" / "js" / "views" / "projects.js"
        ).read_text(encoding="utf-8")
        tree = (ROOT / "space_ui" / "js" / "views" / "tree.js").read_text(
            encoding="utf-8"
        )
        wiki = (ROOT / "space_ui" / "js" / "views" / "wiki.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("import treeView from './views/tree.js?v=", app)
        self.assertIn("registerView(treeView);", app)
        contract = view_contract("tree")
        self.assertIn("id:'tree',label:'Tree'", contract)
        self.assertIn("nav:false", contract)
        self.assertIn("parent:'projects'", contract)
        # One renderer, one position. Three copies in three containers is
        # what made the control jump when you used it, so the views must not
        # render it at all.
        for lens in ("projects", "graph", "tree"):
            self.assertIn(f'data-files-lens="{lens}"', index)
        for source in (projects, tree):
            self.assertNotIn('data-files-lens="', source)
        switcher = (
            ROOT / "space_ui" / "js" / "core" / "lens-switch.js"
        ).read_text(encoding="utf-8")
        self.assertIn("space:view", switcher)
        registry = (
            ROOT / "space_ui" / "js" / "core" / "registry.js"
        ).read_text(encoding="utf-8")
        self.assertIn("space:view", registry)
        # it reads the same dataset as the Graph
        self.assertIn("/xo/space.json", tree)
        # clicking a file previews it; it must not navigate. The Graph
        # hand-off lives in the previewer, as an explicit button.
        self.assertIn("space:preview-file", tree)
        self.assertNotIn("space:focus-project", tree)
        # horizontal: the root is at depth 0 on the left and every level of
        # containers opens one column to the right, drawn on an absolutely
        # positioned surface with an SVG connector layer under the chips.
        self.assertIn("const COL_W=", tree)
        self.assertIn("PAD_X+depth*COL_W", tree)
        self.assertIn("tv-surface", tree)
        self.assertIn("tv-links", tree)
        # leaves are the exception: they stack vertically beside their folder
        # instead of each claiming a column of their own
        self.assertIn("tv-stack", tree)
        self.assertIn("FILE_H", tree)
        # the tree reads as growth: branch thickness scales with what a limb
        # holds, new limbs draw themselves in, and the node you expanded stays
        # put instead of the reflow shoving the root off screen
        self.assertIn("const weight=", tree)
        self.assertIn("pathLength=", tree)
        self.assertIn("is-growing", tree)
        self.assertIn("function restoreAnchor", tree)
        self.assertIn("anchor=", tree)
        # Wiki Files guide must stay aligned with the three-lens UI (drift here
        # is how "two lenses" docs survive after Tree ships).
        self.assertIn("three lenses", wiki)
        self.assertIn("List | Graph | Tree", wiki)
        self.assertIn("#/tree", wiki)
        self.assertIn("/api/xo-projects/{id}/tree", wiki)
        self.assertNotIn("one home, two lenses", wiki)
        self.assertNotIn("'List | Graph lens switch'", wiki)

    def test_file_explorer_reads_the_detailed_tree_endpoint(self) -> None:
        """The Files drawer browses a project folder by folder, and the wire
        model carries the detail it renders."""
        projects = (
            ROOT / "space_ui" / "js" / "views" / "projects.js"
        ).read_text(encoding="utf-8")
        bff = (
            ROOT / "routers" / "cowork_agent" / "bff" / "xo_projects.py"
        ).read_text(encoding="utf-8")
        layout = (
            ROOT / "services" / "cowork_agent" / "project_layout.py"
        ).read_text(encoding="utf-8")

        self.assertIn("/tree", projects)
        self.assertIn("relative_path=", projects)
        self.assertIn("fx-crumbs", projects)      # breadcrumb navigation
        # folders and files get their own pane: one mixed list means hunting
        # for the folder rows among fifty files at every level
        self.assertIn("fx-dirs", projects)
        self.assertIn("fx-files", projects)
        self.assertIn("size_bytes", projects)     # per-file detail
        self.assertIn("modified_at", projects)
        # the detail fields are optional on the wire: a broken symlink or a
        # file deleted mid-listing must still list, just without detail
        for field in ("is_dir: bool", "size_bytes: Optional[int]",
                      "modified_at: Optional[str]", "entries: Optional[int]"):
            self.assertIn(field, bff)
        self.assertIn("st_size", layout)
        self.assertIn("st_mtime", layout)

    def test_previewer_renders_untrusted_files_safely(self) -> None:
        """Opening a file shows it in the floating window without navigating,
        and without giving a file on disk the run of this document.

        Workspace files are agent output, not trusted content, and this page
        holds the user's session. Markdown goes through the escape-first
        renderer; HTML renders in a sandboxed iframe WITHOUT
        allow-same-origin — an opaque origin: no cookies, no storage, no
        parent access, and the API's CORS allowlist refuses it. allow-scripts
        is deliberately granted (scroll-reveal reports and app index pages
        are blank without it); the origin line, not the script line, is the
        one that protects the session, and this test pins exactly that.
        """
        preview = (
            ROOT / "space_ui" / "js" / "core" / "preview.js"
        ).read_text(encoding="utf-8")
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")

        self.assertIn("initPreview", app)
        self.assertIn('id="preview"', index)
        self.assertIn('href="css/preview.css?v=', index)
        # markdown through the escape-first renderer, never raw
        self.assertIn("mdToHtml", preview)
        # HTML only ever inside a sandbox that withholds the origin: scripts
        # may run, but never as this app. The attribute is pinned whole so a
        # grant can only be added by rewriting this contract.
        self.assertIn(
            'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"',
            preview,
        )
        self.assertIn("srcdoc=", preview)
        # every surface opens it through the event, so no view imports another
        for view in ("tree", "projects", "atlas"):
            source = (
                ROOT / "space_ui" / "js" / "views" / f"{view}.js"
            ).read_text(encoding="utf-8")
            self.assertIn("space:preview-file", source)
            self.assertNotIn("core/preview.js", source)

    def test_file_preview_endpoint_is_bounded_and_scoped(self) -> None:
        """The preview endpoint addresses files by project id + relative path,
        never by absolute host path, and refuses anything it cannot show."""
        bff = (
            ROOT / "routers" / "cowork_agent" / "bff" / "xo_projects.py"
        ).read_text(encoding="utf-8")
        layout = (
            ROOT / "services" / "cowork_agent" / "project_layout.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"/api/xo-projects/{project_id}/file"', bff)
        self.assertIn("PREVIEW_MAX_BYTES", bff)
        self.assertIn("PREVIEW_SUFFIXES", bff)
        self.assertIn("preview_unsupported", bff)
        # the read helper enforces the same path rules as the tree listing
        self.assertIn("def read_project_file", layout)
        self.assertIn("relative_path escapes project root", layout)
        self.assertIn("max_bytes", layout)

    def test_files_list_rows_carry_signal_and_are_operable(self) -> None:
        """The List lens is the Files tab's landing view; a row has to say
        something. It fills its columns from workspace-wide requests — four
        in total, not four per project — and its header is a real button."""
        projects = (
            ROOT / "space_ui" / "js" / "views" / "projects.js"
        ).read_text(encoding="utf-8")
        workspace = (
            ROOT / "space_ui" / "js" / "core" / "workspace.js"
        ).read_text(encoding="utf-8")

        # counts come from the one cached dataset, never per project
        self.assertIn("workspaceCounts", projects)
        self.assertIn("/xo/space.json", workspace)
        # live + last-active come from the workspace-scope endpoints
        self.assertIn("/api/xo-projects/activity", projects)
        self.assertIn("/api/xo-projects/timeline?limit=", projects)
        self.assertNotIn("/todos'", projects.split("const PANELS")[0])
        # operable: filter, sort, and a refresh that keeps the open drawer
        self.assertIn('id="prj-filter"', projects)
        self.assertIn("data-sort=", projects)
        self.assertIn("if(expanded&&!items.some", projects)
        # accessible: a real button that reports its state, with Map outside
        # it (a button inside a button is invalid markup)
        self.assertIn('<button class="prj-row-head"', projects)
        self.assertIn('aria-expanded="', projects)
        self.assertIn('aria-controls="prj-drawer-', projects)
        head = projects.split('class="prj-row-head"')[1].split("</button>")[0]
        self.assertNotIn("prj-map", head)

    def test_project_description_falls_back_to_project_docs(self) -> None:
        """Every row showing only "created 11d ago" was the complaint. The
        list endpoint fills `description` from the project's own docs when
        .xo/project.json has none — excluding AGENTS.md, whose opening line
        is identical in every scaffolded project."""
        bff = (
            ROOT / "routers" / "cowork_agent" / "bff" / "xo_projects.py"
        ).read_text(encoding="utf-8")

        self.assertIn("_DESC_FILES", bff)
        self.assertIn('"README.md"', bff)
        self.assertNotIn('"AGENTS.md"', bff)
        self.assertIn("_described(name)", bff)
        self.assertIn("_DESC_MAX", bff)

    def test_cache_bust_chain_is_intact(self) -> None:
        """index.html's app.js stamp must be at least as new as every view stamp.

        Starlette's StaticFiles mount (routers/space.py) sends no Cache-Control,
        so browsers apply heuristic freshness to these module URLs. app.js is
        versioned only by the script tag in index.html: a browser holding the
        cached app.js keeps importing the OLD per-view URLs, which silently
        defeats every per-view bump. Pinning literals here instead just turns
        unrelated tests red on the next legitimate bump.
        """
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")

        view_stamps = re.findall(r"\?v=(\d{8})-[a-z0-9]+", app)
        self.assertTrue(view_stamps, "app.js carries no ?v= stamps")
        shell = re.search(r"js/app\.js\?v=(\d{8})-[a-z0-9]+", index)
        self.assertIsNotNone(shell, "index.html does not version js/app.js")
        self.assertGreaterEqual(
            shell.group(1),
            max(view_stamps),
            "bump the app.js stamp in index.html whenever a view stamp moves",
        )

    def test_installation_guide_documents_one_command_setup(self) -> None:
        guide = (ROOT / "INSTALLATION.md").read_text(encoding="utf-8")

        self.assertIn("curl -fsSL", guide)
        self.assertIn("localhost:5002", guide)
        # The canonical one-liner pipes to `sh`: the short URL serves a POSIX
        # bootstrap that runs install.sh under bash. Fetching install.sh
        # directly still needs bash (BASH_SOURCE, pipefail), and the guide
        # must keep saying so.
        self.assertIn("https://quirq.ai/install | sh", guide)
        self.assertIn("pipe it to `bash`, not `sh`", guide)
        # git went from "you do not need it" to a hard prerequisite.
        self.assertNotIn("You do not need Git", guide)

    def test_installer_runs_natively_without_docker(self) -> None:
        """The installer's premise: no Docker, and no surprise installs.

        Comment lines are stripped first so these assert what the script
        *does*, not what its header *says* about the Docker it replaced.
        """

        lines = (ROOT / "install.sh").read_text(encoding="utf-8").splitlines()
        code = "\n".join(
            line for line in lines if not line.lstrip().startswith("#")
        )

        self.assertNotIn("docker", code.lower())
        self.assertIn("uv venv", code)
        self.assertIn("uv pip install", code)
        # venv/, not uv's default .venv/ — CLAUDE.md, DEVELOPING.md and
        # compose.local.yml all document venv/bin/python.
        self.assertNotIn(".venv", code)
        # Root resolution must stay identical to the retired Docker installer.
        self.assertIn("saved_root_from_file", code)
        self.assertIn("validate_separate_roots", code)
        self.assertIn("prepare_state_root", code)
        # Nothing may be installed beyond requirements.txt.
        self.assertIn("QUIRQ_SKIP_BOOT_INSTALL", code)

    def test_first_run_is_explained_in_wiki_docs_and_the_empty_state(self) -> None:
        """A fresh install opens on an empty Files tab. The wiki page, the two
        docs and the empty state itself must all say what a project is and
        the three ways to get one — and agree on the API call."""

        wiki = (ROOT / "space_ui" / "js" / "views" / "wiki.js").read_text(encoding="utf-8")
        self.assertIn("id:'first-run'", wiki)
        self.assertIn("'first-run':firstRunArticle", wiki)
        self.assertIn("Your first run", wiki)
        self.assertIn("direct child folder of the workspace", wiki)
        self.assertIn("POST /api/files/mkdir", wiki)
        self.assertIn("xo-projects", wiki)
        # In-article cross-link from the install guide to the first-run page.
        self.assertIn('data-wiki-link="first-run"', wiki)
        self.assertIn("data-wiki-link", wiki.split("function pageButton")[0])

        projects = (ROOT / "space_ui" / "js" / "views" / "projects.js").read_text(encoding="utf-8")
        self.assertIn("data-first-run", projects)
        self.assertIn("'first-run'", projects)
        self.assertIn("scaffold:true", projects)
        self.assertNotIn("Create one through the xo-space", projects)

        guide = (ROOT / "INSTALLATION.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for doc in (guide, wiki):
            self.assertIn("Your first run", doc)
            self.assertIn("/api/files/mkdir", doc)
            # The first run is only empty if the directory was: a busy
            # directory lists every folder as an unscaffolded project, and
            # both full tellings must say so.
            self.assertIn("unscaffolded", doc)
        # The 2026-08-29 README rewrite tells the short version on purpose
        # and hands off to the guide: pin the empty state, the "First run"
        # paragraph, and the hand-off link instead of the full walkthrough.
        self.assertIn("First run", readme)
        self.assertIn("No projects in this workspace yet", readme)
        self.assertIn("INSTALLATION.md", readme)
        self.assertIn("## Your first run", guide)
        self.assertIn("uv pip install --python", guide)
        # The cross-link buttons need a rule, or they render as stock buttons.
        css = (ROOT / "space_ui" / "css" / "wiki.css").read_text(encoding="utf-8")
        self.assertIn(".wiki-link{", css)

    def test_contributing_guide_matches_how_the_repo_actually_works(self) -> None:
        """CONTRIBUTING.md is the front door for outside contributors. The
        facts most likely to rot are pinned: the branch model, the four
        invariants, the validation commands, and the README pointing at it."""
        guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        # Work lands on development; main is the release branch.
        self.assertIn("target `development`", guide)
        self.assertIn("publish-container.yml", guide)
        self.assertNotIn("Branch from and target **`main`**", guide)
        for invariant in (
            "modularity invariant", "Thin routers", "project folder is sacred",
            "belongs to the watcher",
        ):
            self.assertIn(invariant, guide)
        for command in (
            "unittest discover -s tests -t .",
            "bash tests/install_sh_harness.sh",
            "./scripts/check_plugin_sync.sh",
        ):
            self.assertIn(command, guide)
        # No hard-coded test count anywhere contributors read: it drifts with
        # every PR, and a stale number is worse than none.
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        developing = (ROOT / "DEVELOPING.md").read_text(encoding="utf-8")
        for doc in (guide, readme, developing):
            self.assertNotRegex(doc, r"\b\d{2,3} tests\b")
        self.assertNotIn("expect 146", developing)
        # Security reports must never be steered into a public issue body, and
        # the guide must not link GitHub private reporting until it is enabled.
        self.assertIn("request for a private channel", guide)
        self.assertNotIn("security/advisories/new", guide)
        # Windows contributors are told, up front, to use WSL.
        self.assertIn("On Windows, use WSL", guide)

        self.assertIn("[CONTRIBUTING.md](CONTRIBUTING.md)", readme)
        self.assertNotIn("branch from it and target it", readme)

    def test_installer_tells_you_how_to_come_back_and_never_nests(self) -> None:
        """Ctrl-C hands back a bare prompt; the banner must have said how to
        start again. And the one-liner run from inside ./xo-space must reuse
        that checkout, not clone a second one into it (which makes the outer
        checkout the projects root and leaves it dirty forever)."""

        lines = (ROOT / "install.sh").read_text(encoding="utf-8").splitlines()
        code = "\n".join(
            line for line in lines if not line.lstrip().startswith("#")
        )
        self.assertIn("print_restart_hint", code)
        self.assertIn("Start again later:", code)
        self.assertIn("https://quirq.ai/install", code)
        # The launch directory itself is probed for a checkout.
        self.assertIn('"${LAUNCH_DIR}/server.py"', code)
        # A checkout on another branch is left alone, like a dirty one.
        self.assertIn("rev-parse --abbrev-ref HEAD", code)

        guide = (ROOT / "INSTALLATION.md").read_text(encoding="utf-8")
        wiki = (ROOT / "space_ui" / "js" / "views" / "wiki.js").read_text(encoding="utf-8")
        for doc in (guide, wiki):
            self.assertIn("./xo-space/install.sh", doc)

    def test_installer_claims_no_container_only_capabilities(self) -> None:
        """Setting either would make the Setup tab offer a restart control
        that cannot work: nothing supervises a foreground process."""

        lines = (ROOT / "install.sh").read_text(encoding="utf-8").splitlines()
        code = "\n".join(
            line for line in lines if not line.lstrip().startswith("#")
        )

        self.assertNotIn("QUIRQ_MANAGED_CONTAINER", code)
        self.assertNotIn("QUIRQ_ALLOW_SELF_RESTART", code)
        # Host-path translation only existed to bridge a container boundary.
        self.assertNotIn("QUIRQ_HOST_HOME", code)
        self.assertNotIn("QUIRQ_HOST_PROJECTS_ROOT", code)
        self.assertNotIn("QUIRQ_HOST_STATE_ROOT", code)

    def test_project_template_no_longer_scaffolds_activity_in_xo(self) -> None:
        legacy_activity = (
            ROOT / "services" / "cowork_agent" / "project_template"
            / ".xo" / "activity.json"
        )
        self.assertFalse(legacy_activity.exists())

    def test_managed_agent_bootstraps_refresh_setup_tab_credentials(self) -> None:
        for agent in ("claude_code", "hermes", "openclaw"):
            setup = (
                ROOT / "config" / "agents" / agent / "setup.sh"
            ).read_text(encoding="utf-8")
            self.assertIn('QUIRQ_MANAGED_CONTAINER:-false', setup)
            self.assertIn(
                "Refreshing .env from managed Quirq configuration",
                setup,
            )


if __name__ == "__main__":
    unittest.main()
