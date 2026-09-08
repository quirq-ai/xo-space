"""``<XO root>/.xo/projects.json`` — the projects registry (syncplan §5.2).

The keying is the thing worth pinning. ``projects`` is keyed by **directory
name** with ``pid`` as a field, and ``by_pid`` is a derived reverse index whose
values are **always arrays** — because a pid-keyed map cannot represent a
project that has no pid yet (every project is pid-less at birth), and silently
loses one of two folders that share a pid (``cp -r`` and restore both produce
that state).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer.workspace import projects_json


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
    / "projects.schema.json"
)


def _project(root: Path, name: str, *, pid: object = "unset") -> Path:
    pdir = root / name
    (pdir / ".xo").mkdir(parents=True)
    meta: dict = {"schema": 1, "name": name}
    if pid != "unset":
        meta["pid"] = pid
    (pdir / ".xo" / "project.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    return pdir


def _bare(root: Path, name: str) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True)
    return pdir


def _git_repo(pdir: Path, *, remote: str | None = None) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(pdir)], check=True)
    if remote:
        subprocess.run(
            ["git", "-C", str(pdir), "remote", "add", "origin", remote],
            check=True,
        )


class ProjectsRegistryShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        projects_json.reset_caches()

    def _apply(self, tmp: str) -> dict:
        env = {
            "XO_PROJECTS_ROOT": str(Path(tmp) / "projects"),
            "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
        }
        with patch.dict(os.environ, env, clear=False):
            projects_json.apply()
            return json.loads(projects_json.path().read_text(encoding="utf-8"))

    def test_projects_are_keyed_by_directory_name_with_pid_a_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "blackhole", pid="6f1c8a02-1111-2222-3333-444455556666")
            payload = self._apply(tmp)

        self.assertEqual(payload["schema"], 2)
        self.assertEqual(payload["$schema"], "xo/projects.schema.json")
        self.assertEqual(sorted(payload["projects"]), ["blackhole"])
        entry = payload["projects"]["blackhole"]
        self.assertEqual(entry["pid"], "6f1c8a02-1111-2222-3333-444455556666")
        self.assertTrue(entry["scaffolded"])
        self.assertEqual(
            payload["by_pid"],
            {"6f1c8a02-1111-2222-3333-444455556666": ["blackhole"]},
        )

    def test_a_bare_folder_is_listed_with_a_null_pid(self) -> None:
        """A pid-keyed map could not hold this row at all: JSON has no null
        key, and a folder the user simply dropped into the root may never
        receive a pid."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _bare(root, "scratch-notes")
            payload = self._apply(tmp)

        entry = payload["projects"]["scratch-notes"]
        self.assertIsNone(entry["pid"])
        self.assertFalse(entry["scaffolded"])
        self.assertEqual(payload["by_pid"], {})

    def test_a_present_but_null_pid_is_carried_as_null(self) -> None:
        """``scaffold_project`` ships every identity key present-but-null."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "fresh", pid=None)
            payload = self._apply(tmp)

        self.assertIsNone(payload["projects"]["fresh"]["pid"])
        self.assertTrue(payload["projects"]["fresh"]["scaffolded"])
        self.assertEqual(payload["by_pid"], {})

    def test_a_duplicate_pid_keeps_both_folders_and_raises_the_alarm(self) -> None:
        """``cp -r`` gives both copies the identical UUID and ``fill_identity``
        re-mints neither. Keyed by pid one folder would vanish; here the index
        value is a list of length two, which *is* the alarm."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "origin", pid="dupe-pid")
            _project(root, "restored-copy", pid="dupe-pid")
            payload = self._apply(tmp)

        self.assertEqual(sorted(payload["projects"]), ["origin", "restored-copy"])
        self.assertEqual(payload["by_pid"]["dupe-pid"], ["origin", "restored-copy"])
        self.assertGreater(len(payload["by_pid"]["dupe-pid"]), 1)

    def test_every_by_pid_value_is_an_array_even_for_one_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "solo", pid="solo-pid")
            payload = self._apply(tmp)

        for folders in payload["by_pid"].values():
            self.assertIsInstance(folders, list)

    def test_an_unusable_pid_stays_a_field_but_never_becomes_a_key(self) -> None:
        """A synced pid is untrusted input. As a value it is reported as it
        was found; as an object *key* it is clamped out entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "hostile", pid="../../etc/passwd")
            _project(root, "sane", pid="0d8f")
            payload = self._apply(tmp)

        self.assertEqual(payload["projects"]["hostile"]["pid"], "../../etc/passwd")
        self.assertEqual(list(payload["by_pid"]), ["0d8f"])

    def test_a_non_string_pid_is_reported_as_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "weird", pid=1234)
            payload = self._apply(tmp)

        self.assertIsNone(payload["projects"]["weird"]["pid"])
        self.assertEqual(payload["by_pid"], {})

    def test_the_watcher_list_is_reused_when_it_is_passed_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "alpha")
            _project(root, "beta")
            env = {
                "XO_PROJECTS_ROOT": str(root),
                "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(
                    projects_json, "list_project_ids", side_effect=AssertionError
                ):
                    projects_json.apply(["alpha", "beta"])
                payload = json.loads(
                    projects_json.path().read_text(encoding="utf-8")
                )

        self.assertEqual(sorted(payload["projects"]), ["alpha", "beta"])


class ProjectsRegistryGitTests(unittest.TestCase):
    def setUp(self) -> None:
        projects_json.reset_caches()

    def _apply(self, tmp: str, **extra: str) -> dict:
        env = {
            "XO_PROJECTS_ROOT": str(Path(tmp) / "projects"),
            "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            **extra,
        }
        with patch.dict(os.environ, env, clear=False):
            projects_json.apply()
            return json.loads(projects_json.path().read_text(encoding="utf-8"))

    def test_a_repo_reports_its_origin_with_credentials_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            pdir = _project(root, "cloned")
            _git_repo(pdir, remote="https://user:token@github.com/owner/repo.git")
            payload = self._apply(tmp)

        git = payload["projects"]["cloned"]["git"]
        self.assertTrue(git["is_repo"])
        self.assertEqual(git["remote_url"], "https://github.com/owner/repo.git")
        self.assertNotIn("token", json.dumps(payload))
        self.assertEqual(git["default_branch"], "main")

    def test_a_plain_folder_reports_no_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "plain")
            payload = self._apply(tmp)

        self.assertEqual(
            payload["projects"]["plain"]["git"],
            {"is_repo": False, "remote_url": None, "default_branch": None},
        )

    def test_a_folder_inside_a_larger_checkout_does_not_inherit_its_remote(
        self,
    ) -> None:
        """The projects root itself being a repository must not attribute its
        origin to every project under it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _git_repo(root, remote="https://github.com/owner/parent.git")
            _project(root, "nested")
            payload = self._apply(tmp)

        self.assertEqual(
            payload["projects"]["nested"]["git"],
            {"is_repo": False, "remote_url": None, "default_branch": None},
        )

    def test_git_is_not_shelled_out_on_every_tick(self) -> None:
        """The cost gate: this file is rewritten every tick, and two ``git``
        spawns per project per second is exactly what the plan forbids."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            pdir = _project(root, "cloned")
            _git_repo(pdir, remote="https://github.com/owner/repo.git")

            calls: list[Path] = []

            def counted(target: Path) -> dict:
                calls.append(target)
                return {"remote_url": None, "default_branch": None}

            env = {
                "XO_PROJECTS_ROOT": str(root),
                "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(projects_json, "git_provenance", counted):
                    for _ in range(5):
                        projects_json.apply()
                    self.assertEqual(len(calls), 1)

                    # ...and the interval is what holds it back, not a
                    # one-shot: drop the window and it reads again.
                    with patch.dict(
                        os.environ, {"XO_GIT_PROVENANCE_REFRESH_S": "0"}
                    ):
                        projects_json.apply()
                    self.assertEqual(len(calls), 2)

    def test_git_init_between_ticks_is_noticed_without_waiting_for_the_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            pdir = _project(root, "later")
            before = self._apply(tmp)
            self.assertFalse(before["projects"]["later"]["git"]["is_repo"])

            _git_repo(pdir, remote="https://github.com/owner/repo.git")
            after = self._apply(tmp)

        self.assertTrue(after["projects"]["later"]["git"]["is_repo"])


class ProjectsRegistryWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        projects_json.reset_caches()

    def test_an_unchanged_registry_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "alpha", pid="alpha-pid")
            env = {
                "XO_PROJECTS_ROOT": str(root),
                "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertTrue(projects_json.apply())
                stamp = projects_json.path().stat().st_mtime_ns
                self.assertFalse(projects_json.apply())
                self.assertFalse(projects_json.apply())
                self.assertEqual(projects_json.path().stat().st_mtime_ns, stamp)

                # a new project is a change, and it lands
                _project(root, "beta", pid="beta-pid")
                self.assertTrue(projects_json.apply())

    def test_a_restart_compares_against_the_file_not_an_empty_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "alpha", pid="alpha-pid")
            env = {
                "XO_PROJECTS_ROOT": str(root),
                "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            }
            with patch.dict(os.environ, env, clear=False):
                projects_json.apply()
                stamp = projects_json.path().stat().st_mtime_ns
                projects_json.reset_caches()          # as a fresh process
                self.assertFalse(projects_json.apply())
                self.assertEqual(projects_json.path().stat().st_mtime_ns, stamp)


class ProjectsRegistryCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        projects_json.reset_caches()

    def test_the_reader_prefers_projects_json_and_falls_back_to_workspace_json(
        self,
    ) -> None:
        from services.cowork_agent import scopes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            (root / ".xo").mkdir(parents=True)
            env = {
                "XO_PROJECTS_ROOT": str(root),
                "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            }
            with patch.dict(os.environ, env, clear=False):
                (root / ".xo" / "workspace.json").write_text(
                    json.dumps({"schema": 1, "projects": ["legacy"]}),
                    encoding="utf-8",
                )
                scope = scopes.WorkspaceVisualizerScope()
                self.assertEqual(scope.read_projects()["projects"], ["legacy"])
                # the deprecated name still answers, for one release
                self.assertEqual(scope.read_workspace()["projects"], ["legacy"])

                projects_json.apply()
                scope = scopes.WorkspaceVisualizerScope()
                fresh = scope.read_projects()

        self.assertEqual(fresh["schema"], 2)
        self.assertIsInstance(fresh["projects"], dict)


class ProjectsRegistrySchemaTests(unittest.TestCase):
    """The schema is documentation until something reads it; this at least
    keeps it from drifting from its own writer (T16 makes it executable)."""

    def setUp(self) -> None:
        projects_json.reset_caches()

    def test_the_writer_emits_exactly_what_the_schema_declares(self) -> None:
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            root.mkdir()
            _project(root, "alpha", pid="alpha-pid")
            env = {
                "XO_PROJECTS_ROOT": str(root),
                "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            }
            with patch.dict(os.environ, env, clear=False):
                projects_json.apply()
                payload = json.loads(
                    projects_json.path().read_text(encoding="utf-8")
                )

        self.assertEqual(schema["$id"], "xo/projects.schema.json")
        self.assertEqual(sorted(payload), sorted(schema["properties"]))
        for key in schema["required"]:
            self.assertIn(key, payload)
        entry_schema = schema["properties"]["projects"]["additionalProperties"]
        self.assertEqual(
            sorted(payload["projects"]["alpha"]),
            sorted(entry_schema["properties"]),
        )
        git_schema = entry_schema["properties"]["git"]
        self.assertEqual(
            sorted(payload["projects"]["alpha"]["git"]),
            sorted(git_schema["properties"]),
        )

    def test_the_retired_workspace_schema_is_gone(self) -> None:
        self.assertFalse((SCHEMA_FILE.parent / "workspace.schema.json").exists())


if __name__ == "__main__":
    unittest.main()
