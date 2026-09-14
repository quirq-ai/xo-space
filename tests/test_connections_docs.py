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
    """Keep local API/catalog references aligned with connection behavior.

    Detailed user guides live in xo-docs. The compact Wiki's navigation is
    covered separately by test_space_wiki.
    """

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
        # top level under services/, beside inbox/ and swarm_api/, never under cowork_agent/
        start = layout.index("services/  ")
        services_block = layout[start:layout.index("  cowork_agent/  ", start)]
        self.assertIn("  connections/", services_block)
        self.assertNotIn("cowork_agent/connections", dev)
        self.assertIn("### Placement: cowork_agent/ is for the agent, services/ is for the Space", dev)
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


class Pr97ConnectionsDocsTests(unittest.TestCase):
    """PR #97: the catalog gained Slack and Telegram, the poller opens one MCP
    session per poll, the identity lookup learned the deploy-gap rule, and the
    shared Space modules (storage, timestamps, errors, periodic, bff/errors)
    joined the tree. Each claim is pinned where it is made and, where the code
    can be asked, checked against it."""

    @staticmethod
    def _catalog() -> dict[str, list[str]]:
        from services.connections import collectors
        from services.cowork_agent.connectors.composio.service import TOOLKITS
        return {t: [s["id"] for s in collectors.catalog(t)] for t in TOOLKITS if collectors.catalog(t)}

    def test_every_collector_is_listed_where_the_catalog_is_described(self) -> None:
        cat = self._catalog()
        self.assertEqual(set(cat), {"gmail", "googlecalendar", "notion", "slack", "telegram"})
        # DEVELOPING 10.8: the catalog sentence names every toolkit and collector id
        dev = squash(read("DEVELOPING.md"))
        sentence = dev[dev.index("the read-only catalog:"): dev.index("every other toolkit has an empty list")]
        for toolkit, ids in cat.items():
            self.assertIn(f"`{toolkit}`", sentence)
            for cid in ids:
                self.assertIn(f"`{cid}`", sentence)
        # .env.example: the polling block lists them as "toolkit: id, id"
        env = read(".env.example")
        block = squash(env[env.index("# Connections polling (Inbox)"): env.index("# XO_CONNECTIONS_POLL_ENABLED")])
        for toolkit, ids in cat.items():
            self.assertIn(f"{toolkit}: {', '.join(ids)}", block)
        self.assertIn("(one MCP session per poll)", block)
        # README: the tree comment lists the toolkits, the prose every collector
        readme = read("space_ui/README.md")
        tree = [line for line in readme.splitlines() if line.startswith("~/.quirq/connections/<toolkit>/")]
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0].split("#", 1)[1].strip(), ", ".join(cat))
        prose = squash(readme[readme.index("Collectors are read-only tools from the catalog"):
                              readme.index("Routes (`routers/cowork_agent/bff/connections.py`")])
        for toolkit, ids in cat.items():
            self.assertIn(f"`{toolkit}`", prose)
            for cid in ids:
                self.assertIn(f"`{cid}`", prose)

    def test_auth_config_count_matches_the_toolkit_table(self) -> None:
        from services.cowork_agent.connectors.composio.service import TOOLKITS
        words = {8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen"}
        env = read(".env.example")
        self.assertIn(f"# the {words[len(TOOLKITS)]} COMPOSIO_AUTH_CONFIG_<TOOLKIT> ids", env)
        self.assertNotRegex(env, rf"# the (?!{words[len(TOOLKITS)]} )\w+ COMPOSIO_AUTH_CONFIG")

    def test_developing_guide_and_readme_describe_one_session_per_poll(self) -> None:
        from services.connections import poller
        import services.connections as pkg
        dev = read("DEVELOPING.md")
        sub = squash(dev[dev.index("### 10.8 Connections polling"): dev.index("## 11. The Space Inbox")])
        self.assertIn("each poll opens one MCP session (`mcp_client.McpSession`", sub)
        self.assertIn("lists its tools once (`tools/list`), runs every collector's `tools/call` on it, "
                      "then sends one best-effort DELETE", sub)
        self.assertIn('(`stage == "initialize"` and `status == 404`)', sub)
        self.assertIn(f'`"<collector>: {poller.SESSION_LOST}"` (`poller.SESSION_LOST`)', sub)
        self.assertIn("branches on those attributes, never on the message text", sub)
        self.assertNotIn("initialize, `notifications/initialized`, `tools/call`, then a best-effort DELETE of the session", sub)
        self.assertNotIn("Each collector is one `tools/call`", sub)
        layout = squash(dev[dev.index("## 2. Repository layout"): dev.index("## 3. How dispatch works")])
        self.assertIn("mcp_client (McpSession: one streamable-HTTP JSON-RPC session per poll over httpx)", layout)
        readme = read("space_ui/README.md")
        section = squash(readme[readme.index("### Connections polling"): readme.index("### Hand-editing")])
        self.assertIn("Each poll opens one MCP session (the handshake once), lists its tools once", section)
        self.assertIn(f'recorded as "{poller.SESSION_LOST}"', section)
        # the package docstring describes the session, not a one-call client
        self.assertIn("``McpSession`` runs the handshake once", squash(pkg.__doc__ or ""))
        self.assertNotIn("for one ``tools/call``", pkg.__doc__ or "")
        self.assertTrue(hasattr(pkg, "__doc__") and "opens one session per poll" in squash(pkg.__doc__))

    def test_identity_docs_describe_the_dual_send_and_the_deploy_gap(self) -> None:
        from services.cowork_agent.connectors.composio import service as composio_service
        from services.cowork_agent.connectors.composio import state
        self.assertTrue(callable(state.identity_field_gap))
        self.assertIsInstance(composio_service.LEGACY_STAMP, str)
        dev = squash(read("DEVELOPING.md"))
        self.assertIn("Until xo-swarm-api #41 is deployed it is dual-sent as `workspace_id` too", dev)
        self.assertIn("(`state.identity_field_gap`) is a deploy gap", dev)
        self.assertIn("`GET /xo-auth/session/self` answers 503 naming xo-swarm-api #41", dev)
        self.assertIn("Any other 422 (a string detail: the swarm read the id and rejected its value) "
                      "is authoritative and points at `XO_SPACE_ID`", dev)
        self.assertIn("(`{account_id, workspace_id}` from a swarm before xo-swarm-api #41; only `account_id` is read", dev)
        self.assertIn("or a 422 rejecting the id's value) never falls back", dev)
        # the store ownership rule, in 10.1, the sessions.json row and 10.5
        self.assertNotIn("A v4 store is never refused on ownership grounds", dev)
        self.assertIn("a document carrying the retired `workspace_id` key and no `space_id` is treated as "
                      "another space's (not adopted, its session queued for the boot sweep, replaced by the next write)", dev)
        self.assertIn("A store stamped for another space, or with the retired `workspace_id` key only, is not adopted", dev)
        self.assertIn("A v4 store stamped for another space is not adopted", dev)
        self.assertIn("(`service.LEGACY_STAMP`)", dev)
        self.assertIn("Only a store with no stamp at all is adopted", dev)
        env = read(".env.example")
        block = env[env.index("# This workspace's id at the swarm"): env.index("# XO_SPACE_ID=")]
        block = squash(re.sub(r"(?m)^#\s?", "", block))   # one comment block, read as prose
        self.assertIn("and as `workspace_id` too, until xo-swarm-api #41", block)
        self.assertIn("is a deploy gap, not a sign-in failure", block)
        self.assertIn("answers 503 naming #41", block)
        self.assertIn("A 422 rejecting the VALUE means this id is wrong", block)
        self.assertIn("(or with the retired `workspace_id` key only) is ignored rather than adopted", block)
        self.assertIsNone(DASHES.search(block))

    def test_placement_rule_names_the_shared_space_modules(self) -> None:
        import importlib
        for mod in ("services.storage.flock", "services.storage.atomic_write", "services.storage.reader",
                    "services.storage.paths", "services.timestamps", "services.errors", "services.periodic",
                    "routers.cowork_agent.bff.errors"):
            importlib.import_module(mod)
        dev = read("DEVELOPING.md")
        layout = dev[dev.index("## 2. Repository layout"): dev.index("## 3. How dispatch works")]
        start = layout.index("services/  ")
        services_block = layout[start: layout.index("  cowork_agent/  ", start)]
        for name in ("  storage/", "timestamps.py errors.py", "periodic.py", "run_forever", "ServiceError"):
            self.assertIn(name, services_block)
        self.assertIn("errors.py is the", layout)   # the bff line
        self.assertIn("(http_error)", layout)
        self.assertIn("(ForbidExtra)", layout)
        self.assertIn("| `services/storage/paths.py` |", dev)
        self.assertNotIn("services/cowork_agent/local_state.py", dev)
        rule = squash(dev[dev.index("### Placement: cowork_agent/ is for the agent"):
                          dev.index("### One executor for external commands")])
        for mod in ("`services/storage/`", "`services/timestamps.py`", "`services/errors.py`",
                    "`services/periodic.py`", "`routers/cowork_agent/bff/errors.py`"):
            self.assertIn(mod, rule)
        self.assertIn("`services.cowork_agent.local_state` import paths still resolve to the same module objects", rule)
        self.assertIn("`services/connections` never imports the inbox", rule)
        for rel in ("AGENTS.md", "CLAUDE.md"):
            text = squash(read(rel))
            self.assertIn("`services/storage/`", text)
            self.assertIn("`services/periodic.py`", text)
            self.assertIn("never imports the inbox", text)
            self.assertIsNone(DASHES.search(text), rel)
        import services.inbox as inbox_pkg
        self.assertIn("``update_many``", inbox_pkg.__doc__ or "")

    def test_readme_lists_the_shared_core_modules(self) -> None:
        readme = read("space_ui/README.md")
        table = readme[readme.index("## Files"): readme.index("## How it's served")]
        rows = {line.split("|")[1].strip(): line for line in table.splitlines() if line.startswith("| `")}
        for name in ("`toast`", "`esc`", "`rel`", "`pills`"):
            self.assertIn(name, rows["`js/core/ui.js`"])
        self.assertIn("`failText(res)`", rows["`js/core/api.js`"])
        self.assertIn("`every`", rows["`js/core/connections.js`"])
        self.assertIn("`pollLine`", rows["`js/core/connections.js`"])
        self.assertIn("(ignored while editing)", rows["`js/core/registry.js`"])
        self.assertIn("Primary sections", rows["`js/core/registry.js`"])
        self.assertIn("canonical routes", rows["`js/core/navigation.js`"])
        self.assertIn("native links", rows["`js/core/section-nav.js`"])
        self.assertIn("import map", rows["`index.html`"])
        self.assertIn("keeps unsaved edits", rows["`js/views/connectors.js`"])
        # each claim against the source it describes
        ui = read("space_ui/js/core/ui.js")
        for fn in ("toast", "esc", "rel", "pills"):
            self.assertRegex(ui, rf"export (function|const) {fn}\b")
        self.assertIn("export function failText(", read("space_ui/js/core/api.js"))
        conn = read("space_ui/js/core/connections.js")
        for fn in ("every", "collectorLabels", "pollLine"):
            self.assertIn(f"export function {fn}(", conn)
        self.assertIn("/INPUT|TEXTAREA|SELECT/", read("space_ui/js/core/registry.js"))
        html = read("space_ui/index.html")
        for mod in ("api", "ui", "connections"):
            self.assertIn(f'"./js/core/{mod}.js":"./js/core/{mod}.js?v=', html)
        self.assertIsNone(DASHES.search(table))


if __name__ == "__main__":
    unittest.main()
