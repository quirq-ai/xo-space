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

The catalog is checked twice, and the second check is the one that
matters. :class:`QuirqCatalogReflectsTheRealFileSetTests` materialises
each row where the catalog says it lives and asserts the catalog finds
it — self-consistency, which cannot see a row whose tier disagrees with
the real sink (``docs/OUTSTANDING.md``, **O-J**).
:class:`CatalogAgreesWithTheWritersTests` closes that: it runs the real
writers — a scaffold, a ``Watcher.tick()``, the manifest builder and the
three request-path stores — and asserts every published row lands on
what they actually wrote.

Nothing here touches the real ``~/xo-projects`` or ``~/.quirq``: both
catalog tests redirect both roots into a temp dir, and every helper
re-reads the environment on every call.
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
WORKITEMS_API_DOC = (
    ROOT / ".agents" / "skills" / "xo-projects" / "references"
    / "workitems-http-api.md"
)
TEMPLATE = ROOT / "services" / "cowork_agent" / "project_template"

#: Every agent-facing doc the plan lists for T10, plus the two template
#: documents that quote the todo rule in passing.
AGENT_DOCS = (
    SKILL,
    TODO_API_DOC,
    WORKITEMS_API_DOC,
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
    all of them — so it catches a row the *reader* resolves differently
    from the way it is published (a bad ``location`` string, a tier the
    lookup has no base for).

    It cannot catch a row whose tier disagrees with the writer, because
    it created the file in the wrong place too. That is O-J, and
    :class:`CatalogAgreesWithTheWritersTests` below is where it is
    checked.
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


class _FakeSource:
    """One backend's source, replaying a fixed batch.

    Named for the agent the environment below pins only because the
    watcher asserts a source's name matches its manifest — the same
    concession ``tests/test_workspace_tier_invariant.py`` makes.
    """

    name = "claude_code"

    def __init__(self, events):
        self._events = events

    def poll_events(self):
        return list(self._events)

    def poll_presence(self):
        return []


class CatalogAgreesWithTheWritersTests(unittest.TestCase):
    """**O-J.** The catalog is compared against what the writers do.

    The class above proves the catalog is self-consistent. This one
    proves it is *true*: nothing here is allowed to place a file. Every
    document is produced by the code that owns it in production — the
    project scaffold, a real ``Watcher.tick()``, the ``xo.json`` manifest
    builder, and the three request-path stores (todos, workitems, the
    GitHub mirror and workitem claims) — and only then is each published
    row resolved and required to land on the result.

    The tier roots come from :mod:`project_layout`, which owns the
    synced-vs-runtime decision (T18), never from the catalog: a row with
    the wrong tier must not be allowed to move the place it is compared
    against.

    :meth:`test_no_row_is_also_satisfiable_from_the_other_tier` is what
    stops the whole thing being vacuous — if a document existed under
    both roots, "found at the declared tier" would prove nothing.
    """

    PROJECT = "Demo Project"
    TS = "2026-09-07T12:00:00Z"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.xo_root = tmp / "xo-projects"
        self.xo_root.mkdir(parents=True)
        self.state_root = tmp / "quirq"
        self.state_root.mkdir(parents=True)
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.xo_root),
            "QUIRQ_STATE_ROOT": str(self.state_root),
            "XO_PROJECT_TEMPLATE": "",
            "AGENT_NAME": _FakeSource.name,
            # The telemetry view scans the session stores under $HOME.
            "HOME": str(tmp),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        project_layout._ROOT_RESOLUTION_CACHE.clear()
        self.addCleanup(project_layout._ROOT_RESOLUTION_CACHE.clear)

        project_layout.scaffold_project(self.PROJECT)
        self.project = project_layout.resolve_project_dirname(self.PROJECT)
        self._drive_every_writer()

    # ── the writers ──────────────────────────────────────────────────

    def _events(self):
        from services.cowork_agent.visualizer.ingest.events import (
            MessageObserved,
            SessionFirstSeen,
            UsageObserved,
        )

        common = {
            "ts": self.TS,
            "project_id": self.project,
            "native_session_id": "native-1",
            "runtime": _FakeSource.name,
        }
        return [
            SessionFirstSeen(cwd=str(self.xo_root / self.project), **common),
            MessageObserved(role="assistant", **common),
            UsageObserved(input_tokens=10, output_tokens=5, model="m", **common),
        ]

    def _drive_every_writer(self) -> None:
        """Produce every declared document the way production does."""
        import asyncio

        from services import xo_manifest
        from services.cowork_agent import scopes
        from services.cowork_agent.engine import sessions_io as session_index
        from services.cowork_agent.visualizer import github_mirror
        from services.cowork_agent.visualizer import watcher as watcher_module
        from services.cowork_agent.visualizer.workspace import space_json, views

        # The session index is adapter-written, not watcher-written, so the
        # per-project shard and the workspace union have nothing to roll up
        # unless a row exists first.
        session_index.write_session_row(
            self.project,
            f"{_FakeSource.name}:native-1",
            {"sessionId": "sess-1", "nativeSessionId": "native-1",
             "backend": _FakeSource.name},
        )
        # The request path: four documents whose only writer is a route.
        scope = scopes.VisualizerScope(self.project)
        scope.create_todo(runtime=_FakeSource.name, content="a todo")
        item = scope.create_workitem(runtime=_FakeSource.name, title="a workitem")
        scope.claim_workitem(
            item["id"], session_id="sess-1", runtime=_FakeSource.name
        )
        # An empty page list is a legitimate poll of a quiet repo and still
        # writes the mirror, so no GitHub response has to be invented.
        github_mirror.record_pages(
            self.project, repo="owner/repo", pages=[], complete=False
        )
        # xo.json is seeded at server startup, not by the tick.
        asyncio.run(xo_manifest.write_static_manifest())

        # Both workspace sinks self-throttle on module state, and the views
        # sink also remembers which roots it has swept. All of it outlives one
        # test, so a tick driven without this reset silently does nothing.
        views._last_build = 0.0
        views._SWEPT.clear()
        space_json._last_build = 0.0
        watcher = watcher_module.Watcher.__new__(watcher_module.Watcher)
        watcher.sources = [_FakeSource(self._events())]
        watcher.model_by_session = {}
        watcher.tick_count = 0
        watcher.tick()

    # ── the two contracts, and the roots their tiers name ────────────

    def _contracts(self):
        from services.cowork_agent.quirq_catalog import _WORKSPACE_OUTPUT_CONTRACT

        return (
            (
                "project",
                _PROJECT_OUTPUT_CONTRACT,
                {
                    _TIER_SYNCED: project_layout.xo_dir(self.project),
                    _TIER_RUNTIME: project_layout.runtime_dir_for_project(
                        self.project
                    ),
                },
            ),
            (
                "workspace",
                _WORKSPACE_OUTPUT_CONTRACT,
                {
                    _TIER_SYNCED: project_layout.workspace_xo_dir(),
                    _TIER_RUNTIME: project_layout.workspace_runtime_dir(),
                },
            ),
        )

    @staticmethod
    def _present(path: Path) -> bool:
        """``sessionslist.d`` is a directory of shards, so "present" is
        "holds a file" for it and "is a file" for everything else — the
        same rule ``quirq_catalog._measure`` applies."""
        if path.is_file():
            return True
        return path.is_dir() and any(child.is_file() for child in path.iterdir())

    # ── the invariant ────────────────────────────────────────────────

    def test_every_row_is_declared_at_the_tier_its_writer_wrote_to(self) -> None:
        for label, contract, roots in self._contracts():
            for definition in contract:
                with self.subTest(contract=label, path=definition["path"]):
                    base = roots[definition["tier"]]
                    self.assertIsNotNone(base, "the tier has no root at all")
                    self.assertTrue(
                        self._present(base / definition["path"]),
                        f"the {label} row {definition['path']!r} is declared "
                        f"{definition['tier']}, but after running every real "
                        f"writer nothing is at {base / definition['path']}. "
                        f"Either the row's tier disagrees with its writer — "
                        f"the row renders '0 present' forever — or this test "
                        f"no longer drives the writer that produces it.",
                    )

    def test_no_row_is_also_satisfiable_from_the_other_tier(self) -> None:
        """Without this the test above could pass on a document that
        happens to exist under both roots, which is what a half-finished
        tier move looks like — and what T19/T20 had to sweep."""
        for label, contract, roots in self._contracts():
            for definition in contract:
                other = (
                    _TIER_RUNTIME if definition["tier"] == _TIER_SYNCED
                    else _TIER_SYNCED
                )
                with self.subTest(contract=label, path=definition["path"]):
                    stale = roots[other] / definition["path"]
                    self.assertFalse(
                        stale.exists(),
                        f"the {label} document {definition['path']!r} exists "
                        f"in both tiers ({stale} is the one nothing should "
                        f"write), so its declared tier cannot be checked",
                    )

    def test_the_catalog_reports_every_row_present_after_real_writes(self) -> None:
        """The user-visible end of it: ``quirq.js`` prints ``present_count``
        verbatim, so this is what the Quirq view actually shows."""
        outputs = quirq_catalog()["project_outputs"]
        for key in ("project_contract", "workspace_contract"):
            for row in outputs[key]:
                with self.subTest(contract=key, path=row["path"]):
                    self.assertEqual(
                        row["present_count"], 1,
                        f"{row['location']} renders '0 present' even though a "
                        f"real writer just produced it",
                    )
                    self.assertIsNotNone(row["updated_at"])


if __name__ == "__main__":
    unittest.main()
