"""The agent-facing docs match the contract the code actually implements.

Three things rot independently, and all three are silent when they do:

1. **The mirror.** Every doc used to tell an agent that a watcher sink
   mirrored its runtime's native todos into ``.xo/todos.json``. That sink
   is gone (syncplan §7, T7/T8). Removing it did not stop a native
   ``TaskCreate`` from firing — it stopped anyone *seeing* the result, so
   an agent that believes the old doc records todos nowhere and is never
   told. Prose is the only enforcement there is, which is exactly why it
   is worth a test.

2. **The moved paths.** T19/T20 moved the session index, statistics, the
   timeline and sync state out of ``<project>/.xo/``. The template
   ``AGENTS.md`` is the first file every agent reads and its boot ritual
   named two of them by path; following it now reads a directory nothing
   writes and finds nothing, with no error.

3. **The catalog.** ``quirq_catalog`` is data-driven — ``quirq.js``
   renders whatever rows it publishes. A file that changes root without a
   matching contract entry renders "0 present" against a directory nothing
   writes any more, which looks exactly like "you have no data".

Nothing here touches the real ``~/xo-projects`` or ``~/.quirq``: the
catalog test redirects both roots into a temp dir, and both helpers
re-read the environment on every call.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.quirq_catalog import (
    _PROJECT_OUTPUT_CONTRACT,
    _TIER_RUNTIME,
    _TIER_SYNCED,
    quirq_catalog,
)


ROOT = Path(__file__).resolve().parents[1]

SKILL = ROOT / ".agents" / "skills" / "xo-projects" / "SKILL.md"
TODO_API_DOC = (
    ROOT / ".agents" / "skills" / "xo-projects" / "references" / "todos-http-api.md"
)
TEMPLATE = ROOT / "services" / "cowork_agent" / "project_template"

#: Every agent-facing doc the plan lists for T10, plus the two template
#: documents that quote the todo rule in passing.
AGENT_DOCS = (
    SKILL,
    TODO_API_DOC,
    TEMPLATE / "AGENTS.md",
    TEMPLATE / "OBJECTIVES.md",
    TEMPLATE / "PLAN.md",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class NoDocTeachesTheMirrorTests(unittest.TestCase):
    """Acceptance criterion 1: no doc still teaches the mirror."""

    #: Phrases that only make sense if the sink still exists. Each is a
    #: *claim*, not a keyword — the docs are free to explain that the
    #: mirror was removed, and do.
    FORBIDDEN = (
        r"watcher mirrors",
        r"mirrors those into",
        r"the watcher mirrors",
        r"[Dd]o \*\*not\*\* call the HTTP endpoints",
        r"[Ii]f you'?re a Claude Code agent, stop",
        r"non-Claude-Code (path|runtimes)",
        r"[Tt]wo write paths",
    )

    def test_no_agent_doc_makes_a_mirror_claim(self) -> None:
        for doc in AGENT_DOCS:
            text = read(doc)
            for pattern in self.FORBIDDEN:
                self.assertIsNone(
                    re.search(pattern, text),
                    f"{doc.relative_to(ROOT)} still teaches the removed todo "
                    f"mirror (matched {pattern!r}). The sink is gone; a doc "
                    f"that says otherwise makes an agent record todos nowhere.",
                )

    def test_every_doc_that_mentions_a_native_tool_says_it_is_not_enough(self) -> None:
        """Naming the native tool is fine — leaving it as the answer is not."""
        for doc in AGENT_DOCS:
            text = read(doc)
            if "native" not in text.lower():
                continue
            self.assertRegex(
                text,
                r"not (enough|a substitute)|does not reach|still call|invisible",
                f"{doc.relative_to(ROOT)} mentions a native todo tool without "
                f"saying it does not reach todos.json",
            )


class TodoApiIsBackendNeutralTests(unittest.TestCase):
    """Acceptance criterion 2: the HTTP API reads as the event source for
    every backend, not as the fallback for the ones without a task tool."""

    def test_the_runtime_table_covers_the_runtime_with_a_native_tool(self) -> None:
        rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*`([^`]+)`", read(TODO_API_DOC), re.M)
        values = {value for _label, value in rows}
        self.assertIn(
            "claude_code",
            values,
            "the runtime table omits the one backend that has a native todo "
            "tool — the row a reader most needs to see is the one that says "
            "it still calls this API",
        )
        # The table is a menu of defaults, not a closed set: the other
        # backends must stay listed or the doc reads as claude-specific.
        for expected in ("openclaw", "hermes", "codex"):
            self.assertIn(expected, values)

    def test_the_doc_declares_itself_the_only_write_path(self) -> None:
        text = read(TODO_API_DOC)
        self.assertRegex(text, r"only write path|sole writer")
        self.assertIn("every runtime", text.lower())

    def test_soft_delete_semantics_are_documented(self) -> None:
        """T9 changed DELETE from a removal to a tombstone, and added the
        switch that reads them back. An agent that believes the old text
        thinks a delete is destructive and avoids it."""
        text = read(TODO_API_DOC)
        self.assertIn("include_deleted", text)
        self.assertIn("deleted_at", text)
        self.assertRegex(text, r"[Tt]ombstone")
        # Idempotence is a promised behaviour of the route; keep it stated.
        self.assertIn("deleted: false", text)


class BootRitualPathsTests(unittest.TestCase):
    """Acceptance criterion 3: every path that moved in T19/T20 is correct
    in the template's boot ritual."""

    #: Paths under ``<project>/.xo/`` that no writer targets any more.
    MOVED = (
        ".xo/sessions/",
        ".xo/sessions.json",
        ".xo/timeline.jsonl",
        ".xo/stats.json",
        ".xo/sync.json",
    )

    def test_the_template_contract_names_no_moved_path(self) -> None:
        text = read(TEMPLATE / "AGENTS.md")
        for moved in self.MOVED:
            self.assertNotIn(
                moved,
                text,
                f"project_template/AGENTS.md still sends agents to {moved}, "
                f"which moved to the runtime tier in T19. Reading it finds "
                f"nothing and reports no error.",
            )

    def test_the_boot_ritual_reaches_history_through_the_api(self) -> None:
        text = read(TEMPLATE / "AGENTS.md")
        for endpoint in (
            "/api/xo-projects/<project-id>/todos",
            "/api/xo-projects/<project-id>/usage/sessions",
            "/api/xo-projects/<project-id>/timeline",
            "/api/xo-projects/<project-id>/activity",
        ):
            self.assertIn(endpoint, text, f"the contract never names {endpoint}")

    def test_claude_md_still_inherits_rather_than_duplicating(self) -> None:
        """The template's CLAUDE.md is one line — ``@AGENTS.md``. If it ever
        grows a copy of the todo rule, this suite stops covering it."""
        self.assertEqual(read(TEMPLATE / "CLAUDE.md").strip(), "@AGENTS.md")

    def test_the_template_ships_only_the_durable_half_of_xo(self) -> None:
        """The docs describe ``.xo/`` as identity + todos + peers; the
        scaffold has to agree, or a fresh project contradicts its own
        contract on day one."""
        shipped = {p.name for p in (TEMPLATE / ".xo").iterdir() if p.is_file()}
        self.assertEqual(shipped, {"project.json", "todos.json", "peers.json"})
        self.assertFalse((TEMPLATE / ".xo" / "sessions").exists())


class QuirqCatalogReflectsTheRealFileSetTests(unittest.TestCase):
    """Acceptance criterion 4, and the one that is a user-visible bug
    rather than prose: ``quirq.js`` renders ``present_count`` verbatim, so
    a contract row pointed at the wrong root silently reads "0 present".

    The test writes one file for every row the catalog declares, at the
    location the catalog itself publishes, and asserts the catalog finds
    all of them. A row whose tier is wrong cannot pass.
    """

    PID = "demo-pid-0001"
    PROJECT = "demo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.xo_root = tmp / "xo-projects"
        self.state_root = tmp / "quirq"
        self._env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.xo_root),
            "QUIRQ_STATE_ROOT": str(self.state_root),
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        # The realpath memo is keyed on the raw env value, so a new temp root
        # is always a miss — clear it anyway so this test cannot depend on
        # that being true.
        project_layout._ROOT_RESOLUTION_CACHE.clear()
        self.addCleanup(project_layout._ROOT_RESOLUTION_CACHE.clear)

        project_xo = self.xo_root / self.PROJECT / ".xo"
        project_xo.mkdir(parents=True)
        (project_xo / "project.json").write_text(
            json.dumps({"schema": 2, "pid": self.PID, "name": self.PROJECT}),
            encoding="utf-8",
        )
        self.project_xo = project_xo

    def _write(self, base: Path, relative: str) -> None:
        """Materialise one contract entry. ``sessionslist.d`` is a directory
        of shards, so "present" means it holds at least one file.

        Never clobbers a file that already exists — ``project.json`` is both a
        contract row and the document the runtime key is read from, so
        overwriting it with a placeholder silently re-keys the runtime home to
        the folder name and every runtime row then reads as absent.
        """
        target = base / relative
        if target.suffix:
            if target.exists():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "0123456789abcdef.json").write_text("{}", encoding="utf-8")

    def test_every_declared_project_file_is_found_where_the_catalog_says(self) -> None:
        runtime = project_layout.runtime_dir_for_project(self.PROJECT)
        self.assertIsNotNone(runtime, "a project with a pid must have a runtime home")
        roots = {_TIER_SYNCED: self.project_xo, _TIER_RUNTIME: runtime}
        for definition in _PROJECT_OUTPUT_CONTRACT:
            self._write(roots[definition["tier"]], definition["path"])

        rows = quirq_catalog()["project_outputs"]["project_contract"]
        missing = [r["path"] for r in rows if r["present_count"] != 1]
        self.assertEqual(
            missing, [],
            "these project rows render '0 present' even though the file "
            "exists in its tier — the contract's tier is wrong",
        )

    def test_every_declared_workspace_file_is_found_where_the_catalog_says(self) -> None:
        synced = self.xo_root / ".xo"
        runtime = project_layout.workspace_runtime_dir()
        from services.cowork_agent.quirq_catalog import _WORKSPACE_OUTPUT_CONTRACT

        for definition in _WORKSPACE_OUTPUT_CONTRACT:
            self._write(
                synced if definition["tier"] == _TIER_SYNCED else runtime,
                definition["path"],
            )

        rows = quirq_catalog()["project_outputs"]["workspace_contract"]
        missing = [r["path"] for r in rows if r["present_count"] != 1]
        self.assertEqual(
            missing, [],
            "these workspace rows render '0 present' even though the file "
            "exists in its tier — T20 moved the rollups to ~/.quirq/workspace/",
        )

    def test_the_published_location_names_the_root_the_file_is_in(self) -> None:
        """``location`` is what the UI prints. A right count with a wrong
        location is a different lie, not a fixed one."""
        catalog = quirq_catalog()["project_outputs"]
        for row in catalog["project_contract"]:
            expected = (
                "<project>/.xo" if row["tier"] == _TIER_SYNCED
                else "<quirq state>/projects/<pid>"
            )
            self.assertEqual(row["location"], f"{expected}/{row['path']}")
        for row in catalog["workspace_contract"]:
            expected = (
                "<XO root>/.xo" if row["tier"] == _TIER_SYNCED
                else "<quirq state>/workspace"
            )
            self.assertEqual(row["location"], f"{expected}/{row['path']}")

    def test_todos_is_the_one_project_file_with_no_watcher_producer(self) -> None:
        """The catalog's ``producer`` strings are read by a human deciding
        who to blame for a stale file. The todo sink is gone, so naming it
        sends them to a module that no longer exists."""
        rows = {d["path"]: d for d in _PROJECT_OUTPUT_CONTRACT}
        self.assertNotIn("sink", rows["todos.json"]["producer"].lower().replace(
            "there is no watcher todo sink", ""
        ))
        self.assertIn("Todo API", rows["todos.json"]["producer"])


if __name__ == "__main__":
    unittest.main()
