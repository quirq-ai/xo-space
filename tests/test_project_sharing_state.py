from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.sharing import state

REPO = "github.com/acme/trip-planner"


class CommitRelayStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / ".quirq"
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_files_land_under_quirq_relay(self) -> None:
        state.save_cursor(REPO, 7)
        files = list((self.root / "sharing").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), {"schema": 1, "cursor": 7})

    def test_fields_do_not_clobber_each_other(self) -> None:
        state.save_cursor(REPO, 5)
        state.save_last_reported(REPO, "abc123")
        state.save_cursor(REPO, 9)
        self.assertEqual(state.load_cursor(REPO), 9)
        self.assertEqual(state.load_last_reported(REPO), "abc123")

    def test_missing_state_reads_as_empty(self) -> None:
        self.assertEqual(state.load_cursor(REPO), 0)
        self.assertIsNone(state.load_last_reported(REPO))

    def test_corrupt_file_reads_as_empty(self) -> None:
        path = state.state_path(REPO)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(state.load_cursor(REPO), 0)
        self.assertIsNone(state.load_last_reported(REPO))
        state.save_cursor(REPO, 3)  # recovers by overwriting
        self.assertEqual(state.load_cursor(REPO), 3)

    def test_cloned_at_is_remembered_beside_the_bookmarks(self) -> None:
        self.assertIsNone(state.load_cloned_at(REPO))
        state.save_cursor(REPO, 4)
        state.save_cloned_at(REPO, "2026-09-07T10:00:00+00:00")
        self.assertEqual(state.load_cloned_at(REPO), "2026-09-07T10:00:00+00:00")
        self.assertEqual(state.load_cursor(REPO), 4)  # same file, no clobber

    def test_keyed_on_identity_not_folder(self) -> None:
        # A folder rename changes nothing the relay reads: the key is the origin.
        state.save_cursor(REPO, 11)
        self.assertEqual(state.load_cursor(REPO), 11)
        self.assertEqual(state.load_cursor("github.com/acme/other"), 0)

    def test_removal_marker_is_scoped_to_repository_and_projects_root(self) -> None:
        projects = Path(self._tmp.name) / "projects"
        elsewhere = Path(self._tmp.name) / "another-workspace"
        state.mark_removed(REPO, projects)
        self.assertTrue(state.is_removed(REPO, projects))
        self.assertFalse(state.is_removed(REPO, elsewhere))
        self.assertFalse(state.is_removed("github.com/acme/other", projects))
        self.assertTrue(state.removed_path(REPO, projects).is_relative_to(self.root))

    def test_relay_bookmarks_cannot_erase_removal_and_clear_is_exact(self) -> None:
        projects = Path(self._tmp.name) / "projects"
        other = "github.com/acme/other"
        state.mark_removed(REPO, projects)
        state.mark_removed(other, projects)
        state.save_cursor(REPO, 21)
        state.save_last_reported(REPO, "newsha")
        state.save_cloned_at(REPO, "2026-09-14T00:00:00Z")
        self.assertTrue(state.is_removed(REPO, projects))
        state.clear_removed(REPO, projects)
        self.assertFalse(state.is_removed(REPO, projects))
        self.assertTrue(state.is_removed(other, projects))
        self.assertEqual(state.load_cursor(REPO), 21)
        state.clear_removed(REPO, projects)  # idempotent

    def test_malformed_or_unreadable_removal_marker_still_suppresses(self) -> None:
        projects = Path(self._tmp.name) / "projects"
        marker = state.removed_path(REPO, projects)
        marker.parent.mkdir(parents=True)
        marker.write_text("{incomplete", encoding="utf-8")
        self.assertTrue(state.is_removed(REPO, projects))
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            self.assertTrue(state.is_removed(REPO, projects))

    def test_removal_marker_write_failure_is_not_reported_as_success(self) -> None:
        projects = Path(self._tmp.name) / "projects"
        with patch.object(state.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                state.mark_removed(REPO, projects)

    def test_existing_marker_is_never_followed_or_overwritten(self) -> None:
        projects = Path(self._tmp.name) / "projects"
        outside = Path(self._tmp.name) / "keep.txt"
        outside.write_text("keep", encoding="utf-8")
        marker = state.removed_path(REPO, projects)
        marker.parent.mkdir(parents=True)
        marker.symlink_to(outside)
        state.mark_removed(REPO, projects)
        self.assertTrue(state.is_removed(REPO, projects))
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        state.clear_removed(REPO, projects)
        self.assertTrue(outside.exists())

    def test_status_keeps_remote_membership_without_a_deleted_local_project(self) -> None:
        from modules.sharing import service, status

        projects = Path(self._tmp.name) / "projects"
        projects.mkdir()
        state.mark_removed(REPO, projects)
        status.reset()
        try:
            status.record_poll(ok=True, membership={REPO}, local={REPO: "trip-planner"},
                               members={REPO: 1})
            with patch.object(service, "xo_projects_root", return_value=projects):
                entry = service.status_snapshot()["repos"][REPO]
            self.assertTrue(entry["auto_clone_suppressed"])
            self.assertIsNone(entry["project"])
            self.assertTrue(entry["shared"])
            self.assertTrue(entry["available"])
            self.assertEqual(entry["members"], 1)
            # An explicit clone made outside the app is still a real local
            # project, even when the automatic-clone marker remains.
            (projects / "trip-planner").mkdir()
            with patch.object(service, "xo_projects_root", return_value=projects):
                entry = service.status_snapshot()["repos"][REPO]
            self.assertEqual(entry["project"], "trip-planner")
        finally:
            status.reset()
