from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import inbox as inbox_routes
from services.cowork_agent import quirq_catalog
from services.inbox import service, store


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "xo-projects"
SKILL_REF = SKILL_DIR / "references" / "inbox-http-api.md"
DASHES = re.compile("[\\u2013\\u2014]")  # en dash, em dash: banned in new docs
AGENT_NAMES = ("openclaw", "hermes", "claude_code", "codex", "antigravity")
#: Every view app.js registers (nav tabs and the nav:false lenses alike): the
#: ids a link.view may name and the UI will follow.
REGISTERED_VIEWS = ("dashboard", "projects", "graph", "tree", "time", "sessions",
                    "inbox", "sharing", "wiki", "quirq", "secrets", "connectors")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def squash(text: str) -> str:
    """Collapse whitespace so a sentence wrapped across lines still pins."""
    return re.sub(r"\s+", " ", text)


class InboxDocsTests(unittest.TestCase):
    """The local Inbox API and storage references stay aligned: the Quirq
    output contract, Space UI README, developer guide, and xo-projects skill.
    The compact Wiki's navigation is covered by test_space_wiki."""

    def test_quirq_catalog_describes_inbox_json_as_machine_local(self) -> None:
        self.assertEqual(
            [d for d in quirq_catalog._WORKSPACE_OUTPUT_CONTRACT if d["path"] == "inbox.json"], [],
            "no longer a workspace .xo file",
        )
        text = quirq_catalog._description("inbox.json", is_dir=False)
        self.assertIn("Inbox", text)
        self.assertIn("cursors", text)

    def test_readme_documents_the_inbox_tab(self) -> None:
        readme = read("space_ui/README.md")
        self.assertIn("## Inbox tab", readme)
        # the count moves whenever a tab lands upstream; pin the Inbox entry itself
        self.assertIn("**Sessions**, **Inbox**, **Setup**", readme)
        self.assertIn("`js/views/inbox.js`", readme)
        self.assertIn("css/inbox.css", readme)
        for route in (
            "GET /api/inbox?status=open|done|all&limit=N",
            "POST /api/inbox",
            "PATCH /api/inbox/{id}",
            "DELETE /api/inbox/{id}",
        ):
            self.assertIn(route, readme)
        self.assertIn("`~/.quirq/inbox.json`", readme)
        self.assertNotIn(".xo/inbox.json", readme)
        self.assertIn("`services/inbox/store.py`", readme)
        for feeder in ("`timeline`", "`todos`", "`sharing`"):
            self.assertIn(feeder, readme)
        for constant in ("MAX_ITEMS = 500", "DONE_TTL_DAYS = 30"):
            self.assertIn(constant, readme)
        self.assertIn("### Hand-editing", readme)
        section = readme[readme.index("## Inbox tab"):readme.index("## Data format")]
        self.assertIsNone(DASHES.search(section))

    def test_skill_reference_exists_and_is_linked(self) -> None:
        ref = SKILL_DIR / "references" / "inbox-http-api.md"
        self.assertTrue(ref.is_file())
        text = ref.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 70)
        self.assertIsNone(DASHES.search(text))
        for route in (
            "GET    /api/inbox",
            "POST   /api/inbox",
            "PATCH  /api/inbox/{item_id}",
            "DELETE /api/inbox/{item_id}",
        ):
            self.assertIn(route, text)
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
        self.assertIn("~/.quirq/inbox.json", dev)
        self.assertIn("bff/inbox.py", dev)


class BatchRouteAndAutoCloseDocsTests(unittest.TestCase):
    """PR #97 added the batch ``PATCH /api/inbox``, the ``auto_closed`` flag and
    the always-stamped ingest throttle, and moved the file primitives to
    ``services/storage``. Every place that lists the inbox routes or the item
    fields (README, developer guide, skill reference) must carry them, and the
    status-code split the docs claim is checked against the live router."""

    def test_readme_documents_the_batch_route_the_flag_and_the_throttle(self) -> None:
        readme = read("space_ui/README.md")
        section = readme[readme.index("## Inbox tab"): readme.index("## Data format")]
        flat = squash(section)
        self.assertIn("five HTTP routes", section)
        self.assertNotIn("four HTTP routes", section)
        self.assertIn("`PATCH /api/inbox` `{ids, status}` (1 to 500 ids)", flat)
        self.assertIn("answers `{updated, missing}`", flat)
        self.assertIn("which is what Mark all seen sends, once per page", flat)
        self.assertIn("or `limit` outside 1..500 is a 422 (pydantic)", flat)
        # the item shape, the feeder rule and the hand-editing recipe
        self.assertIn('"auto_closed": true,', section)
        self.assertIn("The one exception to \"never reset\"", flat)
        self.assertIn("a done a person set carries no flag and stays done", flat)
        self.assertIn("- Make a done stick:", section)
        self.assertIn("Any value other than `true` is dropped on read", flat)
        # the throttle and the badge cadence
        self.assertIn("the throttle is stamped whether or not a feeder fails", flat)
        self.assertIn("polled every 60 s while another tab is shown", flat)
        # the primitives moved: the README names the new home, not the visualizer
        self.assertIn("`services/storage/flock.locked`", section)
        self.assertIn("`services/storage/reader.read_json`", section)
        self.assertNotIn("the visualizer's `flock.locked`", section)

    def test_developing_guide_has_an_inbox_section_with_the_contract(self) -> None:
        dev = read("DEVELOPING.md")
        self.assertIn("## 11. The Space Inbox", dev)
        sec = dev[dev.index("## 11. The Space Inbox"):]
        flat = squash(sec)
        for route in (
            "`GET /api/inbox?status=open|done|all&limit=N`",
            "`POST /api/inbox`",
            "`PATCH /api/inbox` (`{ids, status}`: 1 to 500 ids in one locked write",
            "`PATCH /api/inbox/{item_id}` (`{status}`)",
            "`DELETE /api/inbox/{item_id}`",
        ):
            self.assertIn(route, flat)
        self.assertIn("answering `{updated, missing}`", flat)
        self.assertIn("is a 422 from pydantic", flat)
        self.assertIn("404 `item_not_found`", flat)
        self.assertIn(
            f"The throttle (`INGEST_MIN_INTERVAL_S`, {service.INGEST_MIN_INTERVAL_S:g} s per process) "
            "is stamped as soon as a run reaches the feeders, whether or not a feeder or the write then fails",
            flat,
        )
        self.assertIn("`register_new_events_listener`", sec)
        self.assertIn('flags it `"auto_closed": true`', flat)
        self.assertIn("never undone by a feeder", flat)
        self.assertIn("`~/.quirq/inbox.json`", sec)
        self.assertIsNone(DASHES.search(sec))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, sec)

    def test_skill_reference_documents_the_batch_route_views_and_codes(self) -> None:
        text = SKILL_REF.read_text(encoding="utf-8")
        self.assertIn("PATCH  /api/inbox                {ids, status}", text)
        self.assertIn('{ "ids": ["a1b2c3d4", "e5f6a7b8"], "status": "seen" }', text)
        self.assertIn('→ 200 { "updated": 1, "missing": ["e5f6a7b8"] }', text)
        self.assertIn("→ 400 invalid_status | invalid_value (empty, or more than 500 ids)", text)
        self.assertIn("is a 422 from pydantic", text)
        self.assertIn("→ 400 invalid_value (empty or overlong title, body, kind, source, url)", text)
        self.assertIn('"auto_closed": true', text)
        self.assertIn("Either PATCH drops `auto_closed`", text)
        self.assertIn("stamped whether or not a feeder fails", text)
        # the view list is exactly what app.js registers, and the rule is the store's
        self.assertNotIn("(`projects`, `sessions`, `time`, `dashboard`; unknown views are ignored)", text)
        listed = re.search(r"the view ids registered today are (.+?), and the UI ignores", text).group(1)
        self.assertEqual(sorted(re.findall(r"`([a-z_-]+)`", listed)), sorted(REGISTERED_VIEWS))
        self.assertIn(f"any `{store.VIEW_RE.pattern}` id", text)
        views = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "space_ui" / "js" / "views").glob("*.js"))
        for view in REGISTERED_VIEWS:
            self.assertRegex(views, rf"(id:\s*'{view}'|atlasView\('{view}')", view)
        app = read("space_ui/js/app.js")
        self.assertEqual(len(re.findall(r"^\s*registerView\(", app, re.M)), len(REGISTERED_VIEWS))
        feeders = read("services/inbox/feeders.py")
        self.assertEqual(sorted(set(re.findall(r'"view": "([a-z]+)"', feeders))), ["connectors", "projects", "sessions"])
        self.assertIn("the feeders themselves use `sessions`, `projects` and `connectors`", text)

    def test_the_status_code_split_the_docs_claim_holds(self) -> None:
        """422 is pydantic's (shape), 400 is the service's (value): none of these
        requests reaches the file, so no state root is needed beyond a stub."""
        app = FastAPI()
        app.include_router(inbox_routes.router)
        c = TestClient(app)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QUIRQ_STATE_ROOT": tmp}):
            self.assertEqual(c.post("/api/inbox", json={"body": "x"}).status_code, 422)          # missing title
            self.assertEqual(c.post("/api/inbox", json={"title": "t", "zzz": 1}).status_code, 422)  # unknown key
            self.assertEqual(c.get("/api/inbox?limit=501").status_code, 422)
            self.assertEqual(c.get("/api/inbox?limit=0").status_code, 422)
            self.assertEqual(c.patch("/api/inbox", json={"ids": "a1b2c3d4", "status": "seen"}).status_code, 422)
            self.assertEqual(c.patch("/api/inbox", json={"ids": [1], "status": "seen"}).status_code, 422)
            r = c.post("/api/inbox", json={"title": "   "})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_value"))
            r = c.post("/api/inbox", json={"title": "x" * (store.TITLE_MAX + 1)})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_value"))
            r = c.patch("/api/inbox", json={"ids": [], "status": "seen"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_value"))
            r = c.patch("/api/inbox", json={"ids": ["a1b2c3d4"] * (service.UPDATE_MANY_MAX + 1), "status": "seen"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_value"))
            r = c.patch("/api/inbox", json={"ids": ["a1b2c3d4"], "status": "bogus"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_status"))
            self.assertFalse(os.listdir(tmp), "none of these requests may touch the store")


if __name__ == "__main__":
    unittest.main()
