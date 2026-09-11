from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.cowork_agent import quirq_catalog


ROOT = Path(__file__).resolve().parents[1]
SKILL_REF = ROOT / ".agents" / "skills" / "xo-projects" / "references" / "inbox-http-api.md"
DASHES = re.compile("[\\u2013\\u2014]")  # en dash, em dash: banned in new docs
AGENT_NAMES = ("openclaw", "hermes", "claude_code", "codex", "antigravity")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def squash(text: str) -> str:
    """Collapse whitespace so a sentence wrapped across lines still pins."""
    return re.sub(r"\s+", " ", text)


def lines_with(text: str, needle: str) -> list[str]:
    found = [line for line in text.splitlines() if needle in line]
    assert found, f"no line contains {needle!r}"
    return found


class ConnectionsDocsTests(unittest.TestCase):
    """Connections polling and the two new Inbox feeders are documented in six
    places that drift independently: the wiki (the Inbox and Connectors tab
    guides plus the .quirq catalog page), the quirq catalog descriptions, the
    space_ui README, the developer guide, .env.example, and the xo-projects
    skill reference. These pins fail when one of them forgets the feature."""

    # ------------------------------------------------------------- wiki
    def _inbox_guide(self) -> str:
        wiki = read("space_ui/js/views/wiki.js")
        guides = wiki.index("const TAB_GUIDES=")
        start = wiki.index("\n  inbox:{", guides)
        return wiki[start: wiki.index("\n  wiki:{", start)]

    def _connectors_guide(self) -> str:
        wiki = read("space_ui/js/views/wiki.js")
        guides = wiki.index("const TAB_GUIDES=")
        start = wiki.index("\n  connectors:{", guides)
        return wiki[start: wiki.index("function tabGuideArticle", start)]

    def test_wiki_inbox_guide_covers_the_two_feeders_filter_section_and_url(self) -> None:
        guide = self._inbox_guide()
        self.assertIn("Five feeders fill it", guide)
        self.assertNotIn("Three feeders", guide)
        self.assertIn("'five feeders + API'", guide)
        self.assertIn("'source filter'", guide)
        # the two feeder inputs, in the sources table
        self.assertIn("'~/.quirq/projects/<pid>/github/issues.json'", guide)
        self.assertIn("'~/.quirq/connections/<toolkit>/events.jsonl'", guide)
        self.assertIn("issue:&lt;project&gt;:&lt;number&gt;", guide)
        self.assertIn("connection:&lt;toolkit&gt;:&lt;collector&gt;:&lt;id&gt;", guide)
        self.assertIn("last 7 days", guide)
        self.assertIn("last 24 hours", guide)
        # the auto-close rule is the one thing a reader must not misread
        self.assertIn("every mirror was readable", guide)
        # the source filter, the Connections section, and the url field
        self.assertIn("['Filter by source'", guide)
        self.assertIn("All, Issues, Connections, Workspace, Sharing, and Agents", guide)
        self.assertIn("['Watch the connections'", guide)
        self.assertIn("'GET /api/connections · POST /api/connections/{toolkit}/poll'", guide)
        self.assertIn("No connections polled yet", guide)
        self.assertIn("Open link", guide)
        self.assertIn("link, and url", guide)
        self.assertIn("XO_CONNECTIONS_POLL_ENABLED", guide)
        self.assertIsNone(DASHES.search(guide))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, guide)

    def test_wiki_connectors_guide_mentions_the_polling_drawer(self) -> None:
        guide = self._connectors_guide()
        self.assertIn("['Collect into Inbox'", guide)
        self.assertIn("'GET/PUT/DELETE /api/connections/{toolkit} · POST /api/connections/{toolkit}/poll'", guide)
        self.assertIn("['Polling shows an error'", guide)
        self.assertIn("nothing is stored until Save", guide)
        self.assertIn("carry no session header", guide)
        # only the added lines are held to the dash rule; the older lines of
        # this guide predate it
        for needle in ("Collect into Inbox", "/api/connections/{toolkit} ·", "Polling shows an error"):
            for line in lines_with(guide, needle):
                self.assertIsNone(DASHES.search(line), line)
                for agent in AGENT_NAMES:
                    self.assertNotIn(agent, line)

    def test_wiki_quirq_catalog_page_lists_the_connections_folder(self) -> None:
        wiki = read("space_ui/js/views/wiki.js")
        page = wiki[wiki.index("function quirqDataArticle()"):]
        page = page[: page.index("function ", 10)]
        # the directory map, in the same style as its neighbours
        self.assertIn("├── connections/", page)
        self.assertIn("&lt;toolkit&gt;/", page)
        for name in ("config.json", "state.json", "events.jsonl"):
            self.assertIn(name, page)
        self.assertIn("events.&lt;stamp&gt;.jsonl, three kept", page)
        # the file-by-file article
        article_start = page.index("<code>connections/&lt;toolkit&gt;/</code>")
        article = page[page.rindex('<article class="wiki-file">', 0, article_start): page.index("</article>", article_start)]
        self.assertIn("per-connection polling", article)
        self.assertIn("Rotated files are history only", article)
        self.assertIn("24 hour bootstrap floor", article)
        self.assertIn("DELETE /api/connections/{toolkit}", article)
        self.assertIsNone(DASHES.search(article))
        for line in lines_with(page, "connections/"):
            self.assertIsNone(DASHES.search(line), line)

    def test_wiki_quirq_catalog_row_counts_five_feeders(self) -> None:
        # inbox.json is machine-local now: its catalog entry lives on the .quirq page
        wiki = read("space_ui/js/views/wiki.js")
        article = wiki[wiki.index("<header><code>inbox.json</code><span>the Space Inbox</span></header>"):]
        article = article[: article.index("</article>")]
        self.assertIn("five feeders (timeline, todos, sharing, issues, connections)", article)

    # ------------------------------------------------------------- quirq catalog
    def test_quirq_catalog_describes_the_connections_tree(self) -> None:
        d = quirq_catalog._description
        self.assertEqual(
            d("connections", is_dir=True),
            "Per-connection polling: config, state, and collected events, one folder per toolkit",
        )
        self.assertEqual(
            d("connections/gmail", is_dir=True),
            "Polled connection: what to collect, how often, and what arrived",
        )
        self.assertEqual(
            d("connections/gmail/config.json", is_dir=False),
            "What to collect and how often; hand-editable",
        )
        self.assertEqual(
            d("connections/gmail/state.json", is_dir=False),
            "Poll cursors and the last result",
        )
        for name in ("events.jsonl", "events.20260911T120000Z.jsonl"):
            self.assertEqual(
                d(f"connections/gmail/{name}", is_dir=False),
                "Collected items, append-only, rotated at 2 MB",
            )
        # scoped: a deeper directory, a connection file the rules do not name,
        # and the same basenames elsewhere keep their own labels
        self.assertEqual(d("connections/gmail/deeper", is_dir=True), "Directory")
        self.assertEqual(d("connections/gmail/other.txt", is_dir=False), "Machine-local state file")
        self.assertEqual(d("state.json", is_dir=False), "Installation and onboarding state")
        self.assertEqual(
            d("projects/x/events.jsonl", is_dir=False),
            "Project runtime state; re-derivable, never synced",
        )
        self.assertEqual(d("connectionsx", is_dir=True), "Directory")
        for rel, is_dir in (("connections", True), ("connections/gmail", True),
                            ("connections/gmail/config.json", False),
                            ("connections/gmail/state.json", False),
                            ("connections/gmail/events.jsonl", False)):
            self.assertIsNone(DASHES.search(d(rel, is_dir=is_dir)))

    # ------------------------------------------------------------- README
    def test_readme_documents_feeders_url_and_connections_polling(self) -> None:
        readme = read("space_ui/README.md")
        section = readme[readme.index("## Inbox tab"): readme.index("## Data format")]
        flat = squash(section)
        # the feeder table
        self.assertIn("| `issues` |", section)
        self.assertIn("| `connections` |", section)
        self.assertIn("`cursors.issues`", section)
        self.assertIn("`cursors.connections`", section)
        self.assertIn("only the last 7 days are taken", section)
        self.assertIn("every mirror was readable", section)
        # the item shape and the API
        self.assertIn("link?, url?}", flat)
        self.assertIn('"url": null,', section)
        self.assertIn('"issues": {"enabled": true, "states": ["open"]}', flat)
        self.assertIn('"connections": {"enabled": true}', flat)
        self.assertIn("(title, body, link, url; status is never reset)", flat)
        # the UI
        self.assertIn("Open link (only when `url` is an http or https address", flat)
        self.assertIn('`rel="noopener noreferrer"`', section)
        self.assertIn("All | Issues | Connections | Workspace | Sharing | Agents", section)
        self.assertIn("- Connections section:", section)
        self.assertIn(
            "No connections polled yet. Connect a toolkit on the Connectors tab and turn on polling.",
            flat,
        )
        # the subsection
        self.assertIn("### Connections polling", section)
        sub = section[section.index("### Connections polling"): section.index("### Hand-editing")]
        self.assertIn("~/.quirq/connections/<toolkit>/", sub)
        for name in ("config.json", "state.json", "events.jsonl"):
            self.assertIn(name, sub)
        self.assertIn('"interval_s": 900,', sub)
        self.assertIn('"collectors": ["unread"],', sub)
        for route in (
            "`GET /api/connections`",
            "`GET /api/connections/{toolkit}`",
            "`PUT`",
            "`DELETE`",
            "`POST /api/connections/{toolkit}/poll`",
            "`GET /api/connections/{toolkit}/events?limit=1..500`",
        ):
            self.assertIn(route, sub)
        self.assertIn("404 `unknown_toolkit`", sub)
        self.assertIn("`XO_CONNECTIONS_POLL_ENABLED`", sub)
        self.assertIn("`XO_CONNECTIONS_POLL_TICK_S`", sub)
        self.assertIn("newer than its 24 hour bootstrap floor", sub)
        self.assertIn("one cursor across every toolkit", section)
        # hand-editing knows the new cursors and states
        self.assertIn("`sources.issues.states`", section)
        self.assertIsNone(DASHES.search(section))
        # the pre-existing example item names a runtime on purpose, so only
        # the parts this feature added are held to the no-agent-name rule
        added = [sub]
        for needle in ("| `issues` |", "| `connections` |", "- Connections section:"):
            added.extend(lines_with(section, needle))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, "\n".join(added))

    # ------------------------------------------------------------- DEVELOPING
    def test_developing_guide_lists_the_package_and_explains_degradation(self) -> None:
        dev = read("DEVELOPING.md")
        layout = dev[dev.index("## 2. Repository layout"): dev.index("## 3. How dispatch works")]
        self.assertIn("project_sharing, inbox.py, connections.py)", layout)
        self.assertIn("feeders (timeline,\n                                    todos, sharing, issues, connections) service", layout)
        self.assertIn("    connections/", layout)
        for module in ("store", "collectors", "mcp_client", "poller", "service"):
            self.assertIn(module, layout)
        self.assertIn("bff/connections.py", layout)
        for line in lines_with(layout, "connections"):
            self.assertIsNone(DASHES.search(line), line)
        self.assertIn("### 10.8 Connections polling", dev)
        sub = dev[dev.index("### 10.8 Connections polling"):]
        self.assertIn("**What it reads.**", sub)
        self.assertIn("**Where it writes.**", sub)
        self.assertIn("**How it degrades.**", sub)
        self.assertIn("`~/.quirq/connections/<toolkit>/config.json`", sub)
        self.assertIn("not signed in to XO (no account id)", sub)
        self.assertIn("is not turned on in this workspace", sub)
        self.assertIn("`XO_CONNECTIONS_POLL_ENABLED=false`", sub)
        self.assertIn("`XO_CONNECTIONS_POLL_TICK_S`", sub)
        self.assertIn("never from headers", sub)
        self.assertIn("`busy`", sub)
        self.assertIsNone(DASHES.search(sub))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, sub)

    # ------------------------------------------------------------- .env.example
    def test_env_example_lists_the_two_variables_commented(self) -> None:
        env = read(".env.example")
        self.assertIn("# XO_CONNECTIONS_POLL_ENABLED=true", env)
        self.assertIn("# XO_CONNECTIONS_POLL_TICK_S=30", env)
        # never set for real in the example: the defaults are the documentation
        self.assertNotRegex(env, r"(?m)^XO_CONNECTIONS_POLL_")
        block = env[env.index("# Connections polling (Inbox)"):]
        block = block[: block.index("XO_CONNECTIONS_POLL_TICK_S")]
        block = block + env[env.index("XO_CONNECTIONS_POLL_TICK_S"):].splitlines()[0]
        self.assertIn("events.jsonl", block)
        self.assertIn("Polling drawer", block)
        self.assertIn("min 5", block)
        self.assertIsNone(DASHES.search(block))
        # it sits with the other pollers, after the project sharing block
        self.assertLess(env.index("# XO_POLL_TOKEN="), env.index("# Connections polling (Inbox)"))
        self.assertLess(env.index("# Connections polling (Inbox)"), env.index("USAGE_SYNC_HOUR_UTC"))

    # ------------------------------------------------------------- skill reference
    def test_skill_reference_documents_the_optional_url(self) -> None:
        text = SKILL_REF.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 70)
        self.assertIsNone(DASHES.search(text))
        self.assertIn('"url": "https://github.com/org/my-app/issues/12"', text)
        self.assertIn("http(s) only, up to 2000 chars", text)
        self.assertIn("`url` is what the Open link button does", text)
        self.assertIn("anything else is `invalid_value`", text)
        self.assertIn('"url": null', text)
        self.assertIn("GitHub issues, and polled connection events", text)


if __name__ == "__main__":
    unittest.main()
