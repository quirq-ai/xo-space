"""``scopes.py`` — the three-root split, and what the BFF routes get from it.

docs/syncplan.md §9 T19 / Appendix A.2. A visualizer scope used to be one root:
``<project>/.xo/``, joined to five filenames. The tier move split it in three.

* **synced** — ``<project>/.xo/`` — ``todos.json`` and the four todo CRUD
  methods. Part of what a clone would want, so it stays in the project tree.
* **runtime** — ``~/.quirq/projects/<key>/`` — ``stats.json``,
  ``timeline.jsonl`` and the session index. Machine-local derived state.
* **activity** — ``~/.quirq/watcher/`` — unchanged; it already lived there.

Two things make this more than a path edit, and both are pinned below:

1. ``VisualizerScope.__init__`` was pure path arithmetic. It now does a **JSON
   read per construction**, because the runtime root is keyed by
   ``project.json:pid``. A project with no pid — never minted, folder deleted,
   ``project.json`` corrupt — has no runtime root at all, and every read must
   answer empty rather than raise. A scope is built on the request path, so a
   500 here is a 500 for the user.
2. **13 BFF routes** reach a read that moved. They pass a scope handle around
   and never touch a path, so none of them changed — which is exactly the
   claim worth testing rather than assuming.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout, scopes
from services.cowork_agent.engine import sessions_io as session_index

# The 13 routes the plan names (bff/visualizer.py 6, bff/workspace_visualizer.py
# 7). Every one reaches read_stats / read_timeline / read_sessionslist /
# read_one_session — i.e. a file that moved out of the project tree.
PROJECT_ROUTES = (
    "/api/xo-projects/{pid}/usage/summary/card",
    "/api/xo-projects/{pid}/usage/analytics",
    "/api/xo-projects/{pid}/usage/sessions",
    "/api/xo-projects/{pid}/usage/summary",
    "/api/xo-projects/{pid}/usage/sessions/sess-1",
    "/api/xo-projects/{pid}/timeline",
)
WORKSPACE_ROUTES = (
    "/api/xo-projects/usage",
    "/api/xo-projects/usage/summary/card",
    "/api/xo-projects/usage/analytics",
    "/api/xo-projects/usage/sessions",
    "/api/xo-projects/usage/summary",
    "/api/xo-projects/usage/sessions/sess-1",
    "/api/xo-projects/timeline",
)


class _ScopeCase(unittest.TestCase):
    PROJECT = "demo"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "xo-projects"
        self.root.mkdir(parents=True)
        self.state = self.tmp / "state"
        self.state.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
                "XO_PROJECT_TEMPLATE": "",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        self.xo = self.root / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)

    # ── helpers ──────────────────────────────────────────────────────────
    def mint_pid(self, pid: str = "11112222-3333-4444-5555-666677778888") -> str:
        (self.xo / "project.json").write_text(
            json.dumps({"schema": 2, "pid": pid, "name": self.PROJECT}), encoding="utf-8"
        )
        return pid

    def runtime(self) -> Path:
        path = project_layout.runtime_dir_for_project(self.PROJECT)
        self.assertIsNotNone(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def scope(self, name: str | None = None):
        return scopes.resolve_scope("xo-projects-visualizer", name or self.PROJECT)


class ThreeRootTests(_ScopeCase):
    def test_the_scope_names_three_distinct_roots(self) -> None:
        pid = self.mint_pid()
        scope = self.scope()

        self.assertEqual(scope._xo_root, self.xo)
        self.assertEqual(scope._runtime_root, self.state / "projects" / pid)
        self.assertTrue(
            str(scope._activity_path).startswith(str(self.state / "watcher")),
            scope._activity_path,
        )
        # The runtime root is outside the project tree — that is the point of
        # the move, and it is what makes share-safety a filesystem invariant
        # rather than a .gitignore policy.
        self.assertNotIn(str(self.root), str(scope._runtime_root))

    def test_todos_are_read_from_the_synced_root(self) -> None:
        self.mint_pid()
        (self.xo / "todos.json").write_text(
            json.dumps({"schema": 2, "sessions": {}}), encoding="utf-8"
        )
        self.assertEqual(self.scope().read_todos()["schema"], 2)

    def test_stats_are_read_from_the_runtime_root(self) -> None:
        self.mint_pid()
        (self.runtime() / "stats.json").write_text(
            json.dumps({"schema": 2, "marker": "runtime"}), encoding="utf-8"
        )
        self.assertEqual(self.scope().read_stats()["marker"], "runtime")

    def test_the_timeline_is_read_from_the_runtime_root(self) -> None:
        self.mint_pid()
        (self.runtime() / "timeline.jsonl").write_text(
            json.dumps({"ts": "2026-09-07T12:00:00Z", "type": "session.started"}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(len(self.scope().read_timeline(limit=10)), 1)

    def test_the_session_index_is_read_from_the_runtime_root(self) -> None:
        self.mint_pid()
        session_index.write_session_row(
            self.PROJECT, "a:k", {"sessionId": "sess-1", "backend": "codex"}
        )
        self.assertEqual(list(self.scope().read_sessionslist()), ["a:k"])

    def test_read_one_session_resolves_by_all_three_handles(self) -> None:
        self.mint_pid()
        session_index.write_session_row(
            self.PROJECT, "a:k",
            {"sessionId": "sess-1", "nativeSessionId": "native-1", "backend": "codex"},
        )
        for handle in ("a:k", "sess-1", "native-1"):
            with self.subTest(handle=handle):
                found = self.scope().read_one_session(handle)
                self.assertIsNotNone(found)
                self.assertEqual(found[0], "a:k")

    def test_constructing_a_scope_creates_nothing(self) -> None:
        """The runtime helper is reached on read paths; a helper that mkdir'd
        would conjure a directory for every id anyone ever asks about."""
        self.mint_pid()
        before = sorted(str(p) for p in self.state.rglob("*"))
        self.scope()
        self.assertEqual(sorted(str(p) for p in self.state.rglob("*")), before)


class NoPidTests(_ScopeCase):
    """A pid may legitimately never exist — the identity sink refuses to mint
    one for a project folder that isn't there (``project_json.py``). The scope
    has to be an empty reader in that case, never an error."""

    def test_a_project_with_no_project_json_reads_empty(self) -> None:
        scope = self.scope()
        self.assertIsNone(scope.read_stats())
        self.assertEqual(scope.read_sessionslist(), {})
        self.assertEqual(scope.read_timeline(limit=10), [])
        self.assertIsNone(scope.read_one_session("anything"))

    def test_a_project_that_does_not_exist_reads_empty(self) -> None:
        scope = self.scope("no-such-project")
        self.assertFalse(scope.project_exists())
        self.assertIsNone(scope.read_stats())
        self.assertIsNone(scope.read_todos())
        self.assertEqual(scope.read_sessionslist(), {})
        self.assertEqual(scope.read_timeline(limit=10), [])
        self.assertIsNone(scope.read_activity())
        self.assertIsNone(scope.read_one_session("anything"))

    def test_a_corrupt_project_json_reads_empty_rather_than_raising(self) -> None:
        (self.xo / "project.json").write_text("{not json", encoding="utf-8")
        scope = self.scope()
        self.assertIsNone(scope.read_stats())
        self.assertEqual(scope.read_sessionslist(), {})

    def test_a_hostile_pid_does_not_escape_the_runtime_home(self) -> None:
        """``project.json`` is synced, so its pid arrives from other machines
        and from snapshot restores. An unusable one falls back to the folder
        name — wrong-but-contained — and must never resolve outside."""
        self.mint_pid("../../../../etc")
        scope = self.scope()
        self.assertIsNotNone(scope._runtime_root)
        self.assertTrue(
            str(scope._runtime_root).startswith(str(self.state / "projects")),
            scope._runtime_root,
        )
        self.assertIsNone(scope.read_stats())

    def test_reading_a_missing_project_creates_nothing(self) -> None:
        self.scope("no-such-project").read_sessionslist()
        self.assertFalse((self.root / "no-such-project").exists())
        self.assertFalse((self.state / "projects" / "no-such-project").exists())


class ReadThroughTests(_ScopeCase):
    """Migration is read-through + copy-on-first-write; there is no mover."""

    def test_a_pre_move_stats_file_still_serves(self) -> None:
        self.mint_pid()
        (self.xo / "stats.json").write_text(
            json.dumps({"schema": 1, "marker": "legacy"}), encoding="utf-8"
        )
        self.assertEqual(self.scope().read_stats()["marker"], "legacy")

    def test_the_runtime_copy_wins_once_it_exists(self) -> None:
        self.mint_pid()
        (self.xo / "stats.json").write_text(
            json.dumps({"marker": "legacy"}), encoding="utf-8"
        )
        (self.runtime() / "stats.json").write_text(
            json.dumps({"marker": "runtime"}), encoding="utf-8"
        )
        self.assertEqual(self.scope().read_stats()["marker"], "runtime")

    def test_a_pre_move_session_index_still_serves(self) -> None:
        self.mint_pid()
        legacy = self.xo / "sessions"
        legacy.mkdir(parents=True)
        (legacy / "sessionslist.json").write_text(
            json.dumps({"old:k": {"sessionId": "sess-old", "backend": "openclaw"}}),
            encoding="utf-8",
        )
        self.assertEqual(list(self.scope().read_sessionslist()), ["old:k"])

    def test_the_timeline_is_deliberately_not_read_through(self) -> None:
        """Open decision O3, default applied: ``timeline.jsonl`` is append-only
        WITH rotation, so a read-through would have to reconcile the
        ``timeline.<stamp>.jsonl`` glob across two roots. History is dropped
        rather than half-merged."""
        self.mint_pid()
        (self.xo / "timeline.jsonl").write_text(
            json.dumps({"ts": "2026-09-07T12:00:00Z", "type": "session.started"}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(self.scope().read_timeline(limit=10), [])


class WorkspaceScopeTests(_ScopeCase):
    """T20 split the workspace tier the same way T19 split the per-project one.

    Its synced root keeps the records (``space.json``, ``projects.json``,
    ``xo.json``); its runtime root is ``~/.quirq/workspace/`` and holds every
    rollup the watcher derives from a walk of the projects. Reading a rollup
    from the old root would have been the same silent empty this whole phase
    is written against, so the split is pinned here rather than assumed.
    """

    def test_the_workspace_scope_splits_its_two_roots(self) -> None:
        workspace = scopes.resolve_scope("xo-workspace-visualizer")
        self.assertEqual(workspace._xo_root, project_layout.workspace_xo_dir())
        self.assertEqual(
            workspace._runtime_root, project_layout.workspace_runtime_dir()
        )
        self.assertNotEqual(workspace._runtime_root, workspace._xo_root)

    def test_the_workspace_rollups_are_read_from_the_runtime_root(self) -> None:
        runtime = project_layout.workspace_runtime_dir()
        runtime.mkdir(parents=True)
        (runtime / "stats.json").write_text(
            json.dumps({"schema": 2, "rolling": {}}), encoding="utf-8"
        )
        (runtime / "timeline.jsonl").write_text(
            json.dumps({"ts": "2026-09-07T12:00:00Z", "type": "session.started"})
            + "\n",
            encoding="utf-8",
        )
        workspace = scopes.resolve_scope("xo-workspace-visualizer")
        self.assertEqual(workspace.read_stats(), {"schema": 2, "rolling": {}})
        self.assertEqual(len(workspace.read_timeline(limit=10)), 1)

    def test_a_rollup_left_in_the_synced_root_is_not_read(self) -> None:
        """No read-through here, unlike the per-project tier: these files are
        recomputed in full every tick, so the runtime copy exists within a
        second of the move and ``views.sweep_abandoned`` deletes the old one."""
        xo = project_layout.workspace_xo_dir()
        xo.mkdir(parents=True)
        (xo / "stats.json").write_text(
            json.dumps({"schema": 2, "stale": True}), encoding="utf-8"
        )
        workspace = scopes.resolve_scope("xo-workspace-visualizer")
        self.assertIsNone(workspace.read_stats())

    def test_the_workspace_session_index_is_still_one_file(self) -> None:
        ws = project_layout.workspace_sessions_dir()
        ws.mkdir(parents=True)
        (ws / "sessionslist.json").write_text(
            json.dumps({"a:k": {"sessionId": "sess-1", "backend": "codex"}}),
            encoding="utf-8",
        )
        workspace = scopes.resolve_scope("xo-workspace-visualizer")
        self.assertEqual(list(workspace.read_sessionslist()), ["a:k"])

    def test_workspace_reads_are_empty_before_the_first_tick(self) -> None:
        workspace = scopes.resolve_scope("xo-workspace-visualizer")
        self.assertIsNone(workspace.read_stats())
        self.assertEqual(workspace.read_sessionslist(), {})
        self.assertEqual(workspace.read_timeline(limit=10), [])


class BffRouteTests(_ScopeCase):
    """The 13 routes that reach a moved read. None of them changed — they hold
    a scope handle, never a path — so this is the check that the handle really
    absorbed the whole move."""

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers.cowork_agent.bff.visualizer import router as project_router
        from routers.cowork_agent.bff.workspace_visualizer import (
            router as workspace_router,
        )

        app = FastAPI()
        app.include_router(workspace_router)
        app.include_router(project_router)
        return TestClient(app)

    def _populate(self) -> None:
        self.mint_pid()
        session_index.write_session_row(
            self.PROJECT, "a:k",
            {
                "sessionId": "sess-1",
                "nativeSessionId": "native-1",
                "backend": "codex",
                "directory": str(self.root / self.PROJECT),
                "updatedAt": 1,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
        (self.runtime() / "stats.json").write_text(
            json.dumps({
                "schema": 2,
                "rolling": {},
                "by_session": {"native-1": {"tokens": {"input": 1, "output": 1}}},
                "by_runtime": {},
            }),
            encoding="utf-8",
        )
        (self.runtime() / "timeline.jsonl").write_text(
            json.dumps({
                "ts": "2026-09-07T12:00:00Z",
                "type": "session.started",
                "session_id": "native-1",
                "runtime": "codex",
            }) + "\n",
            encoding="utf-8",
        )

    def test_every_project_route_serves_a_populated_project(self) -> None:
        self._populate()
        with self._client() as client:
            for route in PROJECT_ROUTES:
                with self.subTest(route=route):
                    resp = client.get(route.format(pid=self.PROJECT))
                    self.assertEqual(resp.status_code, 200, resp.text)

    def test_every_workspace_route_serves_a_populated_workspace(self) -> None:
        self._populate()
        with self._client() as client:
            for route in WORKSPACE_ROUTES:
                with self.subTest(route=route):
                    resp = client.get(route)
                    self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_pid_less_project_is_empty_state_not_a_500(self) -> None:
        """The project folder exists but identity was never minted, so there is
        no runtime root. Every route must still answer."""
        with self._client() as client:
            for route in PROJECT_ROUTES:
                with self.subTest(route=route):
                    resp = client.get(route.format(pid=self.PROJECT))
                    self.assertNotEqual(resp.status_code, 500, resp.text)
                    self.assertIn(resp.status_code, (200, 404), resp.text)

    def test_a_missing_project_is_a_404_not_a_500(self) -> None:
        with self._client() as client:
            for route in PROJECT_ROUTES:
                with self.subTest(route=route):
                    resp = client.get(route.format(pid="no-such-project"))
                    self.assertNotEqual(resp.status_code, 500, resp.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
