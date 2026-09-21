"""The Cmd+K command palette (space_ui/js/core/command-palette.js): shell
chrome that opens a searchable overlay for navigation, projects, quick
actions and handing a query to the active page's search.

Built as a vanilla ES module + the shared CSS tokens (no React/Tailwind/
bundler), styled after shadcn's Command dialog. These assertions pin the
seams: how it is wired into the shell, the key chord, the accessible dialog,
and the four result sources.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"
DASHES = re.compile("[\u2013\u2014]")   # en/em dash: banned in new code


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class CommandPaletteCompositionTests(unittest.TestCase):
    def test_wired_into_the_shell_with_a_cache_buster(self) -> None:
        app = read("js/app.js")
        self.assertRegex(
            app,
            r"import \{initCommandPalette\} from './core/command-palette\.js\?v=\d{8}-[a-z0-9]+';",
        )
        # started in its own bulkhead, handed the same switchTo the shell uses
        self.assertIn("initCommandPalette({switchTo,refreshCurrentView});", app)

    def test_stylesheet_linked_and_shell_stamp_advanced(self) -> None:
        index = read("index.html")
        self.assertRegex(index, r'href="css/command-palette\.css\?v=\d{8}-[a-z0-9]+"')
        self.assertRegex(index, r'src="js/app\.js\?v=\d{8}-[a-z0-9]+"')

    def test_navbar_trigger_opens_the_palette(self) -> None:
        index = read("index.html")
        # the compact navbar affordance: a search icon + shortcut badge
        self.assertIn('id="cmdk-trigger"', index)
        self.assertIn('id="cmdk-trigger-kbd"', index)
        self.assertNotIn('id="graph-search"', index)
        self.assertNotIn('id="q"', index)
        self.assertLess(index.index('class="resource-links"'), index.index('id="cmdk-trigger"'))
        self.assertLess(index.index('id="cmdk-trigger"'), index.index('id="wiki-link"'))
        toolbar = read("js/core/toolbar.js")
        # the trigger and the `/` shortcut ask the palette to open by event,
        # without importing it (shell chrome talks by event)
        self.assertIn("space:open-command-palette", toolbar)
        self.assertIn("getElementById('cmdk-trigger')", toolbar)
        self.assertIn('!trigger)return', toolbar)
        palette = read("js/core/command-palette.js")
        self.assertIn("addEventListener('space:open-command-palette'", palette)
        # the inline page-search field is now shown only for an active filter,
        # so the navbar defaults to just the trigger
        self.assertIn("const showLocal=!!search&&value!==''", toolbar)

    def test_opens_on_cmd_or_ctrl_k_from_anywhere(self) -> None:
        src = read("js/core/command-palette.js")
        # the chord: meta OR ctrl + k, not alt; capture phase + preventDefault
        # so it works inside inputs and overrides the browser's own Ctrl/Cmd+K
        self.assertIn("event.metaKey||event.ctrlKey", src)
        self.assertIn("event.key==='k'||event.key==='K'", src)
        self.assertIn("{capture:true}", src)
        self.assertIn("event.preventDefault()", src)
        self.assertIn("open?close():openPalette()", src)

    def test_is_an_accessible_command_dialog(self) -> None:
        src = read("js/core/command-palette.js")
        self.assertIn('role="dialog"', src)
        self.assertIn('aria-modal="true"', src)
        self.assertIn('role="listbox"', src)
        self.assertIn('role="option"', src)
        self.assertIn("aria-activedescendant", src)
        # arrow/enter/escape are handled, and Escape is stopped from reaching
        # the page underneath (whose own Escape clears a page search)
        self.assertIn("event.key==='Escape'", src)
        self.assertIn("event.stopPropagation()", src)
        self.assertIn("event.key==='ArrowDown'", src)
        self.assertIn("event.key==='Enter'", src)
        # focus is restored to wherever it was when the palette closes
        self.assertIn("restoreFocus", src)

    def test_navigates_through_the_injected_switch_to(self) -> None:
        src = read("js/core/command-palette.js")
        # never imports a view module or the registry; navigates via switchTo
        self.assertIn("switchTo(route)", src)
        self.assertNotIn("from '../views/", src)
        self.assertNotIn("from './registry.js", src)

    def test_project_source_reads_the_catalog_and_opens_a_drawer(self) -> None:
        src = read("js/core/command-palette.js")
        self.assertIn("API_BASE+'/api/xo-projects'", src)
        self.assertIn("display_name", src)
        # selecting a project opens the List and asks it to open that drawer
        self.assertIn("go('projects/data/list')", src)
        self.assertIn("space:open-project", src)

    def test_can_hand_the_query_to_the_active_page_search(self) -> None:
        src = read("js/core/command-palette.js")
        self.assertIn("getElementById('view-search')", src)
        self.assertIn("getElementById('view-search-wrap')", src)
        # feeds the shared toolbar input through its own input event
        self.assertIn("new Event('input',{bubbles:true})", src)

    def test_quick_actions_are_present(self) -> None:
        src = read("js/core/command-palette.js")
        self.assertIn("refreshCurrentView", src)
        self.assertIn("Refresh this page", src)
        self.assertIn("New project", src)

    def test_imports_core_helpers_bare_for_the_import_map(self) -> None:
        src = read("js/core/command-palette.js")
        # api.js and ui.js are import-map stamped; importing them bare keeps
        # one shared instance (the same rule every other module follows)
        self.assertIn("import {API_BASE,apiFetch} from './api.js';", src)
        self.assertIn("import {toast} from './ui.js';", src)
        self.assertNotRegex(src, r"from '\./(api|ui)\.js\?v=")

    def test_navigation_routes_cover_the_primary_destinations(self) -> None:
        src = read("js/core/command-palette.js")
        routes = set(re.findall(r"\['([a-z][a-z0-9/-]*)',", src))
        for route in ("projects/overview", "projects/data/list", "projects/data/graph",
                      "projects/data/tree", "projects/timeline", "projects/manage",
                      "agents/overview", "work", "work/live", "work/history",
                      "setup/workspace", "setup/connectors", "setup/secrets",
                      "setup/server", "setup/server/details", "wiki"):
            self.assertIn(route, routes, route)

    def test_new_files_carry_no_dashes(self) -> None:
        for rel in ("js/core/command-palette.js", "css/command-palette.css"):
            self.assertIsNone(DASHES.search(read(rel)), rel)
        self.assertIsNone(DASHES.search(Path(__file__).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
