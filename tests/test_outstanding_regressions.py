"""Regressions for the ``docs/OUTSTANDING.md`` follow-ups O-A, O-C and O-K.

Companion to ``test_defect_regressions.py``, kept separate because these are
a different kind of miss. Those six were *wrong code*; these three are places
where a design was declared and only partly wired — a field with no writer, a
key with no qualifier, a schema block nothing filled in. Nothing was broken
enough to fail a gate, because in each case the code did exactly what it said
and the missing half was somewhere else.

O-B lives in ``test_git_provenance.py`` instead: it is a property of the URL
sanitiser and belongs with the rest of that surface's tests.

Every test below fails against the code as it shipped on ``sync-compatible``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.sinks import project_json
from services.cowork_agent.visualizer.workspace import projects_json
from services.cowork_agent.visualizer.workspace import sessions_augment as ws_augment
from services.cowork_agent.visualizer.workspace_index import (
    list_project_ids,
    list_project_pids,
)


def _scaffold(root: Path, name: str, *, pid: str | None = None, **extra) -> Path:
    """A project with a filled-in ``project.json``. Returns its dir."""
    pdir = root / name
    (pdir / ".xo").mkdir(parents=True)
    meta: dict = {
        "schema": 2,
        "pid": pid,
        "name": name,
        "owner_user_id": "local",
        "created_at": "2026-01-01T00:00:00Z",
    }
    meta.update(extra)
    (pdir / ".xo" / "project.json").write_text(json.dumps(meta), encoding="utf-8")
    return pdir


def _git_repo(pdir: Path, remote: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(pdir)], check=True)
    subprocess.run(
        ["git", "-C", str(pdir), "remote", "add", "origin", remote], check=True
    )


class _RootedTest(unittest.TestCase):
    """Every test here needs both roots pointed somewhere disposable."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        env = {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(Path(self._tmp.name) / ".quirq"),
        }
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        projects_json.reset_caches()
        ws_augment.reset_caches()
        self.addCleanup(projects_json.reset_caches)
        self.addCleanup(ws_augment.reset_caches)


# ── O-A — deleted_by was structurally unreachable over HTTP ──────────────────


class TombstoneAttributionTests(_RootedTest):
    """``scopes.delete_todo`` forwarded no kwargs, so the store parameter that
    records *who* tombstoned a todo could never be set from a request. The
    store side always worked; the path to it did not exist."""

    PROJECT = "demo"

    def setUp(self) -> None:
        super().setUp()
        _scaffold(self.root, self.PROJECT, pid="00000001-0000-4000-8000-000000000001")
        app = FastAPI()
        from routers.cowork_agent.bff.visualizer import router

        app.include_router(router)
        self.client = TestClient(app)
        self.base = f"/api/xo-projects/{self.PROJECT}/todos"

    def _create(self, content: str) -> str:
        res = self.client.post(
            self.base, json={"runtime": "claude_code", "content": content}
        )
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["id"]

    def _stored(self, todo_id: str) -> dict:
        doc = json.loads(
            (self.root / self.PROJECT / ".xo" / "todos.json").read_text("utf-8")
        )
        for todo in doc["sessions"]["_project"]["todos"]:
            if todo["id"] == todo_id:
                return todo
        raise AssertionError(f"todo {todo_id} not found on disk")

    def test_runtime_reaches_deleted_by(self) -> None:
        """The whole point: attribution survives the round trip to disk."""
        todo_id = self._create("attributed")
        res = self.client.request(
            "DELETE", f"{self.base}/{todo_id}", params={"runtime": "claude_code"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["deleted"])
        self.assertEqual(self._stored(todo_id)["deleted_by"], "claude_code")

    def test_attribution_is_optional_and_the_wire_is_unchanged(self) -> None:
        """Existing callers pass no ``runtime`` and must be unaffected — same
        status, same response body, still tombstoned, just unattributed."""
        todo_id = self._create("unattributed")
        res = self.client.request("DELETE", f"{self.base}/{todo_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            res.json(),
            {"project_id": self.PROJECT, "todo_id": todo_id, "deleted": True},
        )
        stored = self._stored(todo_id)
        self.assertIsNone(stored["deleted_by"])
        self.assertIsNotNone(stored["deleted_at"])

    def test_a_bad_runtime_is_a_400_and_writes_nothing(self) -> None:
        """``todos.json`` is synced, so attribution may not be a channel for
        arbitrary caller text. And the rejection must not be reported as
        "todos.json write failed" — nothing was written."""
        todo_id = self._create("bad runtime")
        res = self.client.request(
            "DELETE", f"{self.base}/{todo_id}", params={"runtime": "bad value"}
        )
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["detail"]["code"], "invalid_runtime")
        self.assertIsNone(self._stored(todo_id).get("deleted_at"))

    def test_idempotency_is_preserved(self) -> None:
        """A second DELETE stays a no-op ``deleted: false`` — and must not
        rewrite the first tombstone's author."""
        todo_id = self._create("twice")
        self.client.request(
            "DELETE", f"{self.base}/{todo_id}", params={"runtime": "claude_code"}
        )
        res = self.client.request(
            "DELETE", f"{self.base}/{todo_id}", params={"runtime": "openclaw"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["deleted"])
        self.assertEqual(self._stored(todo_id)["deleted_by"], "claude_code")


# ── O-C — the "_project" pseudo-session collided across projects ─────────────


class WorkspaceUnionKeyTests(_RootedTest):
    """``_project`` is a constant, not an identifier. Every project holding an
    API-created todo emitted a row under that one key, and the flat union kept
    whichever project was written last."""

    def _augment(self, name: str, *, task_total: int, session_key: str) -> None:
        rt = project_layout.runtime_sessions_dir_for_project(name, create=True)
        (rt / "sessions-augment.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "sessions": {
                        "_project": {"taskCount": {"total": task_total}},
                        session_key: {"taskCount": {"total": 1}},
                    },
                }
            ),
            encoding="utf-8",
        )

    def _union(self) -> dict:
        ws_augment.apply()
        target = project_layout.workspace_sessions_dir() / "sessions-augment.json"
        return json.loads(target.read_text("utf-8"))["sessions"]

    def test_every_project_keeps_its_project_row(self) -> None:
        _scaffold(self.root, "alpha", pid="00000001-0000-4000-8000-000000000001")
        _scaffold(self.root, "zulu", pid="00000002-0000-4000-8000-000000000002")
        self._augment("alpha", task_total=10, session_key="hermes:a:web:aaaaaaa1")
        self._augment("zulu", task_total=20, session_key="hermes:z:web:aaaaaaa2")

        sessions = self._union()
        totals = sorted(
            row["taskCount"]["total"]
            for key, row in sessions.items()
            if key.endswith("_project")
        )
        # Before the fix this was [20] — alpha's row was overwritten.
        self.assertEqual(totals, [10, 20])

    def test_the_qualifier_is_the_pid_not_the_folder_name(self) -> None:
        """``pid`` survives a rename and the directory name does not, so the
        aggregate row must not move when a folder is renamed."""
        pid = "00000001-0000-4000-8000-000000000001"
        _scaffold(self.root, "alpha", pid=pid)
        self._augment("alpha", task_total=10, session_key="hermes:a:web:aaaaaaa1")

        self.assertIn(f"{pid}/_project", self._union())

    def test_real_session_keys_are_never_rewritten(self) -> None:
        """``reader.merge_sessionslist`` joins this file to the workspace
        ``sessionslist.json`` by key and drops what it cannot match, so
        qualifying real keys would silently empty the merge."""
        _scaffold(self.root, "alpha", pid="00000001-0000-4000-8000-000000000001")
        self._augment("alpha", task_total=10, session_key="hermes:a:web:aaaaaaa1")

        sessions = self._union()
        self.assertIn("hermes:a:web:aaaaaaa1", sessions)
        self.assertEqual(
            [k for k in sessions if k.startswith("hermes:")], ["hermes:a:web:aaaaaaa1"]
        )

    def test_a_pidless_project_falls_back_to_its_folder_name(self) -> None:
        """The pre-mint window still needs *a* qualifier; it just cannot be
        the pid yet."""
        _scaffold(self.root, "premint", pid=None)
        self._augment("premint", task_total=7, session_key="hermes:p:web:aaaaaaa3")

        self.assertIn("premint/_project", self._union())


class ProjectPidListingTests(_RootedTest):
    """``list_project_pids`` is a second projection of the same walk, kept
    separate from ``list_project_ids`` because the directory name is the
    lookup key everywhere and the pid is the durable identity."""

    def test_ids_stay_names_and_pids_are_a_separate_mapping(self) -> None:
        pid = "00000001-0000-4000-8000-000000000001"
        _scaffold(self.root, "alpha", pid=pid)
        (self.root / "bare").mkdir()

        self.assertEqual(list_project_ids(), ["alpha", "bare"])
        self.assertEqual(list_project_pids(), {"alpha": pid, "bare": None})

    def test_a_renamed_folder_keeps_its_pid(self) -> None:
        """The stored ``name`` goes stale on a rename; the pid does not."""
        pid = "00000001-0000-4000-8000-000000000001"
        _scaffold(self.root, "alpha", pid=pid)
        (self.root / "alpha").rename(self.root / "renamed")

        self.assertEqual(list_project_pids(), {"renamed": pid})

    def test_a_blank_pid_reads_as_absent(self) -> None:
        _scaffold(self.root, "alpha", pid="   ")
        self.assertEqual(list_project_pids(), {"alpha": None})


# ── O-K — project.json:git was declared but had no writer ────────────────────


class DurableGitProvenanceTests(_RootedTest):
    """``project.schema.json`` declares ``git`` and syncplan §5.1 assigns it to
    "the git refresher", but ``git_provenance()`` had exactly one caller —
    ``projects.json``, the machine-local cache that snapshots exclude. The
    durable half of the pair was never wired."""

    def _refresh(self) -> dict:
        with patch.dict(os.environ, {"XO_GIT_PROVENANCE_REFRESH_S": "0"}):
            projects_json.apply()
        return json.loads(
            (self.root / "demo" / ".xo" / "project.json").read_text("utf-8")
        )

    def test_provenance_lands_in_project_json(self) -> None:
        pdir = _scaffold(
            self.root, "demo", pid="00000001-0000-4000-8000-000000000001"
        )
        _git_repo(pdir, "https://github.com/owner/repo.git")

        doc = self._refresh()
        self.assertEqual(
            doc["git"],
            {"remote_url": "https://github.com/owner/repo.git",
             "default_branch": "main"},
        )

    def test_the_ssh_username_survives_into_the_durable_record(self) -> None:
        """O-B and O-K compound: before both fixes this record would have
        held ``ssh://github.com/…``, which is not clone-able — and after a
        restore it is the only copy left."""
        pdir = _scaffold(
            self.root, "demo", pid="00000001-0000-4000-8000-000000000001"
        )
        _git_repo(pdir, "ssh://git@github.com/owner/repo.git")

        self.assertEqual(
            self._refresh()["git"]["remote_url"],
            "ssh://git@github.com/owner/repo.git",
        )

    def test_the_write_is_key_scoped(self) -> None:
        """R-WRITE: the git refresher owns ``git`` and nothing else. Identity
        and metadata written by the other two writers must survive."""
        pdir = _scaffold(
            self.root,
            "demo",
            pid="00000001-0000-4000-8000-000000000001",
            display_name="Demo",
            description="a description",
        )
        _git_repo(pdir, "https://github.com/owner/repo.git")

        doc = self._refresh()
        self.assertEqual(doc["pid"], "00000001-0000-4000-8000-000000000001")
        self.assertEqual(doc["display_name"], "Demo")
        self.assertEqual(doc["description"], "a description")
        self.assertEqual(doc["owner_user_id"], "local")

    def test_a_restore_does_not_erase_the_remote_url(self) -> None:
        """The case the field exists for. ``tarball.py`` excludes ``.git`` and
        includes ``project.json``, so after a restore the folder is not a repo
        — and writing that answer through would erase the only surviving
        record of where it came from."""
        pdir = _scaffold(
            self.root, "demo", pid="00000001-0000-4000-8000-000000000001"
        )
        _git_repo(pdir, "https://github.com/owner/repo.git")
        before = self._refresh()["git"]

        import shutil

        shutil.rmtree(pdir / ".git")
        projects_json.reset_caches()
        after = self._refresh()

        self.assertEqual(after["git"], before)

    def test_an_unscaffolded_folder_is_never_given_a_project_json(self) -> None:
        """``refresh_git`` must not create the file: a document with a ``git``
        block and no identity is not a project, and writing one would mkdir a
        ghost — the hazard ``fill_identity`` guards at its own entry."""
        pdir = self.root / "demo"
        pdir.mkdir()
        _git_repo(pdir, "https://github.com/owner/repo.git")

        with patch.dict(os.environ, {"XO_GIT_PROVENANCE_REFRESH_S": "0"}):
            projects_json.apply()

        self.assertFalse((pdir / ".xo").exists())

    def test_a_non_repo_never_gets_a_null_block(self) -> None:
        """Absent beats a block of nulls: the schema says ``git`` is absent
        until the refresher has run, and a null block would claim it ran and
        found nothing."""
        _scaffold(self.root, "demo", pid="00000001-0000-4000-8000-000000000001")

        self.assertNotIn("git", self._refresh())

    def test_the_durable_block_validates_against_the_schema(self) -> None:
        """A block the schema rejects would be worse than no block at all —
        ``git`` is ``additionalProperties: false`` and has no ``is_repo``,
        which the registry's own block does carry."""
        from jsonschema import Draft7Validator

        schema_file = (
            Path(__file__).resolve().parents[1]
            / "services" / "cowork_agent" / "visualizer" / "schema"
            / "project.schema.json"
        )
        schema = json.loads(schema_file.read_text("utf-8"))

        pdir = _scaffold(
            self.root, "demo", pid="00000001-0000-4000-8000-000000000001"
        )
        _git_repo(pdir, "https://github.com/owner/repo.git")

        errors = list(Draft7Validator(schema).iter_errors(self._refresh()))
        self.assertEqual([e.message for e in errors], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
