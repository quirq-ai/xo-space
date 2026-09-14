"""Every xo-project carries the same ``.xo/``.

``tests/fixtures/xo-project/`` is the golden sample: what a project's ``.xo/``
holds right after XO Space first meets the folder, however it got there. These
tests hold four things to it:

1. the definition in ``services/xo_structure.py`` and the schemas;
2. every way a project comes to exist: scaffold, the clone API, project
   sharing's auto-clone, and a folder put into the root by hand and found by
   the watcher;
3. the stores that own each document, which must read the new documents and
   keep their shape on first write;
4. the rules that make the check safe to run against folders a person owns.

Identity values (``pid``, ``owner_user_id``, ``created_at``) are minted per
project, so they are checked for shape and then compared as placeholders.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import xo_structure
from services.cowork_agent import coder_identity, project_layout
from services.cowork_agent.visualizer import peers_store, todos_store, workitems_store
from services.cowork_agent.visualizer import watcher as watcher_mod
from services.storage import atomic_write
from utils.commands import CommandResult

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "xo-project"
FIXTURE_XO = FIXTURE / ".xo"
SCHEMAS = ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
PROJECT = "sample-project"

_PLACEHOLDERS = {
    "pid": "00000000-0000-4000-8000-000000000000",
    "owner_user_id": "local",
    "created_at": "2026-01-01T00:00:00Z",
}
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _files(directory: Path) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*"))


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_XO / name).read_text(encoding="utf-8"))


class CanonicalSampleTests(unittest.TestCase):
    """The sample, the code that creates it, and the schemas agree."""

    def test_the_sample_holds_exactly_the_canonical_files(self) -> None:
        self.assertEqual(
            _files(FIXTURE_XO), sorted(xo_structure.CANONICAL_FILES),
            "tests/fixtures/xo-project/.xo and xo_structure.CANONICAL_FILES "
            "disagree; change both together (new sample files need git add -f)",
        )

    def test_the_sample_documents_are_byte_for_byte_what_the_code_creates(self) -> None:
        for name, document in xo_structure.empty_documents().items():
            self.assertEqual(
                (FIXTURE_XO / name).read_bytes(),
                atomic_write._json_text(document).encode("utf-8"),
                name,
            )

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_the_sample_documents_satisfy_their_schemas(self) -> None:
        import jsonschema

        for name in ("project.json", "todos.json", "workitems.json", "peers.json"):
            schema = json.loads(
                (SCHEMAS / name.replace(".json", ".schema.json")).read_text(encoding="utf-8")
            )
            with self.subTest(document=name):
                jsonschema.Draft7Validator(schema).validate(_fixture(name))

    def test_the_template_ships_no_xo_files(self) -> None:
        template = ROOT / "services" / "cowork_agent" / "project_template"
        self.assertFalse(
            (template / ".xo").exists(),
            "the project template must not carry .xo/ files: services/xo_structure.py "
            "is the one definition, and a template copy would win on scaffold",
        )

    def test_the_sample_readme_names_every_file(self) -> None:
        readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
        for name in (*xo_structure.CANONICAL_FILES, *xo_structure.OPTIONAL_FILES):
            self.assertIn(f"`{name}`", readme)


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "projects"
        self.root.mkdir()
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(self.base / "state"),
            "QUIRQ_COMMAND_LOG": "off",
            "XO_SCHEDULER_ENABLED": "false",
        })
        env.start()
        self.addCleanup(env.stop)
        owner = patch.object(coder_identity, "resolve_user_id", return_value="local")
        owner.start()
        self.addCleanup(owner.stop)
        xo_structure._CHECKED.clear()
        self.addCleanup(xo_structure._CHECKED.clear)

    def folder(self, name: str = PROJECT) -> Path:
        project = self.root / name
        project.mkdir()
        (project / "README.md").write_text("a project\n", encoding="utf-8")
        return project

    def assert_matches_fixture(self, project: Path) -> None:
        xo = project / ".xo"
        self.assertEqual(_files(xo), _files(FIXTURE_XO))
        for name in xo_structure.CANONICAL_FILES:
            if name == "project.json":
                self.assert_identity_matches_fixture(xo / name)
            else:
                self.assertEqual((xo / name).read_bytes(), (FIXTURE_XO / name).read_bytes(), name)

    def assert_identity_matches_fixture(self, path: Path) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        expected = _fixture("project.json")
        self.assertEqual(list(document), list(expected), "project.json keys or key order")
        self.assertRegex(document["pid"], _UUID4)
        self.assertRegex(document["created_at"], _STAMP)
        self.assertTrue(isinstance(document["owner_user_id"], str) and document["owner_user_id"])
        self.assertEqual({**document, **_PLACEHOLDERS}, expected)


class CreationPathTests(_Sandbox):
    """Every way a project comes to exist ends with the same .xo/."""

    def test_a_folder_simply_put_in_the_root(self) -> None:
        project = self.folder()
        report = xo_structure.ensure_xo_structure(PROJECT)
        self.assertTrue(report.ok, report)
        self.assertEqual(report.created, xo_structure.CANONICAL_FILES)
        self.assert_matches_fixture(project)
        # Nothing outside .xo/ was touched.
        self.assertEqual(sorted(p.name for p in project.iterdir()), [".xo", "README.md"])

    def test_a_folder_cloned_by_hand_is_found_by_the_watcher(self) -> None:
        project = self.folder()
        with patch.object(watcher_mod, "get_active_agent", return_value=SimpleNamespace(name="stub")), \
             patch.object(watcher_mod, "try_load_capability", return_value=None):
            watcher = watcher_mod.Watcher()
        watcher.tick()
        self.assert_matches_fixture(project)

    def test_a_scaffolded_project(self) -> None:
        project_layout.scaffold_project(PROJECT)
        project = self.root / PROJECT
        self.assert_matches_fixture(project)
        # The template still supplies the work tier beside .xo/.
        self.assertTrue((project / "AGENTS.md").is_file())

    def test_a_scaffolded_project_keeps_what_the_person_typed(self) -> None:
        result = project_layout.scaffold_project(
            PROJECT, display_name="Sample Project", description="A sample.",
        )
        document = json.loads((self.root / PROJECT / ".xo" / "project.json").read_text())
        self.assertEqual(list(document), list(_fixture("project.json")))
        self.assertEqual(
            (document["display_name"], document["description"]),
            ("Sample Project", "A sample."),
        )
        self.assertEqual(result["pid"], document["pid"])

    def test_a_project_cloned_through_the_api(self) -> None:
        from services import project_management

        async def fake_git(argv, **options):
            target = Path(argv[-1])
            target.mkdir()
            (target / "README.md").write_text("cloned\n", encoding="utf-8")
            return CommandResult(argv=[], returncode=0, output="", duration_seconds=0)

        with patch.object(project_management, "run", new=fake_git):
            result = asyncio.run(project_management.clone_project(
                PROJECT, "https://example.com/org/sample-project.git",
            ))
        self.assertEqual(result, {"project_id": PROJECT, "created": True})
        self.assert_matches_fixture(self.root / PROJECT)

    def test_a_project_auto_cloned_by_project_sharing(self) -> None:
        from services.cowork_agent.project_sharing import clone, git_ops

        async def fake_clone(url, dest, *, config_args, cwd, timeout):
            (Path(dest) / ".git").mkdir(parents=True)
            return True, "", False

        with patch.object(clone, "_github_auth", new=AsyncMock(return_value=(None, False))), \
             patch.object(git_ops, "clone", new=fake_clone):
            result = asyncio.run(clone.clone_shared_repo("github.com/acme/sample-project"))
        self.assertEqual(result.state, "cloned")
        self.assert_matches_fixture(self.root / PROJECT)


class SafetyTests(_Sandbox):
    """Additive only, never raises, never writes outside .xo/."""

    def test_existing_files_are_never_rewritten(self) -> None:
        project = self.folder()
        xo = project / ".xo"
        xo.mkdir()
        existing = {
            "todos.json": b'{"schema": 2, "sessions": {"_project": {"runtime": "x", "todos": []}}}',
            "peers.json": b"not json",
        }
        for name, body in existing.items():
            (xo / name).write_bytes(body)
        report = xo_structure.ensure_xo_structure(PROJECT)
        for name, body in existing.items():
            self.assertEqual((xo / name).read_bytes(), body, name)
        self.assertEqual(report.created, ("project.json", "workitems.json"))

    def test_an_unreadable_project_json_is_reported_not_repaired(self) -> None:
        project = self.folder()
        xo = project / ".xo"
        xo.mkdir()
        (xo / "project.json").write_bytes(b"{broken")
        report = xo_structure.ensure_xo_structure(PROJECT)
        self.assertEqual((xo / "project.json").read_bytes(), b"{broken")
        self.assertTrue(any("project.json" in problem for problem in report.problems), report)
        self.assertEqual(report.created, ("todos.json", "workitems.json", "peers.json"))

    def test_an_existing_identity_is_kept_and_only_missing_fields_are_added(self) -> None:
        project = self.folder()
        (project / ".xo").mkdir()
        identity = {
            "schema": 2,
            "pid": "11111111-2222-4333-8444-555555555555",
            "name": PROJECT,
            "owner_user_id": "ada",
            "created_at": "2025-05-05T05:05:05Z",
        }
        (project / ".xo" / "project.json").write_text(json.dumps(identity), encoding="utf-8")
        xo_structure.ensure_xo_structure(PROJECT)
        document = json.loads((project / ".xo" / "project.json").read_text())
        self.assertEqual({key: document[key] for key in identity}, identity)
        self.assertEqual((document["display_name"], document["description"]), (PROJECT, ""))

    def test_a_second_check_changes_nothing(self) -> None:
        project = self.folder()
        xo_structure.ensure_xo_structure(PROJECT)
        before = {p.name: p.read_bytes() for p in (project / ".xo").iterdir()}
        report = xo_structure.ensure_xo_structure(PROJECT)
        self.assertEqual(report.created, ())
        self.assertTrue(report.ok, report)
        self.assertEqual({p.name: p.read_bytes() for p in (project / ".xo").iterdir()}, before)

    def test_a_former_projects_root_is_left_alone(self) -> None:
        project = self.folder("xo-projects")
        (project / ".xo").mkdir()
        (project / ".xo" / "space.json").write_text("{}", encoding="utf-8")
        report = xo_structure.ensure_xo_structure("xo-projects")
        self.assertIsNotNone(report.skipped)
        self.assertEqual(_files(project / ".xo"), ["space.json"])

    def test_a_symlinked_xo_is_never_written_through(self) -> None:
        project = self.folder()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (project / ".xo").symlink_to(elsewhere, target_is_directory=True)
        report = xo_structure.ensure_xo_structure(PROJECT)
        self.assertIsNotNone(report.skipped)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_missing_folder_is_not_created(self) -> None:
        report = xo_structure.ensure_xo_structure("ghost")
        self.assertIsNotNone(report.skipped)
        self.assertFalse((self.root / "ghost").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_a_read_only_folder_is_reported_not_raised(self) -> None:
        project = self.folder()
        project.chmod(0o555)
        self.addCleanup(project.chmod, 0o755)
        report = xo_structure.ensure_xo_structure(PROJECT)
        self.assertIsNotNone(report.skipped)
        self.assertFalse((project / ".xo").exists())

        project.chmod(0o755)
        xo = project / ".xo"
        xo.mkdir()
        xo.chmod(0o555)
        self.addCleanup(xo.chmod, 0o755)
        report = xo_structure.ensure_xo_structure(PROJECT)
        self.assertIsNone(report.skipped)
        self.assertTrue(report.problems)
        self.assertEqual(list(xo.iterdir()), [])


class StoreCompatibilityTests(_Sandbox):
    """The owning stores accept the new documents and keep their shape."""

    def test_the_stores_read_the_new_documents_as_empty(self) -> None:
        xo = self.folder() / ".xo"
        xo_structure.ensure_xo_structure(PROJECT)
        self.assertEqual(peers_store.list_peers(xo / "peers.json"), [])
        self.assertEqual(workitems_store.list_workitems(xo / "workitems.json"), [])
        self.assertIsNone(todos_store.get_todo(xo / "todos.json", "missing"))

    def test_a_first_store_write_keeps_the_document_shape(self) -> None:
        xo = self.folder() / ".xo"
        xo_structure.ensure_xo_structure(PROJECT)
        todos_store.create_todo(xo / "todos.json", runtime="test-runtime", content="first")
        workitems_store.create_workitem(xo / "workitems.json", runtime="test-runtime", title="first")
        peers_store.create_peer(xo / "peers.json", user_id="ada", role="collaborator")
        for name in ("todos.json", "workitems.json", "peers.json"):
            written = json.loads((xo / name).read_text(encoding="utf-8"))
            expected = _fixture(name)
            with self.subTest(document=name):
                self.assertEqual(list(written), list(expected))
                self.assertEqual(
                    (written["$schema"], written["schema"]),
                    (expected["$schema"], expected["schema"]),
                )
                self.assertIsNotNone(written["updated_at"])


class WatcherCheckTests(_Sandbox):
    def test_an_unchanged_xo_is_not_rechecked_and_a_deleted_file_comes_back(self) -> None:
        project = self.folder()
        first = xo_structure.ensure_xo_structure_if_changed(PROJECT)
        self.assertEqual(first.created, xo_structure.CANONICAL_FILES)
        self.assertIsNone(xo_structure.ensure_xo_structure_if_changed(PROJECT))

        xo = project / ".xo"
        (xo / "todos.json").unlink()
        # Make the change visible even on a filesystem with coarse timestamps.
        st = xo.stat()
        os.utime(xo, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        again = xo_structure.ensure_xo_structure_if_changed(PROJECT)
        self.assertEqual(again.created, ("todos.json",))
        self.assert_matches_fixture(project)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitTests(_Sandbox):
    def test_xo_shows_in_the_projects_git_status_so_it_can_be_committed(self) -> None:
        # .xo/ travels through git. An ignored .xo/ would let a fast-forward
        # merge overwrite local todos without a word.
        project = self.folder()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        xo_structure.ensure_xo_structure(PROJECT)
        status = subprocess.run(
            ["git", "-C", str(project), "status", "--porcelain", "--untracked-files=all"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn("README.md", status)
        for name in xo_structure.CANONICAL_FILES:
            self.assertIn(f".xo/{name}", status)


if __name__ == "__main__":
    unittest.main()
