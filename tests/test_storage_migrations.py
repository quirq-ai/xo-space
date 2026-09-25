"""Moving the state root's files from where earlier releases kept them."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.storage import migrations


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def move(self, old: str, new: str) -> migrations.Move:
        return migrations.Move(old, lambda: self.root / old, lambda: self.root / new)


class MigrateTests(_Sandbox):
    def test_a_file_moves_and_a_second_run_does_nothing(self) -> None:
        (self.root / "inbox.json").write_text("{}", encoding="utf-8")
        moves = [self.move("inbox.json", "inbox/inbox.json")]
        self.assertEqual(len(migrations.migrate_layout(moves)), 1)
        self.assertFalse((self.root / "inbox.json").exists())
        self.assertEqual((self.root / "inbox" / "inbox.json").read_text(), "{}")
        self.assertEqual(migrations.migrate_layout(moves), [])

    def test_when_both_exist_the_new_one_wins_and_the_old_one_stays(self) -> None:
        (self.root / "inbox.json").write_text("old", encoding="utf-8")
        (self.root / "inbox").mkdir()
        (self.root / "inbox" / "inbox.json").write_text("new", encoding="utf-8")
        with self.assertLogs("services.storage.migrations", "WARNING"):
            self.assertEqual(migrations.migrate_layout([self.move("inbox.json", "inbox/inbox.json")]), [])
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
        with self.assertLogs("services.storage.migrations", "WARNING"):
            migrations.migrate_layout([self.move("project_sharing", "sharing")])
        self.assertEqual((new / "a.json").read_text(), "a")
        self.assertEqual((new / "removed" / "r.json").read_text(), "r")
        self.assertEqual((new / "clash.json").read_text(), "new")
        self.assertEqual(sorted(p.name for p in old.iterdir()), ["clash.json"])

    def test_an_override_moves_nothing_and_a_failure_does_not_stop_the_rest(self) -> None:
        (self.root / "a.json").write_text("a", encoding="utf-8")

        def boom() -> Path:
            raise OSError("unreadable")

        moves = [
            migrations.Move("overridden", lambda: None, lambda: self.root / "x"),
            migrations.Move("broken", boom, lambda: self.root / "y"),
            self.move("a.json", "usage/a.json"),
        ]
        with self.assertLogs("services.storage.migrations", "ERROR"):
            moved = migrations.migrate_layout(moves)
        self.assertEqual(len(moved), 1)
        self.assertTrue((self.root / "usage" / "a.json").is_file())

    def test_the_real_moves_put_the_inbox_and_sharing_into_their_folders(self) -> None:
        (self.root / "inbox.json").write_text("{}", encoding="utf-8")
        (self.root / "project_sharing" / "removed").mkdir(parents=True)
        (self.root / "project_sharing" / "repo-1234abcd.json").write_text("{}", encoding="utf-8")
        migrations.migrate_layout()
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

        migrations.migrate_layout()

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
        self.assertEqual(migrations.migrate_layout(), [])

    def test_rebuilt_data_is_removed_not_moved(self) -> None:
        (self.root / "watcher" / "activity").mkdir(parents=True)
        (self.root / "watcher" / "activity" / "workspace.json").write_text("{}", encoding="utf-8")
        moved = migrations.migrate_layout([migrations.Move("live presence", lambda: self.root / "watcher" / "activity", None)])
        self.assertEqual(len(moved), 1)
        self.assertFalse((self.root / "watcher" / "activity").exists())

    def test_settings_and_secrets_move_into_their_folders(self) -> None:
        for name in ("roots.env", "runtime.env", "state.json", "secrets.env"):
            (self.root / name).write_text("K=V\n", encoding="utf-8")
        env = {"QUIRQ_RUNTIME_FILE": "", "QUIRQ_SECRETS_FILE": str(self.root / "secrets" / "secrets.env")}
        with patch.dict(os.environ, env):
            migrations.migrate_layout()
        for path in ("settings/roots.env", "settings/runtime.env", "settings/onboarding.json", "secrets/secrets.env"):
            self.assertTrue((self.root / path).is_file(), path)
        self.assertEqual((self.root / "secrets").stat().st_mode & 0o777, 0o700)

    def test_a_settings_file_an_override_points_elsewhere_stays_put(self) -> None:
        (self.root / "runtime.env").write_text("K=V\n", encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_RUNTIME_FILE": str(self.root / "runtime.env")}):
            migrations.migrate_layout()
        self.assertTrue((self.root / "runtime.env").is_file())
        self.assertFalse((self.root / "settings" / "runtime.env").exists())

    def test_logs_move_into_their_folders(self) -> None:
        (self.root / "commands.log").write_text("a", encoding="utf-8")
        (self.root / "scheduler" / "logs").mkdir(parents=True)
        (self.root / "scheduler" / "logs" / "job1.log").write_text("c", encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": ""}):
            migrations.migrate_layout()
        self.assertTrue((self.root / "logs" / "scheduler" / "job1.log").is_file())
        self.assertEqual(len(list((self.root / "inbox" / "activity" / "archive").iterdir())), 1)
        self.assertFalse((self.root / "scheduler" / "logs").exists())

    def test_file_modes_survive_the_move(self) -> None:
        secret = self.root / "secrets.env"
        secret.write_text("K=V\n", encoding="utf-8")
        secret.chmod(0o600)
        migrations.migrate_layout([self.move("secrets.env", "secrets/secrets.env")])
        self.assertEqual((self.root / "secrets" / "secrets.env").stat().st_mode & 0o777, 0o600)


class CommandLogTests(_Sandbox):
    def test_every_old_copy_goes_into_the_archive_in_the_order_it_was_written(self) -> None:
        """The very old top-level log and its .1, then the previous release's
        logs/ pair: each is finished history, so each becomes an archive file
        named for its last write, and the live log starts fresh."""
        copies = [("commands.log.1", "a"), ("commands.log", "b"),
                  ("logs/commands.log.1", "c"), ("logs/commands.log", "d")]
        for i, (name, text) in enumerate(copies):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            os.utime(path, (1767322445 + i * 3600,) * 2)   # an hour apart from 2026-01-02T02:54:05Z
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": ""}):
            migrations.migrate_layout()
        activity = self.root / "inbox" / "activity"
        archives = sorted((activity / "archive").iterdir())
        self.assertEqual([p.read_text(encoding="utf-8") for p in archives], ["a", "b", "c", "d"])
        self.assertEqual(archives[0].name, "commands.20260102T025405Z.log")
        self.assertFalse((activity / "commands.log").exists())
        for name, _ in copies:
            self.assertFalse((self.root / name).exists(), name)
        self.assertEqual(migrations.migrate_layout(), [])

    def test_history_left_in_logs_joins_a_log_already_started_in_inbox_activity(self) -> None:
        """Something wrote the new file before the boot migration ran (a
        launcher's own check): the history in logs/ is archived all the same,
        and archives + live log read back in the order written."""
        old = self.root / "logs" / "commands.log"
        old.parent.mkdir()
        old.write_text("=== history ===\n", encoding="utf-8")
        os.utime(old, (1767322445, 1767322445))   # 2026-01-02T02:54:05Z
        new = self.root / "inbox" / "activity" / "commands.log"
        new.parent.mkdir(parents=True)
        new.write_text("=== first entry after the update ===\n", encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": ""}):
            migrations.migrate_layout()
        archives = sorted((new.parent / "archive").iterdir())
        joined = "".join(p.read_text(encoding="utf-8") for p in [*archives, new])
        self.assertEqual(joined, "=== history ===\n=== first entry after the update ===\n")
        self.assertFalse(old.exists())

    def test_a_command_log_override_elsewhere_stays_put(self) -> None:
        (self.root / "logs").mkdir()
        kept = self.root / "logs" / "commands.log"
        kept.write_text("a", encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": str(kept)}):
            migrations.migrate_layout()
        self.assertTrue(kept.is_file())
        self.assertFalse((self.root / "inbox" / "activity" / "commands.log").exists())

    def test_the_last_rotated_command_log_becomes_the_first_archive(self) -> None:
        """The single `.1` generation earlier releases kept is adopted by the
        archive rather than deleted, named for when it was rotated."""
        rotated = self.root / "commands.log.1"
        rotated.write_text("b", encoding="utf-8")
        os.utime(rotated, (1767322445, 1767322445))     # 2026-01-02T02:54:05Z
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": ""}):
            migrations.migrate_layout()
        archived = self.root / "inbox" / "activity" / "archive" / "commands.20260102T025405Z.log"
        self.assertEqual(archived.read_text(encoding="utf-8"), "b")
        self.assertFalse((self.root / "logs" / "commands.log.1").exists())
        self.assertEqual(migrations.migrate_layout(), [])

    def test_the_command_log_stays_at_its_old_path_until_moved(self) -> None:
        import utils.commands as commands

        new = self.root / "inbox" / "activity" / "commands.log"
        for old in ("commands.log", "logs/commands.log"):
            with self.subTest(old=old), \
                 patch.dict(os.environ, {"QUIRQ_COMMAND_LOG": "", "QUIRQ_COMMAND_LOG_PATH": ""}):
                new.unlink(missing_ok=True)
                self.assertEqual(commands._default_command_log_path(), new)
                (self.root / old).parent.mkdir(parents=True, exist_ok=True)
                (self.root / old).write_text("old\n", encoding="utf-8")
                self.assertEqual(commands._default_command_log_path(), self.root / old)
                migrations.migrate_layout()
                self.assertEqual(commands._default_command_log_path(), new)
                self.assertFalse((self.root / old).exists())


if __name__ == "__main__":
    unittest.main()
