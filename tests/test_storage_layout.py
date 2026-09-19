"""The state root's folders and the move from where earlier releases kept files."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services.storage import layout
from tests.support import Sandbox, SandboxTestCase


class _Sandbox(SandboxTestCase):
    """An empty state root: the tests lay files out where earlier releases
    kept them and watch the migration move them."""

    copy_fixtures = False

    def setUp(self) -> None:
        super().setUp()
        self.root = self.sandbox.state

    def move(self, old: str, new: str) -> layout.Move:
        return layout.Move(old, lambda: self.root / old, lambda: self.root / new)


class FolderTests(_Sandbox):
    def test_every_folder_is_under_the_state_root(self) -> None:
        folders = {
            layout.inbox_dir(): "inbox", layout.sharing_dir(): "sharing",
            layout.usage_dir(): "usage", layout.settings_dir(): "settings",
            layout.secrets_dir(): "secrets", layout.cache_dir(): "cache",
            layout.logs_dir(): "logs", layout.locks_dir(): ".locks",
        }
        for path, name in folders.items():
            self.assertEqual(path, self.root / name)


    def test_logs_are_defined_once_below_the_services_layer(self) -> None:
        from modules.jobs import store as jobs_store
        from utils import runtime_env

        self.assertIs(layout.logs_dir, runtime_env.logs_dir)
        self.assertEqual(layout.jobs_dir(), self.root / "jobs")
        self.assertEqual(jobs_store.jobs_dir(), layout.jobs_dir())
        # A job's own output log sits with the other logs, not with its history.
        self.assertEqual(jobs_store.log_file("job1"), self.root / "logs" / "jobs" / "job1.log")


    def test_the_command_log_stays_at_its_old_path_until_moved(self) -> None:
        import utils.commands as commands

        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG": "", "QUIRQ_COMMAND_LOG_PATH": ""}):
            self.assertEqual(commands._default_command_log_path(), self.root / "logs" / "commands.log")
            (self.root / "commands.log").write_text("old\n", encoding="utf-8")
            self.assertEqual(commands._default_command_log_path(), self.root / "commands.log")
            layout.migrate_layout()
            self.assertEqual(commands._default_command_log_path(), self.root / "logs" / "commands.log")


class MigrateTests(_Sandbox):
    def test_a_file_moves_and_a_second_run_does_nothing(self) -> None:
        (self.root / "inbox.json").write_text("{}", encoding="utf-8")
        moves = [self.move("inbox.json", "inbox/inbox.json")]
        self.assertEqual(len(layout.migrate_layout(moves)), 1)
        self.assertFalse((self.root / "inbox.json").exists())
        self.assertEqual((self.root / "inbox" / "inbox.json").read_text(), "{}")
        self.assertEqual(layout.migrate_layout(moves), [])

    def test_when_both_exist_the_new_one_wins_and_the_old_one_stays(self) -> None:
        (self.root / "inbox.json").write_text("old", encoding="utf-8")
        (self.root / "inbox").mkdir()
        (self.root / "inbox" / "inbox.json").write_text("new", encoding="utf-8")
        with self.assertLogs("services.storage.layout", "WARNING"):
            self.assertEqual(layout.migrate_layout([self.move("inbox.json", "inbox/inbox.json")]), [])
        self.assertEqual((self.root / "inbox.json").read_text(), "old")
        self.assertEqual((self.root / "inbox" / "inbox.json").read_text(), "new")

    def test_folders_merge_child_by_child(self) -> None:
        old = self.root / "project_sharing"
        (old / "removed").mkdir(parents=True)
        (old / "a.json").write_text("a", encoding="utf-8")
        (old / "removed" / "r.json").write_text("r", encoding="utf-8")
        (old / "clash.json").write_text("old", encoding="utf-8")
        new = self.root / "sharing"
        new.mkdir()
        (new / "clash.json").write_text("new", encoding="utf-8")
        with self.assertLogs("services.storage.layout", "WARNING"):
            layout.migrate_layout([self.move("project_sharing", "sharing")])
        self.assertEqual((new / "a.json").read_text(), "a")
        self.assertEqual((new / "removed" / "r.json").read_text(), "r")
        self.assertEqual((new / "clash.json").read_text(), "new")
        self.assertEqual(sorted(p.name for p in old.iterdir()), ["clash.json"])

    def test_an_override_moves_nothing_and_a_failure_does_not_stop_the_rest(self) -> None:
        (self.root / "a.json").write_text("a", encoding="utf-8")

        def boom() -> Path:
            raise OSError("unreadable")

        moves = [
            layout.Move("overridden", lambda: None, lambda: self.root / "x"),
            layout.Move("broken", boom, lambda: self.root / "y"),
            self.move("a.json", "usage/a.json"),
        ]
        with self.assertLogs("services.storage.layout", "ERROR"):
            moved = layout.migrate_layout(moves)
        self.assertEqual(len(moved), 1)
        self.assertTrue((self.root / "usage" / "a.json").is_file())

    def test_the_real_moves_put_the_inbox_and_sharing_into_their_folders(self) -> None:
        (self.root / "inbox.json").write_text("{}", encoding="utf-8")
        (self.root / "project_sharing" / "removed").mkdir(parents=True)
        (self.root / "project_sharing" / "repo-1234abcd.json").write_text("{}", encoding="utf-8")
        layout.migrate_layout()
        self.assertTrue((self.root / "inbox" / "inbox.json").is_file())
        self.assertTrue((self.root / "sharing" / "repo-1234abcd.json").is_file())
        self.assertTrue((self.root / "sharing" / "removed").is_dir())
        self.assertFalse((self.root / "inbox.json").exists())
        self.assertFalse((self.root / "project_sharing").exists())

    def test_the_old_watcher_and_workspace_folders_are_taken_apart(self) -> None:
        watcher = self.root / "watcher"
        (watcher / "activity" / "projects").mkdir(parents=True)
        (watcher / "locks").mkdir()
        for name in ("offsets.json", "sample-offsets.json", "heartbeat.json"):
            (watcher / name).write_text("{}", encoding="utf-8")
        (watcher / "activity" / "workspace.json").write_text("{}", encoding="utf-8")
        (watcher / "locks" / "todos.json.abcd1234.lock").write_text("", encoding="utf-8")
        workspace = self.root / "workspace"
        (workspace / "sessions").mkdir(parents=True)
        for name in ("timeline.jsonl", "timeline.20260101T000000Z.jsonl", "graph.json", "stats.json"):
            (workspace / name).write_text("{}", encoding="utf-8")
        (workspace / "sessions" / "sessionslist.json").write_text("{}", encoding="utf-8")

        layout.migrate_layout()

        projects, cache = self.root / "projects", self.root / "cache"
        for path in (
            projects / "offsets.json", projects / "sample-offsets.json",
            projects / "timeline.jsonl", projects / "timeline.20260101T000000Z.jsonl",
            cache / "heartbeat.json", cache / "graph.json", cache / "stats.json",
            cache / "sessions" / "sessionslist.json",
        ):
            self.assertTrue(path.is_file(), path)
        for gone in (watcher, workspace, projects / "activity", projects / "locks"):
            self.assertFalse(gone.exists(), gone)
        self.assertEqual(layout.migrate_layout(), [])

    def test_rebuilt_data_is_removed_not_moved(self) -> None:
        (self.root / "watcher" / "activity").mkdir(parents=True)
        (self.root / "watcher" / "activity" / "workspace.json").write_text("{}", encoding="utf-8")
        moved = layout.migrate_layout([layout.Move("live presence", lambda: self.root / "watcher" / "activity", None)])
        self.assertEqual(len(moved), 1)
        self.assertFalse((self.root / "watcher" / "activity").exists())

    def test_settings_and_secrets_move_into_their_folders(self) -> None:
        for name in ("roots.env", "runtime.env", "state.json", "secrets.env"):
            (self.root / name).write_text("K=V\n", encoding="utf-8")
        env = {"QUIRQ_RUNTIME_FILE": "", "QUIRQ_SECRETS_FILE": str(self.root / "secrets" / "secrets.env")}
        with patch.dict(os.environ, env):
            layout.migrate_layout()
        for path in ("settings/roots.env", "settings/runtime.env", "settings/onboarding.json", "secrets/secrets.env"):
            self.assertTrue((self.root / path).is_file(), path)
        self.assertEqual((self.root / "secrets").stat().st_mode & 0o777, 0o700)

    def test_a_settings_file_an_override_points_elsewhere_stays_put(self) -> None:
        (self.root / "runtime.env").write_text("K=V\n", encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_RUNTIME_FILE": str(self.root / "runtime.env")}):
            layout.migrate_layout()
        self.assertTrue((self.root / "runtime.env").is_file())
        self.assertFalse((self.root / "settings" / "runtime.env").exists())

    def test_logs_move_into_the_logs_folder(self) -> None:
        (self.root / "commands.log").write_text("a", encoding="utf-8")
        (self.root / "commands.log.1").write_text("b", encoding="utf-8")
        # Two generations of saved command output, both older than logs/jobs/:
        # scheduler/logs/ (the oldest) and logs/scheduler/ (the release before).
        (self.root / "scheduler" / "logs").mkdir(parents=True)
        (self.root / "scheduler" / "logs" / "job1.log").write_text("c", encoding="utf-8")
        (self.root / "logs" / "scheduler").mkdir(parents=True)
        (self.root / "logs" / "scheduler" / "job2.log").write_text("d", encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": ""}):
            layout.migrate_layout()
        for path in ("logs/commands.log", "logs/commands.log.1", "logs/jobs/job1.log", "logs/jobs/job2.log"):
            self.assertTrue((self.root / path).is_file(), path)
        for gone in ("scheduler", "logs/scheduler"):
            self.assertFalse((self.root / gone).exists(), gone)

    def test_saved_commands_move_from_scheduler_to_jobs(self) -> None:
        old = self.root / "scheduler"
        (old / "runs").mkdir(parents=True)
        (old / "jobs.json").write_text("{}", encoding="utf-8")
        (old / "state.json").write_text("{}", encoding="utf-8")
        (old / "runs" / "job1.jsonl").write_text("", encoding="utf-8")
        layout.migrate_layout()
        for path in ("jobs/jobs.json", "jobs/state.json", "jobs/runs/job1.jsonl"):
            self.assertTrue((self.root / path).is_file(), path)
        self.assertFalse(old.exists())
        self.assertEqual(layout.migrate_layout(), [])

    def test_file_modes_survive_the_move(self) -> None:
        secret = self.root / "secrets.env"
        secret.write_text("K=V\n", encoding="utf-8")
        secret.chmod(0o600)
        layout.migrate_layout([self.move("secrets.env", "secrets/secrets.env")])
        self.assertEqual((self.root / "secrets" / "secrets.env").stat().st_mode & 0o777, 0o600)


class UsageWatermarkTests(unittest.TestCase):
    """The watermark used to live in the checkout; its store adopts it once."""

    def setUp(self) -> None:
        base = Sandbox.fresh(self).base
        self.new = base / "usage" / "stub.json"
        self.old = base / "data" / "stub" / "usage_sync_state.json"
        self.old.parent.mkdir(parents=True)
        self.old.write_text('{"last_synced_date": "2026-09-01"}', encoding="utf-8")

    def test_the_checkout_watermark_is_adopted_on_first_read(self) -> None:
        from services import usage_sync

        with patch.multiple(usage_sync, SYNC_STATE_FILE=str(self.new),
                            _DEFAULT_WATERMARK_PATH=str(self.new),
                            _LEGACY_WATERMARK_PATH=str(self.old)):
            self.assertEqual(usage_sync._load_sync_state()["last_synced_date"], "2026-09-01")
        self.assertFalse(self.old.exists())
        self.assertTrue(self.new.is_file())

    def test_an_explicit_override_leaves_the_old_file_alone(self) -> None:
        from services import usage_sync

        with patch.multiple(usage_sync, SYNC_STATE_FILE=str(self.new.with_name("override.json")),
                            _DEFAULT_WATERMARK_PATH=str(self.new),
                            _LEGACY_WATERMARK_PATH=str(self.old)):
            self.assertEqual(usage_sync._load_sync_state(), {})
        self.assertTrue(self.old.exists())

    def test_the_default_path_is_under_the_state_root_per_agent(self) -> None:
        from services import usage_sync

        self.assertEqual(Path(usage_sync._DEFAULT_WATERMARK_PATH).parent.name, "usage")


if __name__ == "__main__":
    unittest.main()
