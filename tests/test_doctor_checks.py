"""One breakage on top of the healthy samples gives the expected finding."""

from __future__ import annotations

import json
import os
import shutil
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from services.doctor import checks, inventory
from services.timestamps import iso
from tests.doctor_sandbox import DoctorSandbox


class SpaceIdentityTests(DoctorSandbox):
    def write_space(self, xo_space_id, *, age_s: float = 3600, roots: dict | None = None) -> None:
        xo = self.projects / ".xo"
        xo.mkdir(exist_ok=True)
        updated = iso(datetime.fromtimestamp(self.now - age_s, timezone.utc))
        document = {"$schema": "xo/space.schema.json", "schema": 2, "xo_space_id": xo_space_id,
                    "updated_at": updated,
                    "roots": roots or {"projects_root": str(self.projects), "state_root": str(self.state)}}
        (xo / "space.json").write_text(json.dumps(document), encoding="utf-8")

    def identity(self) -> list[dict]:
        return [f for f in self.problems() if f["id"] == "space.identity"]

    def test_null_id_while_the_environment_has_one_fails(self) -> None:
        self.write_space(None)
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            [finding] = self.identity()
        self.assertEqual(finding["level"], "FAIL")

    def test_a_different_id_fails(self) -> None:
        self.write_space("space-2")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            [finding] = self.identity()
        self.assertEqual(finding["level"], "FAIL")
        self.assertIn("space-2", finding["observed"])

    def test_an_id_with_no_environment_value_warns(self) -> None:
        self.write_space("space-1")
        [finding] = self.identity()
        self.assertEqual(finding["level"], "WARN")

    def test_matching_id_is_healthy(self) -> None:
        self.write_space("space-1")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            self.assertEqual(self.problems(), [])

    def test_stale_roots_warn_but_not_right_after_a_write(self) -> None:
        other = {"projects_root": "/elsewhere/projects", "state_root": str(self.state)}
        self.write_space("space-1", roots=other, age_s=3600)
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            self.assertEqual([f["subject"] for f in self.identity()], ["space.json roots"])
            self.write_space("space-1", roots=other, age_s=10)
            self.assertEqual(self.identity(), [])

    def test_an_unreadable_space_json_is_left_to_the_read_check(self) -> None:
        (self.projects / ".xo").mkdir()
        (self.projects / ".xo" / "space.json").write_text("{", encoding="utf-8")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            self.assertEqual(self.ids(), {"read.invalid_json"})


class DuplicateIdTests(DoctorSandbox):
    def test_two_folders_with_one_pid_warn(self) -> None:
        shutil.copytree(self.projects / "sample-project", self.projects / "copy")
        [finding] = [f for f in self.problems() if f["id"] == "projects.duplicate_id"]
        self.assertEqual(finding["level"], "WARN")
        self.assertIn("copy", finding["observed"])
        self.assertIn("sample-project", finding["observed"])
        self.assertIn('delete the "pid" line', finding["why_it_matters"])


class StaleTempTests(DoctorSandbox):
    def temps(self, now=None) -> list[str]:
        return sorted(f["subject"] for f in self.problems(self.report(now)) if f["id"] == "tmp.stale")

    def test_old_temp_files_warn(self) -> None:
        (self.state / "inbox" / "inbox.json.tmp").write_text("{}", encoding="utf-8")
        (self.state / "projects" / "p").mkdir()
        (self.state / "projects" / "p" / "shard.json.tmp.123.abcdef12").write_text("{}", encoding="utf-8")
        (self.state / "settings" / ".state-abc12345.json").write_text("{}", encoding="utf-8")
        (self.projects / "sample-project" / ".xo" / "todos.json.tmp").write_text("{}", encoding="utf-8")
        self.assertEqual(self.temps(), [
            "inbox/inbox.json.tmp", "projects/p/shard.json.tmp.123.abcdef12",
            "sample-project/.xo/todos.json.tmp", "settings/.state-abc12345.json"])

    def test_young_temp_files_and_hidden_project_files_are_ignored(self) -> None:
        tmp = self.state / "inbox" / "inbox.json.tmp"
        tmp.write_text("{}", encoding="utf-8")
        (self.projects / "sample-project" / ".xo" / ".gitkeep").write_text("", encoding="utf-8")
        self.assertEqual(self.temps(now=tmp.stat().st_mtime + 60), [])
        tmp.unlink()
        self.assertEqual(self.temps(), [])

    def test_inert_hidden_files_are_not_interrupted_writes(self) -> None:
        old = self.now - 86400
        for name in sorted(checks.INERT_HIDDEN_NAMES):
            path = self.state / name
            path.write_bytes(b"\x00" * 8)
            os.utime(path, (old, old))
        self.assertNotIn("tmp.stale", self.ids())

    def test_an_mkstemp_name_is_still_reported(self) -> None:
        path = self.state / "settings" / ".state-ab12cd.json"
        path.write_text("{}", encoding="utf-8")
        old = self.now - 86400
        os.utime(path, (old, old))
        self.assertIn("tmp.stale", self.ids())


class LayoutTests(DoctorSandbox):
    def test_an_old_copy_beside_the_new_one_warns(self) -> None:
        (self.state / "inbox.json").write_text("{}", encoding="utf-8")
        [finding] = [f for f in self.problems() if f["id"] == "layout.old_copy_left"]
        self.assertIn("the Inbox", finding["observed"])

    def test_not_migrated_advice_depends_on_an_old_server_heartbeat(self) -> None:
        (self.state / "commands.log.1").write_text("old", encoding="utf-8")
        [finding] = [f for f in self.problems() if f["id"] == "layout.not_migrated"]
        self.assertIn("Restart the server once to move or clear these files.", finding["why_it_matters"])
        (self.state / "watcher").mkdir()
        beat = {"schema": 1, "last_tick_at": iso(datetime.fromtimestamp(self.now, timezone.utc))}
        (self.state / "watcher" / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
        [finding] = [f for f in self.problems() if f["id"] == "layout.not_migrated"]
        self.assertIn("older xo-space is still running", finding["why_it_matters"])

    def test_old_copy_left_warns_about_an_older_server_instead_of_delete_when_its_heartbeat_is_fresh(self) -> None:
        (self.state / "inbox.json").write_text("{}", encoding="utf-8")
        (self.state / "watcher").mkdir()
        beat = {"schema": 1, "last_tick_at": iso(datetime.fromtimestamp(self.now, timezone.utc))}
        (self.state / "watcher" / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
        [finding] = [f for f in self.problems()
                     if f["id"] == "layout.old_copy_left" and f["subject"] == "the Inbox"]
        self.assertIn("older xo-space", finding["why_it_matters"])
        self.assertNotIn("Delete it", finding["why_it_matters"])


class LegacyTests(DoctorSandbox):
    def test_pre_move_runtime_files_in_a_project_xo_warn(self) -> None:
        (self.projects / "sample-project" / ".xo" / "stats.json").write_text("{}", encoding="utf-8")
        [finding] = [f for f in self.problems() if f["id"] == "legacy.pending"]
        self.assertEqual(finding["subject"], "sample-project")
        self.assertIn("stats.json", finding["observed"])
        self.assertIn("Restart the server once to move them into the state folder.", finding["why_it_matters"])


class HeartbeatTests(DoctorSandbox):
    def beats(self) -> list[dict]:
        return [f for f in self.problems() if f["id"] == "watcher.heartbeat"]

    def test_disabled_watcher_is_not_checked(self) -> None:
        self.assertEqual(self.beats(), [])

    def test_a_stale_or_missing_heartbeat_warns_when_enabled(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "true"}):
            [stale] = self.beats()
            self.assertIn("last ticked", stale["observed"])
            (self.state / "cache" / "heartbeat.json").unlink()
            [missing] = self.beats()
            self.assertIn("no readable heartbeat yet", missing["observed"])

    def test_a_fresh_heartbeat_is_healthy(self) -> None:
        beat = {"schema": 1, "last_tick_at": iso(datetime.fromtimestamp(self.now - 1, timezone.utc))}
        (self.state / "cache" / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "true"}):
            self.assertEqual(self.beats(), [])


class NoActiveAgentResolutionTests(DoctorSandbox):
    """The watcher and layout checks must not resolve the active agent (that's
    Plane B config, unrelated to reading two watcher env vars), so a broken
    agent setup doesn't turn them into ERROR."""

    def test_heartbeat_and_layout_checks_survive_a_broken_active_agent(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "true"}), \
             patch("services.cowork_agent.runtime_config.get_active_agent",
                   side_effect=RuntimeError("broken agent setup")):
            report = self.report()
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertNotEqual(by_id["watcher"]["level"], "ERROR")
        self.assertNotEqual(by_id["layout"]["level"], "ERROR")


class GrowthTests(DoctorSandbox):
    def test_large_files_quarantine_locks_and_offsets_warn(self) -> None:
        (self.state / "projects" / "timeline.jsonl").write_bytes(b"x" * 64)
        (self.state / ".locks" / "extra.lock").write_text("", encoding="utf-8")
        offsets = self.state / "projects" / "offsets.json"
        document = json.loads(offsets.read_text(encoding="utf-8"))
        document["offsets"].update({f"/s/{i}.jsonl": {"offset": 0, "inode": i} for i in range(3)})
        offsets.write_text(json.dumps(document), encoding="utf-8")
        with patch.object(checks, "MAX_FILE_BYTES", 32), patch.object(checks, "MAX_ENTRIES", 1):
            found = {f["id"] for f in self.problems()}
        self.assertTrue({"growth.file_size", "growth.quarantine", "growth.locks", "growth.offsets"} <= found, found)


class PrivatePermissionTests(DoctorSandbox):
    def perms(self) -> list[dict]:
        return [f for f in self.problems() if f["id"] == "perms.too_open"]

    def test_the_samples_are_private(self) -> None:
        self.assertEqual(self.perms(), [])

    def test_a_world_readable_secret_warns(self) -> None:
        (self.state / "secrets" / "secrets.env").chmod(0o644)
        found = self.perms()
        self.assertEqual([f["subject"] for f in found], ["secrets/secrets.env"])
        self.assertNotIn("=", found[0]["observed"], "a finding never carries what is inside")

    def test_a_world_readable_env_warns(self) -> None:
        (self.state / "settings" / "runtime.env").chmod(0o666)
        self.assertEqual([f["subject"] for f in self.perms()], ["settings/runtime.env"])

    def test_a_group_readable_file_warns(self) -> None:
        (self.state / "secrets" / "token.json").chmod(0o640)
        self.assertEqual([f["subject"] for f in self.perms()], ["secrets/token.json"])

    def test_a_parsed_state_file_is_not_judged_on_permissions(self) -> None:
        (self.state / "settings" / "onboarding.json").chmod(0o644)
        self.assertEqual(self.perms(), [])

    def test_the_private_patterns_are_in_the_inventory(self) -> None:
        patterns = {spec.pattern for spec in inventory.SPECS if spec.klass == inventory.UNPARSED}
        self.assertTrue(set(checks.PRIVATE_PATTERNS) <= patterns)


if __name__ == "__main__":
    unittest.main()
