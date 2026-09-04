from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.project_sharing import config, git_ops


class GitRepoDirsTests(unittest.TestCase):
    def test_only_immediate_visible_dirs_with_a_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha" / ".git").mkdir(parents=True)
            (root / "beta").mkdir()                       # no .git
            (root / ".hidden" / ".git").mkdir(parents=True)  # hidden
            (root / "gamma" / ".git").mkdir(parents=True)
            (root / "delta" / "nested" / ".git").mkdir(parents=True)  # too deep
            (root / "file.txt").write_text("x", encoding="utf-8")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}):
                names = [p.name for p in project_layout.git_repo_dirs()]
        self.assertEqual(names, ["alpha", "gamma"])


class ConfigTests(unittest.TestCase):
    def test_parked_reasons_in_priority_order(self) -> None:
        with patch.dict(os.environ, {"PROJECT_SHARING_ENABLED": "false", "XO_PROJECT_ID": "ws-a"}):
            with patch.object(config, "auth_token", return_value="tok"):
                self.assertEqual(config.parked_reason(), "disabled")
        with patch.dict(os.environ, {"PROJECT_SHARING_ENABLED": "true", "XO_PROJECT_ID": ""}):
            with patch.object(config, "auth_token", return_value="tok"):
                self.assertEqual(config.parked_reason(), "no_workspace_id")
        with patch.dict(os.environ, {"PROJECT_SHARING_ENABLED": "true", "XO_PROJECT_ID": "ws-a"}):
            with patch.object(config, "auth_token", return_value=None):
                self.assertEqual(config.parked_reason(), "no_auth")
            with patch.object(config, "auth_token", return_value="tok"):
                self.assertIsNone(config.parked_reason())

    def test_interval_has_a_floor_and_a_default(self) -> None:
        with patch.dict(os.environ, {"PROJECT_SHARING_POLL_INTERVAL_SECONDS": ""}):
            self.assertEqual(config.poll_interval(), 60.0)
        with patch.dict(os.environ, {"PROJECT_SHARING_POLL_INTERVAL_SECONDS": "1"}):
            self.assertEqual(config.poll_interval(), 5.0)
        with patch.dict(os.environ, {"PROJECT_SHARING_POLL_INTERVAL_SECONDS": "junk"}):
            self.assertEqual(config.poll_interval(), 60.0)

    def test_jitter_stays_within_ratio(self) -> None:
        with patch.dict(os.environ, {"PROJECT_SHARING_POLL_INTERVAL_SECONDS": "60", "PROJECT_SHARING_POLL_JITTER_RATIO": "0.2"}):
            for _ in range(50):
                v = config.jittered_interval()
                self.assertGreaterEqual(v, 48.0)
                self.assertLessEqual(v, 72.0)


class LocalRemoteHeadTests(unittest.TestCase):
    def test_reads_loose_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ref = repo / ".git" / "refs" / "remotes" / "origin" / "main"
            ref.parent.mkdir(parents=True)
            ref.write_text("a" * 40 + "\n", encoding="utf-8")
            self.assertEqual(git_ops.local_remote_head(repo, "main"), "a" * 40)

    def test_reads_packed_ref_when_loose_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / ".git" / "packed-refs").write_text(
                "# pack-refs with: peeled fully-peeled sorted\n"
                + "b" * 40 + " refs/heads/main\n"
                + "c" * 40 + " refs/remotes/origin/main\n",
                encoding="utf-8",
            )
            self.assertEqual(git_ops.local_remote_head(repo, "main"), "c" * 40)

    def test_returns_none_when_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            self.assertIsNone(git_ops.local_remote_head(Path(tmp), "main"))


class StatusSnapshotTests(unittest.TestCase):
    def test_snapshot_carries_the_projects_root_for_clone_commands(self) -> None:
        from services.cowork_agent.project_sharing import service

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": tmp, "XO_PROJECT_ID": "ws-a"}):
                snap = service.status_snapshot()
        self.assertEqual(Path(snap["projects_root"]), Path(tmp).resolve())
        self.assertEqual(snap["own_workspace_id"], "ws-a")
