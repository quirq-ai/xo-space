from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
DASHES = re.compile("[\\u2013\\u2014]")  # en dash, em dash: banned in new docs
AGENT_NAMES = ("openclaw", "hermes", "claude_code", "codex", "antigravity")
#: Documented link.view targets accepted by the Inbox API and resolved by the
#: UI, including stable aliases whose canonical browser routes contain '/'.
INBOX_VIEW_TARGETS = ("dashboard", "projects", "graph", "tree", "time", "agents",
                      "inbox", "sharing", "wiki", "quirq", "setup", "secrets", "connectors")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def squash(text: str) -> str:
    """Collapse whitespace so a sentence wrapped across lines still pins."""
    return re.sub(r"\s+", " ", text)


class InboxDocsTests(unittest.TestCase):
    """The local Inbox API and storage references stay aligned: the Quirq
    output contract, Space UI README, and developer guide.
    The compact Wiki's navigation is covered by test_space_wiki."""

    def test_quirq_catalog_describes_inbox_json_as_machine_local(self) -> None:
        self.assertEqual(
            [d for d in quirq_catalog._WORKSPACE_OUTPUT_CONTRACT if d["path"] == "inbox.json"], [],
            "no longer a workspace .xo file",
        )
        text = quirq_catalog._description("inbox/inbox.json", is_dir=False)
        self.assertIn("Inbox", text)
        self.assertIn("cursors", text)

    def test_readme_documents_the_inbox_tab(self) -> None:
        readme = read("space_ui/README.md")
        self.assertIn("## Inbox tab", readme)
        # the count moves whenever a tab lands upstream; pin the Inbox entry itself
        self.assertIn("**Agents**, **Inbox**, and **Setup**", readme)
        self.assertIn("`js/views/inbox.js`", readme)
        self.assertIn("css/inbox.css", readme)
        for route in (
            "GET /api/inbox?status=open|done|all&limit=N",
            "POST /api/inbox",
            "PATCH /api/inbox/{id}",
            "DELETE /api/inbox/{id}",
        ):
            self.assertIn(route, readme)
        self.assertIn("`~/.quirq/inbox/inbox.json`", readme)
        self.assertNotIn(".xo/inbox.json", readme)
        self.assertIn("`services/inbox/store.py`", readme)
        for feeder in ("`timeline`", "`todos`", "`sharing`"):
            self.assertIn(feeder, readme)
        for constant in ("MAX_ITEMS = 500", "DONE_TTL_DAYS = 30"):
            self.assertIn(constant, readme)
        self.assertIn("### Hand-editing", readme)
        section = readme[readme.index("## Inbox tab"):readme.index("## Data format")]
        self.assertIsNone(DASHES.search(section))

    def test_developing_guide_lists_the_inbox_package(self) -> None:
        dev = read("DEVELOPING.md")
        self.assertIn("inbox/", dev)
        # the package sits beside swarm_api, not under cowork_agent
        start = dev.index("services/  ")
        services_block = dev[start:dev.index("  cowork_agent/  ", start)]
        self.assertIn("  inbox/", services_block)
        self.assertNotIn("cowork_agent/inbox", dev)
        self.assertIn("~/.quirq/inbox/inbox.json", dev)
        self.assertIn("bff/inbox.py", dev)


class BatchRouteAndAutoCloseDocsTests(unittest.TestCase):
    """PR #97 added the batch ``PATCH /api/inbox``, the ``auto_closed`` flag and
    the always-stamped ingest throttle, and moved the file primitives to
    ``services/storage``. Every place that lists the inbox routes or the item
    fields (README, developer guide) must carry them, and the
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
        self.assertIn("`~/.quirq/inbox/inbox.json`", sec)
        self.assertIsNone(DASHES.search(sec))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, sec)

    def test_inbox_view_names_validate_and_match_the_feeders(self) -> None:
        # Browser paths containing '/' are outside the Inbox API's view-name grammar.
        for view in INBOX_VIEW_TARGETS:
            self.assertEqual(store.validate_link({"view": view}, strict=True), {"view": view})
        for route in ("setup/workspace", "setup/secrets", "setup/connectors"):
            with self.assertRaises(store.InboxError):
                store.validate_link({"view": route}, strict=True)
        feeders = read("services/inbox/feeders.py")
        self.assertEqual(sorted(set(re.findall(r'"view": "([a-z]+)"', feeders))),
                         ["agents", "connectors", "projects", "sharing"])

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
                        setup="setup/workspace", secrets="setup/secrets", connectors="setup/connectors")
        self.assertEqual(json.loads(result.stdout), expected)

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
