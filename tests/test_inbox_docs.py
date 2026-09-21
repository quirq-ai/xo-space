from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from services.cowork_agent import quirq_catalog


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "xo-projects"
SKILL_REF = SKILL_DIR / "references" / "inbox-http-api.md"
WORK_REF = SKILL_DIR / "references" / "work-http-api.md"
ITEM_SKILL = ROOT / ".agents" / "skills" / "inbox-item" / "SKILL.md"
DESIGN = ROOT / "docs" / "work-and-workitems.md"
ROUTER = ROOT / "routers" / "cowork_agent" / "bff" / "inbox.py"
FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"
SAMPLE_PID = "00000000-0000-4000-8000-000000000000"
SAMPLE_WORKITEM = "d00d0001-0000-4000-8000-000000000001"
DASHES = re.compile("[\\u2013\\u2014]")  # en dash, em dash: banned in new docs
AGENT_NAMES = ("openclaw", "hermes", "claude_code", "codex", "antigravity")
SECTIONS = ("connections", "projects", "issues", "agents")
OUTCOME_KINDS = ("reply_drafted", "task_proposed", "needs_you", "fyi", "handled")
STATES = ("new", "running", "waiting", "failed", "closed")
#: Documented link.view targets accepted by the Inbox API and resolved by the
#: UI, including stable aliases whose canonical browser routes contain '/'.
INBOX_VIEW_TARGETS = ("dashboard", "projects", "graph", "tree", "time", "agents",
                      "inbox", "sharing", "wiki", "quirq", "setup", "secrets", "connectors")
#: The Inbox routes as the contract of 2026-09-21 lists them (the Inbox over
#: work items and sessions). The design document (section 18.9), the developer
#: guide (section 11) and the router's docstring carry these lines, one route
#: per line, verbatim.
INBOX_ROUTES = [
    "GET    /api/inbox?section=&entity=&state=&limit=      the rows, newest first, plus the sections summary",
    "GET    /api/inbox/sections                             one row per section: label, counts, entities, policy; runner {enabled}",
    "PUT    /api/inbox/sections/{section}                   the policy (strict body: sessions{...}, retention_days)",
    "POST   /api/inbox                                      201: {title, body?, kind?, source?, project_id?, link?, url?} -> a post work item row",
    "GET    /api/inbox/{project_id}/{workitem_id}           the row, fact, session, outcome, claim, workitem (projected record),",
    "                                                       transcript {session_id, native_session_id}, policy, running, can_reply, can_send",
    "POST   /api/inbox/{project_id}/{workitem_id}/reply     {text} -> 202 {session_id}",
    "POST   /api/inbox/{project_id}/{workitem_id}/start     ?retry=true -> 202 {session_id}",
    "POST   /api/inbox/{project_id}/{workitem_id}/send      202",
    "POST   /api/inbox/{project_id}/{workitem_id}/archive   {reason?: completed | not_planned} -> the row",
    "POST   /api/inbox/{project_id}/{workitem_id}/reopen    the row",
]
ROUTE_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)\s*(.*)$")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def squash(text: str) -> str:
    """Collapse whitespace so a sentence wrapped across lines still pins."""
    return re.sub(r"\s+", " ", text)


def section(text: str, heading: str) -> str:
    """From ``heading`` to the next heading of the same level, or the end."""
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    nxt = re.compile(rf"(?m)^{re.escape(level)} ").search(text, start + len(heading))
    return text[start:nxt.start()] if nxt else text[start:]


def fenced_block(text: str, after: str) -> list[str]:
    """The lines of the first fenced block that follows ``after``."""
    fence = text.index("```", text.index(after))
    start = text.index("\n", fence) + 1
    return text[start:text.index("```", start)].rstrip("\n").splitlines()


def routes_of(lines: list[str]) -> list[tuple[str, str, str]]:
    """``(method, path, description)`` per route, a continuation line folded
    into the route above it; the list ends at the first blank line after it
    started, so a docstring's prose is not read as routes."""
    routes: list[list[str]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            if routes:
                break
            continue
        match = ROUTE_LINE.match(line)
        if match:
            routes.append([match.group(1), match.group(2), match.group(3)])
        elif routes:
            routes[-1][2] = f"{routes[-1][2]} {line}"
    return [(method, path, squash(desc).strip()) for method, path, desc in routes]


class InboxDocsTests(unittest.TestCase):
    """The local Inbox API and storage references stay aligned: the Quirq
    output contract, Space UI README, developer guide, and xo-projects skill.
    The compact Wiki's navigation is covered by test_space_wiki."""

    def test_quirq_catalog_describes_the_inbox_folder_as_machine_local(self) -> None:
        self.assertEqual(
            [d for d in quirq_catalog._WORKSPACE_OUTPUT_CONTRACT if d["path"] == "inbox.json"], [],
            "never a workspace .xo file",
        )
        self.assertIn("Inbox", quirq_catalog._description("inbox", is_dir=True))
        # the ledger replaced inbox.json on 2026-09-21: the feeders' cursors live there
        text = quirq_catalog._description("inbox/ledger.json", is_dir=False)
        self.assertIn("cursors", text)

    def test_readme_names_the_inbox_pages_and_routes(self) -> None:
        readme = read("space_ui/README.md")
        # the count moves whenever a tab lands upstream; pin the Work entry itself
        self.assertIn("**Agents**, **Work**, and **Setup**", readme)
        # the two pages of design section 18.10 and the two routes an agent uses
        for needle in ("#/inbox/items", "#/inbox/item", "GET /api/inbox", "POST /api/inbox"):
            self.assertIn(needle, readme)
        self.assertNotIn(".xo/inbox.json", readme)

    def test_skill_reference_exists_and_is_linked(self) -> None:
        self.assertTrue(SKILL_REF.is_file())
        text = SKILL_REF.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 70)
        self.assertIsNone(DASHES.search(text))
        block = fenced_block(text, "## Endpoints")
        for method, path, _ in routes_of(INBOX_ROUTES):
            self.assertTrue(any(line.startswith(method) and path in line for line in block), f"{method} {path}")
        # the same base URL sentence as the todos reference
        self.assertIn("http://${HOST:-localhost}:${PORT:-5002}", text)
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/inbox-http-api.md", skill)
        # the skill is not mirrored anywhere else in the repo, so one edit is
        # the whole edit; a second copy showing up here means both need it
        copies = sorted(
            p for d in (".agents", "plugin") if (ROOT / d).is_dir()
            for p in (ROOT / d).rglob("SKILL.md")
            if "todos-http-api" in p.read_text(encoding="utf-8")
        )
        self.assertEqual(copies, [SKILL_DIR / "SKILL.md"])

    def test_developing_guide_lists_the_inbox_package(self) -> None:
        dev = read("DEVELOPING.md")
        self.assertIn("inbox/", dev)
        # the package sits beside swarm_api, not under cowork_agent
        start = dev.index("services/  ")
        services_block = dev[start:dev.index("  cowork_agent/  ", start)]
        self.assertIn("  inbox/", services_block)
        self.assertNotIn("cowork_agent/inbox", dev)
        self.assertIn("~/.quirq/inbox/ledger.json", dev)
        self.assertIn("bff/inbox.py", dev)


class InboxOverWorkItemsDocsTests(unittest.TestCase):
    """2026-09-21: the Inbox file and the section folders were replaced by
    work items joined with the watcher's session data (design section 18).
    Every place that lists the Inbox routes or its files (the developer guide,
    the design document, the two skill references, the inbox-item skill, the
    fixture README) must carry the new ones, and the route list the developer
    guide claims is the router's is checked against the router's docstring."""

    def test_developing_guide_section_11_carries_the_contract(self) -> None:
        dev = read("DEVELOPING.md")
        sec = section(dev, "## 11. The Space Inbox")
        self.assertNotIn("## 12.", sec)
        flat = squash(sec)
        self.assertEqual(fenced_block(sec, "**Routes.**"), INBOX_ROUTES)
        for needle in (
            "`~/.quirq/inbox/ledger.json`",
            "`~/.quirq/inbox/policy/<section>.json`",
            "`facts.py`",
            "`~/.quirq/projects/<pid>/workitems/<workitem-id>/fact.json`",
            '`runtime="inbox"`',
            '`["inbox", "<section>"]`',
            "`adopt_workitem`",
            "Bodies never enter `workitems.json`",
            "throttled to once per 5 s per process",
            "`register_new_events_listener`",
            "`POST /api/inbox` creates a `post` work item the same way (section `agents`)",
            "is a 422 from pydantic",
            "`InboxError` is a `ServiceError`, mapped by `bff/errors.http_error`",
            "`~/.quirq/inbox/inbox.json`, its `seen`/`done` states and `auto_closed` are gone",
            "docs/work-and-workitems.md section 18",
        ):
            self.assertIn(needle, flat)
        for name in SECTIONS:
            self.assertIn(f"`{name}`", sec)
        self.assertIsNone(DASHES.search(sec))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, sec)

    def test_developing_guide_section_12_describes_the_work_after_the_change(self) -> None:
        dev = read("DEVELOPING.md")
        sec = section(dev, "## 12. The Work")
        flat = squash(sec)
        for needle in (
            "(`items.py`)", "(`runner.py`;", "(`service.py`)",
            "`~/.quirq/inbox/policy/<section>.json`",
            "`~/.quirq/projects/<pid>/workitems/<workitem-id>/`",
            "`fact.json`", "`session.json`", "`outcome.json`",
            "`GET /api/work/inbox`", "`GET /api/work/attention`", "`GET /api/work/summary`",
            "`GET /api/feed`", "`POST /api/feed`", "`PUT /api/work/watermark`",
            "`POST /api/work/dismiss`", "`POST /api/work/ack`", "`PATCH /api/work/pins`",
            "`POST /api/work/promote`",
            "`XO_INBOX_SESSIONS=off`", "`AgentDispatcher(runtime).stream(...)`",
            "`claim_workitem`", "`links.session_ids`", "`inbox-item`",
            "`state_reason: completed`",
            "`GET /api/inbox/{project_id}/{workitem_id}`",
            "`GET /api/sessions/{session_id}/transcript`",
            "Gone with the folders: `~/.quirq/work/inbox/<section>/`, `items.json`, `thread.jsonl`, "
            "`run.log`, and the whole `/api/work/inbox/sections` and `/api/work/inbox/items/...` family",
        ):
            self.assertIn(needle, flat)
        for kind in OUTCOME_KINDS:
            self.assertIn(f"`{kind}`", sec)
        for state in STATES:
            self.assertIn(f"`{state}`", sec)
        self.assertIsNone(DASHES.search(sec))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, sec)

    def test_router_docstring_lists_exactly_the_contract_routes(self) -> None:
        """Section 11 says its route block is the router's docstring, line for
        line. Read from the source, not imported, so a half-built router still
        answers with the lines it has."""
        module = ast.parse(ROUTER.read_text(encoding="utf-8"))
        docstring = ast.get_docstring(module) or ""
        self.assertEqual(routes_of(docstring.splitlines()), routes_of(INBOX_ROUTES))

    def test_design_doc_section_18_supersedes_17(self) -> None:
        design = DESIGN.read_text(encoding="utf-8")
        sec18 = section(design, "## 18. The Inbox over work items and sessions")
        flat = squash(sec18)
        self.assertEqual(fenced_block(sec18, "### 18.9 Routes"), INBOX_ROUTES)
        for needle in (
            "### 18.1 Why", "**One record.**", "**Chats created at ingestion.**", "**Tabs as facets.**",
            "`ledger.json`", "`~/.quirq/inbox/policy/<section>.json`", "`claims.json`",
            "`fact.json`", "`session.json`", "`outcome.json`",
            '"kind": "connection"', '"kind": "sharing"', '"kind": "post"',
            "`services/inbox/facts.py`", "`services/work/runner.py`",
            "`GET /api/sessions/{session_id}/transcript`",
            "`#/inbox/items`", "`#/inbox/item?p=<project_id>&id=<workitem_id>`",
            "### 18.11 What is removed",
            "`/api/work/inbox/sections` and `/api/work/inbox/items/...` family",
            "**Bodies stay in the runtime root.**",
            "**The transcript is the chat.**",
            "**Sessions of a section run in `inbox-<section>` unless the fact names a project.**",
        ):
            self.assertIn(needle, flat)
        for kind in OUTCOME_KINDS:
            self.assertIn(f"`{kind}`", sec18)
        for state in STATES:
            self.assertIn(f"`{state}`", sec18)
        for name in SECTIONS:
            self.assertIn(f"`{name}`", sec18)
        self.assertIsNone(DASHES.search(sec18))
        # section 17 opens with the note on what section 18 replaced and what survives
        sec17 = section(design, "## 17. Connections as folders, a session per item")
        head = sec17[:900]
        self.assertIn("**Superseded on 2026-09-21 by section 18.**", head)
        for survivor in ("the policy shape", "the outcome block", "`inbox-item` skill",
                         "start, resume and outcome parsing"):
            self.assertIn(survivor, squash(head))
        self.assertIsNone(DASHES.search(head))
        # section 11's API table points at 18 and lists none of 17's routes
        sec11 = section(design, "## 11. API")
        self.assertIn("| `POST /api/inbox` |", sec11)
        self.assertIn("(section 18)", sec11)
        rows = [line for line in sec11.splitlines() if line.startswith("| `")]
        self.assertFalse(any("/api/work/inbox/" in row for row in rows), rows)
        self.assertIn("`/api/work/inbox/sections` and `/api/work/inbox/items/...` are gone", squash(sec11))

    def test_skill_reference_documents_the_routes_and_shapes(self) -> None:
        text = SKILL_REF.read_text(encoding="utf-8")
        documented = [(m, p) for m, p, _ in routes_of(fenced_block(text, "## Endpoints"))]
        self.assertEqual(documented, [(m, p) for m, p, _ in routes_of(INBOX_ROUTES)])
        for needle in (
            "## Post a work item", "## List the Inbox", "## Read one item, reply, start, send",
            "## Archive and reopen",
            "`<project>/.xo/workitems.json`", "`~/.quirq/projects/<pid>/workitems/<id>/fact.json`",
            "is a 422 from pydantic",
            "→ 201 the row",
            "→ 400 invalid_value (empty or overlong title, body, kind, source, url) | invalid_project_id | invalid_link",
            '"section": "agents"', '"kind": "post"',
            "`state` is `open` (new, running, waiting, failed; the default), `active` (running), "
            "`waiting`, `closed` or `all`",
            "`limit` is 1 to 500", "throttled to once per 5 s",
            "transcript: {session_id, native_session_id}",
            "`GET /api/sessions/{session_id}/transcript`",
            "→ 202 `{session_id}`", "`?retry=true`", "`not_planned`",
            "Nothing here deletes a work item",
        ):
            self.assertIn(needle, text)
        for name in SECTIONS:
            self.assertIn(f"`{name}`", text)
        for kind in OUTCOME_KINDS:
            self.assertIn(f"`{kind}`", text)
        # The published names include stable aliases; slash-separated browser
        # routes are intentionally outside the Inbox API's view-name grammar.
        listed = re.search(r"`view` names a Space view \((.+?); a view the UI does not know is ignored", text).group(1)
        self.assertEqual(sorted(re.findall(r"`([a-z_-]+)`", listed)), sorted(INBOX_VIEW_TARGETS))
        for route in ("setup/workspace", "setup/secrets", "setup/connections"):
            self.assertIn(f"`#/{route}`", text)
        for gone in ("PATCH  /api/inbox", "DELETE /api/inbox", "auto_closed", "inbox.json",
                     "/api/work/inbox/items", "/api/work/inbox/sections"):
            self.assertNotIn(gone, text)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_documented_view_names_resolve_in_the_registered_ui(self) -> None:
        """Reuse the navigation probe's real registrations and route factory."""
        from tests.test_space_navigation import PROBE

        script = PROBE + r"""
          const documentedTargets=JSON.parse(process.argv[2]),resolved={};
          for(const target of documentedTargets){
            await registry.switchTo('route-probe');
            await registry.switchTo(target);
            resolved[target]=location.hash.slice(2);
          }
          console.log(JSON.stringify(resolved));
        """
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script, "--",
             "#/setup", json.dumps(INBOX_VIEW_TARGETS)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {name: name for name in INBOX_VIEW_TARGETS}
        expected.update(dashboard="projects/overview", projects="projects/overview", graph="projects/data/graph",
                        tree="projects/data/tree", sharing="inbox/sharing", time="projects/timeline",
                        agents="agents/overview", inbox="inbox/items", quirq="setup/server/details",
                        setup="setup/workspace", secrets="setup/secrets", connectors="setup/connections")
        self.assertEqual(json.loads(result.stdout), expected)

    def test_work_reference_hands_the_items_to_the_inbox_reference(self) -> None:
        text = WORK_REF.read_text(encoding="utf-8")
        self.assertIsNone(DASHES.search(text))
        for needle in ("## The loop you are part of", "POST /api/feed", "## Items with a session of their own",
                       "`inbox-item` skill", "`inbox-http-api.md`"):
            self.assertIn(needle, text)
        for gone in ("/api/work/inbox/items", "/api/work/inbox/sections", "~/.quirq/work/inbox/"):
            self.assertNotIn(gone, text)

    def test_inbox_item_skill_names_the_fact_file_and_the_workbench(self) -> None:
        text = ITEM_SKILL.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 72)
        self.assertIsNone(DASHES.search(text))
        self.assertIn("names a fact file and a workbench", text)
        self.assertIn("(connections, projects, issues, agents)", text)
        flat = squash(text)
        for needle in (
            "The prompt gives you the fact file (`fact.json`, read-only: the thing as it arrived), "
            "the workbench folder (yours to write in)",
            "Open `fact.json` only if the prompt's copy is cut short",
            "Never write to `fact.json`, to `workitems.json` or anything else under `.xo/`, "
            "or to anything under `~/.quirq/`",
            "your session is resumed with their words",
        ):
            self.assertIn(needle, flat)
        for kind in OUTCOME_KINDS:
            self.assertIn(f"| `{kind}` |", text)
        # the outcome block is unchanged
        self.assertIn('{"kind": "reply_drafted", "summary": "one or two sentences a person reads instead of the item",',
                      text)
        self.assertIn('"draft": "reply.md", "task": null, "question": null, "acted": []}', text)
        for gone in ("item record", "thread", "run.log"):
            self.assertNotIn(gone, text)
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, text)

    def test_fixture_readme_names_the_ledger_the_policies_and_the_sidecars(self) -> None:
        readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
        rows = {cells[0]: cells for cells in
                ([c.strip() for c in line.split("|")[1:-1]] for line in readme.splitlines() if line.startswith("| `"))}
        self.assertEqual(rows["`inbox/`"][1], "`ledger.json`, `policy/<section>.json`")
        self.assertEqual(rows["`work/`"][1], "`inbox/inbox.json`, `live/live.json`, `history/history.json`")
        self.assertIn("`<pid>/workitems/<workitem-id>/fact.json`, `session.json`, `outcome.json`", rows["`projects/`"][1])
        self.assertIn("├── inbox/         the Inbox's ledger", readme)
        for gone in ("inbox/<section>", "thread.jsonl", "items.json", "| `inbox/` | `inbox.json` |"):
            self.assertNotIn(gone, readme)
        self.assertIsNone(DASHES.search(readme))

    def test_fixture_holds_the_files_the_readme_names(self) -> None:
        """The sample state root carries one work item's ledger, policy and
        sidecars, and none of the files the change removed."""
        workitems = FIXTURE / "projects" / SAMPLE_PID / "workitems"
        for path in (
            FIXTURE / "inbox" / "ledger.json",
            FIXTURE / "inbox" / "policy" / "connections.json",
            workitems / "claims.json",
            workitems / SAMPLE_WORKITEM / "fact.json",
            workitems / SAMPLE_WORKITEM / "session.json",
            workitems / SAMPLE_WORKITEM / "outcome.json",
        ):
            with self.subTest(file=str(path.relative_to(FIXTURE))):
                self.assertTrue(path.is_file())
                self.assertIn("schema", json.loads(path.read_text(encoding="utf-8")))
        # the person's marks (work/inbox/inbox.json) stay; the Inbox file and the section folders go
        for gone in ("inbox/inbox.json", "work/inbox/connections"):
            self.assertFalse((FIXTURE / gone).exists(), gone)


if __name__ == "__main__":
    unittest.main()
