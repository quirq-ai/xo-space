from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.cowork_agent import quirq_catalog


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "xo-projects"
DASHES = re.compile("[\\u2013\\u2014]")  # en dash, em dash: banned in new docs


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class InboxDocsTests(unittest.TestCase):
    """The Inbox tab is documented in five places that drift independently:
    the wiki (PAGES, ARTICLES, TAB_GUIDES, and the .xo catalog table), the
    quirq output contract, the space_ui README, the developer guide, and the
    xo-projects skill. These pins fail when one of them forgets the tab."""

    def test_wiki_has_an_inbox_tab_guide(self) -> None:
        wiki = read("space_ui/js/views/wiki.js")
        # the same two pins test_space_wiki uses for every reachable view:
        # a PAGES entry (navigation) and an ARTICLES entry (content)
        self.assertIn("id:'tab-inbox'", wiki)
        self.assertIn("'tab-inbox':", wiki)
        self.assertIn("tabGuideArticle('inbox')", wiki)
        # the guide carries every field tabGuideArticle() reads, or the page
        # throws at render time and shows blank
        guides = wiki.index("const TAB_GUIDES=")
        start = wiki.index("\n  inbox:{", guides)
        end = wiki.index("\n  wiki:{", start)
        guide = wiki[start:end]
        for field in (
            "tab:'inbox'",
            "name:'Inbox'",
            "kicker:",
            "title:",
            "intro:",
            "facts:",
            "jobs:",
            "sources:",
            "steps:",
            "checks:",
            "note:",
        ):
            self.assertIn(field, guide)
        self.assertIn("/api/inbox", guide)
        self.assertIn("inbox.json", guide)
        self.assertIsNone(DASHES.search(guide))
        # the tab sits between Sessions and Wiki, so the wiki prose that placed
        # Wiki "between Sessions and Setup" is retired with it
        self.assertIn("between Inbox and Setup", wiki)
        self.assertNotIn("between Sessions and Setup", wiki)

    def test_wiki_xo_catalog_lists_inbox_json(self) -> None:
        wiki = read("space_ui/js/views/wiki.js")
        table = wiki[wiki.index("Workspace tier · <code>&lt;XO root&gt;/.xo/</code>"):]
        table = table[: table.index("</table>")]
        self.assertIn("<code>inbox.json</code>", table)
        self.assertIn("timeline, todos, sharing", table)

    def test_quirq_catalog_contract_lists_inbox_json(self) -> None:
        entries = [
            d for d in quirq_catalog._WORKSPACE_OUTPUT_CONTRACT
            if d["path"] == "inbox.json"
        ]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["used_by"], "Inbox")
        self.assertIn("feeders", entry["producer"])
        self.assertIn("cursors", entry["purpose"])

    def test_readme_documents_the_inbox_tab(self) -> None:
        readme = read("space_ui/README.md")
        self.assertIn("## Inbox tab", readme)
        # the count moves whenever a tab lands upstream; pin the Inbox entry itself
        self.assertIn("**Sessions**, **Inbox**, **Wiki**", readme)
        self.assertNotIn("Six top-level tabs", readme)
        self.assertIn("`js/views/inbox.js`", readme)
        self.assertIn("css/inbox.css", readme)
        for route in (
            "GET /api/inbox?status=open|done|all&limit=N",
            "POST /api/inbox",
            "PATCH /api/inbox/{id}",
            "DELETE /api/inbox/{id}",
        ):
            self.assertIn(route, readme)
        self.assertIn("`<XO root>/.xo/inbox.json`", readme)
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
        self.assertIn("bff/inbox.py", dev)


if __name__ == "__main__":
    unittest.main()
