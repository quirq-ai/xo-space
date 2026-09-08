"""T23 — the per-tick project-list memo and the dirname-map cache.

docs/syncplan.md §10 (T23): one watcher tick called ``list_project_ids()``
eight times and ``resolve_project_dirname()`` ``5N + E`` times, each of the
latter re-walking the whole root — a measured 3,410 ``stat()`` calls per
second at 20 projects.

The danger in fixing that is over-caching. ``list_project_ids`` is also on
the **request path**
(``routers/cowork_agent/bff/workspace_visualizer.py:77``), so a
module-level memo would hide a newly created project from the API until
restart. These tests pin both halves: the tick sees one consistent
snapshot, and everything outside a tick still sees the filesystem.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.project_layout import (
    resolve_project_dirname,
    xo_projects_root,
)
from services.cowork_agent.visualizer import watcher
from services.cowork_agent.visualizer import workspace_index
from services.cowork_agent.visualizer.workspace_index import (
    list_project_ids,
    project_index_scope,
)


def _make_project(root: Path, name: str, *, scaffolded: bool = True) -> Path:
    """Create a project directory; scaffolded ones get ``.xo/project.json``."""
    pdir = root / name
    if scaffolded:
        xo = pdir / ".xo"
        xo.mkdir(parents=True, exist_ok=True)
        (xo / "project.json").write_text(
            json.dumps({"name": name, "display_name": name, "description": ""}),
            encoding="utf-8",
        )
    else:
        pdir.mkdir(parents=True, exist_ok=True)
    return pdir


class ProjectIndexScopeTests(unittest.TestCase):
    """The memo exists only inside :func:`project_index_scope`."""

    def test_scope_serves_one_snapshot_for_its_whole_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "alpha")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                with project_index_scope():
                    first = list_project_ids()
                    _make_project(root, "beta")
                    second = list_project_ids()
                self.assertEqual(first, ["alpha"])
                self.assertEqual(second, ["alpha"])
                # Leaving the scope drops the memo entirely.
                self.assertEqual(list_project_ids(), ["alpha", "beta"])

    def test_scope_walks_the_root_exactly_once(self) -> None:
        """Eight calls inside one scope must cost one filesystem walk."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "alpha")
            _make_project(root, "bare", scaffolded=False)
            walks: list[str] = []
            real_list_projects = workspace_index.list_projects
            real_unscaffolded = workspace_index.list_unscaffolded_dirs

            def counted_list_projects():
                walks.append("list_projects")
                return real_list_projects()

            def counted_unscaffolded():
                walks.append("list_unscaffolded_dirs")
                return real_unscaffolded()

            with (
                patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}),
                patch.object(workspace_index, "list_projects", counted_list_projects),
                patch.object(
                    workspace_index,
                    "list_unscaffolded_dirs",
                    counted_unscaffolded,
                ),
            ):
                with project_index_scope():
                    for _ in range(8):
                        self.assertEqual(list_project_ids(), ["alpha", "bare"])

            self.assertEqual(walks, ["list_projects", "list_unscaffolded_dirs"])

    def test_memo_copy_cannot_be_poisoned_by_a_caller(self) -> None:
        """``projects_json`` puts the list straight into a payload; a
        caller mutating what it got back must not corrupt the memo."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "alpha")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                with project_index_scope():
                    got = list_project_ids()
                    got.append("injected")
                    self.assertEqual(list_project_ids(), ["alpha"])

    def test_scope_is_left_even_when_the_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "alpha")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                with self.assertRaises(RuntimeError):
                    with project_index_scope():
                        list_project_ids()
                        raise RuntimeError("tick blew up")
                _make_project(root, "beta")
                self.assertEqual(list_project_ids(), ["alpha", "beta"])

    def test_another_thread_never_inherits_the_memo(self) -> None:
        """A FastAPI request handler runs in its own context/thread: while
        a tick holds a scope, the request path must still hit the disk."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "alpha")
            seen: list[list[str]] = []

            def request_thread() -> None:
                seen.append(list_project_ids())

            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                with project_index_scope():
                    self.assertEqual(list_project_ids(), ["alpha"])
                    _make_project(root, "beta")
                    worker = threading.Thread(target=request_thread)
                    worker.start()
                    worker.join()
                    # The scope's own view is still the snapshot.
                    self.assertEqual(list_project_ids(), ["alpha"])

            self.assertEqual(seen, [["alpha", "beta"]])


class WatcherTickMemoTests(unittest.TestCase):
    """The scope is entered by ``Watcher.tick()`` and nowhere else."""

    def _env(self, tmp: str) -> dict[str, str]:
        return {
            "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            "XO_PROJECTS_ROOT": str(Path(tmp) / "projects"),
            # Keep the expensive, self-throttled views sink out of the way.
            "XO_VIEWS_REFRESH_S": "999999",
        }

    def _watcher(self) -> watcher.Watcher:
        with patch.object(watcher, "try_load_capability", return_value=None):
            return watcher.Watcher()

    def test_one_tick_walks_the_project_root_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir(parents=True)
            for name in ("alpha", "beta", "gamma"):
                _make_project(root, name)

            walks: list[str] = []
            real_list_projects = workspace_index.list_projects

            def counted():
                walks.append("walk")
                return real_list_projects()

            with (
                patch.dict(os.environ, self._env(tmp), clear=False),
                patch.object(workspace_index, "list_projects", counted),
            ):
                self._watcher().tick()

            self.assertEqual(len(walks), 1, f"expected 1 walk, got {len(walks)}")

    def test_tick_still_writes_the_heartbeat(self) -> None:
        """T22 shipped before this; the scope must not break it."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "projects").mkdir(parents=True)
            with patch.dict(os.environ, self._env(tmp), clear=False):
                instance = self._watcher()
                instance.tick()
                instance.tick()
                beat = json.loads(
                    (
                        Path(tmp) / ".quirq" / "watcher" / "heartbeat.json"
                    ).read_text(encoding="utf-8")
                )
            self.assertEqual(beat["tick_count"], 2)
            self.assertIn("last_tick_at", beat)
            self.assertIsInstance(beat["duration_ms"], int)

    def test_projects_json_lists_every_project_after_one_tick(self) -> None:
        """Threading the list through the sinks must not change what they
        write: the six workspace writers still see all projects.

        (``workspace.json`` became ``projects.json`` in syncplan T12, and
        its payload became a map keyed by directory name.)"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir(parents=True)
            _make_project(root, "alpha")
            _make_project(root, "bare", scaffolded=False)

            with patch.dict(os.environ, self._env(tmp), clear=False):
                self._watcher().tick()
                payload = json.loads(
                    (root / ".xo" / "projects.json").read_text(encoding="utf-8")
                )
            self.assertEqual(sorted(payload["projects"]), ["alpha", "bare"])

    def test_request_path_sees_a_project_created_after_a_tick(self) -> None:
        """The hazard the memo is scoped for: the BFF helper must never be
        served a stale project list."""
        from routers.cowork_agent.bff import workspace_visualizer as bff

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir(parents=True)
            _make_project(root, "alpha")

            with patch.dict(os.environ, self._env(tmp), clear=False):
                instance = self._watcher()
                instance.tick()
                self.assertEqual(bff._all_projects(), ["alpha"])

                # A project created through the API between ticks.
                _make_project(root, "beta")
                self.assertEqual(bff._all_projects(), ["alpha", "beta"])

                # And the next tick picks it up too.
                instance.tick()
                payload = json.loads(
                    (root / ".xo" / "projects.json").read_text(encoding="utf-8")
                )
            self.assertEqual(sorted(payload["projects"]), ["alpha", "beta"])


class ResolveDirnameCacheTests(unittest.TestCase):
    """The dirname listing is cached; the answers must not change."""

    def test_literal_name_wins_over_normalisation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "Agno-RAG-Tester")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                self.assertEqual(
                    resolve_project_dirname("Agno-RAG-Tester"), "Agno-RAG-Tester"
                )
                # Reverse lookup: a caller holding the normalised id still
                # lands on the real directory, and repeats are cache hits.
                for _ in range(3):
                    self.assertEqual(
                        resolve_project_dirname("agno-rag-tester"),
                        "Agno-RAG-Tester",
                    )

    def test_unknown_name_falls_back_to_the_normalised_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "alpha")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                self.assertEqual(resolve_project_dirname("alpha"), "alpha")
                self.assertEqual(
                    resolve_project_dirname("Brand New Thing"), "brand-new-thing"
                )
                # Hidden and unsafe segments are unchanged behaviour.
                self.assertEqual(resolve_project_dirname(".xo"), "xo")

    def test_repeat_calls_on_an_unchanged_root_walk_it_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            for name in ("alpha", "beta"):
                _make_project(root, name)
            real_iterdir = Path.iterdir
            walked: list[str] = []

            def counting_iterdir(self):
                walked.append(str(self))
                return real_iterdir(self)

            with (
                patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}),
                patch.object(Path, "iterdir", counting_iterdir),
            ):
                resolved = {resolve_project_dirname("alpha") for _ in range(20)}

            self.assertEqual(resolved, {"alpha"})
            self.assertEqual(
                [w for w in walked if w == str(root)],
                [str(root)],
                "the root listing must be walked exactly once for 20 lookups",
            )

    def test_a_new_project_is_visible_even_if_the_stamp_never_changes(self) -> None:
        """1-second mtime granularity on a bind mount can hide a change.
        Reaching the "no such directory" answer from cached data therefore
        forces one fresh walk before it is believed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _make_project(root, "alpha")
            frozen = (1, 1, 1, 1)
            with (
                patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}),
                patch.object(project_layout, "_root_stamp", lambda _root: frozen),
            ):
                self.assertEqual(resolve_project_dirname("alpha"), "alpha")
                _make_project(root, "Later-Project")
                self.assertEqual(
                    resolve_project_dirname("Later-Project"), "Later-Project"
                )
                self.assertEqual(
                    resolve_project_dirname("later-project"), "Later-Project"
                )

    def test_cache_is_keyed_per_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "a"
            root_b = Path(tmp) / "b"
            root_a.mkdir()
            root_b.mkdir()
            _make_project(root_a, "only-in-a")
            _make_project(root_b, "only-in-b")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root_a)}):
                self.assertEqual(resolve_project_dirname("only-in-a"), "only-in-a")
                self.assertEqual(resolve_project_dirname("only-in-b"), "only-in-b")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root_b)}):
                self.assertEqual(resolve_project_dirname("only-in-b"), "only-in-b")


class ProjectsRootTests(unittest.TestCase):
    """``xo_projects_root`` keeps create-on-read while dropping the mkdir."""

    def test_root_is_created_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "nested" / "projects"
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                self.assertFalse(root.exists())
                self.assertEqual(xo_projects_root(), root.resolve())
                self.assertTrue(root.is_dir())

    def test_root_is_recreated_after_it_disappears(self) -> None:
        """The cached resolution must never turn create-on-read off."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                xo_projects_root()
                self.assertTrue(root.is_dir())
                root.rmdir()
                self.assertFalse(root.exists())
                self.assertEqual(xo_projects_root(), root.resolve())
                self.assertTrue(root.is_dir())

    def test_repeat_calls_on_an_existing_root_never_mkdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                xo_projects_root()
                with patch("os.mkdir") as mkdir:
                    for _ in range(20):
                        xo_projects_root()
                mkdir.assert_not_called()

    def test_resolution_cache_keys_on_home(self) -> None:
        """The default root is ``~/xo-projects``: a changed HOME must not
        be served the previous home's resolution."""
        with tempfile.TemporaryDirectory() as tmp:
            home_a = Path(tmp) / "home-a"
            home_b = Path(tmp) / "home-b"
            home_a.mkdir()
            home_b.mkdir()
            env = {"HOME": str(home_a)}
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("XO_PROJECTS_ROOT", None)
                self.assertEqual(
                    xo_projects_root(), (home_a / "xo-projects").resolve()
                )
            with patch.dict(os.environ, {"HOME": str(home_b)}, clear=False):
                os.environ.pop("XO_PROJECTS_ROOT", None)
                self.assertEqual(
                    xo_projects_root(), (home_b / "xo-projects").resolve()
                )


if __name__ == "__main__":
    unittest.main()
