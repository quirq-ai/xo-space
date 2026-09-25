"""Real Node + filesystem parity and containment tests for the Fly host."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from services.cowork_agent.adapters.fly import runtime, tools

FIXTURES = Path(__file__).with_name("fixtures") / "fly"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for installed-core parity")
class FlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.state = Path(self.temporary.name) / "state"
        self.environment = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.state), "QUIRQ_COMMAND_LOG": "on"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.artifact = json.loads((FIXTURES / "failure-scout.fly.json").read_text())

    def populate(self):
        workspace = json.loads((FIXTURES / "workspace.json").read_text())
        for file in workspace["files"]:
            path = self.root / file["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file["content"], encoding="utf-8")
        return workspace

    async def test_exact_browser_golden_and_only_chosen_files_are_read(self):
        self.populate()
        golden = json.loads((FIXTURES / "browser-golden.json").read_text())
        before = json.dumps(self.artifact, sort_keys=True)
        opened = []
        original = tools.WorkspaceReader.read

        def record(reader, path):
            opened.append(path)
            return original(reader, path)

        with patch.object(tools.WorkspaceReader, "read", record):
            result = await runtime.execute(self.artifact, self.root)
        self.assertEqual(result["trace"], golden["trace"])
        self.assertEqual(opened, [item["path"] for item in golden["trace"]])
        report = result["report"]
        for field in ("status", "reason", "records", "errors", "fields", "missingFields", "note"):
            self.assertEqual(report[field], golden["report"][field], field)
        for field in golden["report"]["scope"]:
            self.assertEqual(report["scope"][field], golden["report"]["scope"][field], field)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(opened), 4)
        self.assertEqual(result["provenance"]["artifactDigest"], golden["artifactDigest"])
        self.assertFalse(result["provenance"]["weightsUpdated"])
        self.assertEqual(json.dumps(self.artifact, sort_keys=True), before)

    async def test_utf8_size_feature_citations_and_no_command_log_content(self):
        content = '{"run":"秘密🪰-do-not-log","success":false,"duration":12}'
        (self.root / "telemetry.json").write_text(content, encoding="utf-8")
        result = await runtime.execute(self.artifact, self.root, {"fields": ["run", "success", "duration"], "query": "metrics", "maxReads": 1})
        import math
        self.assertEqual(result["trace"][0]["features"][13], math.log1p(len(content.encode("utf-8"))) / math.log1p(256000))
        source = result["report"]["records"][0]["source"]
        self.assertEqual(source["contentHash"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(source["path"], "telemetry.json")
        self.assertEqual(result["report"]["status"], "complete")
        logs = list(self.state.rglob("commands.log"))
        self.assertTrue(logs)
        for log in logs:
            text = log.read_text()
            self.assertIn("Fly bridge completed", text)
            self.assertNotIn("do-not-log", text)
            self.assertNotIn("秘密", text)

    async def test_hidden_secret_symlink_and_hardlink_paths_are_excluded(self):
        (self.root / "visible.json").write_text('{"run":1}')
        (self.root / ".quirq").mkdir()
        (self.root / ".quirq" / "hidden.json").write_text('{"run":"secret"}')
        (self.root / "credentials.json").write_text('{"run":"secret"}')
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text('{"run":"outside"}')
        (self.root / "linked.json").symlink_to(outside)
        os.link(outside, self.root / "hard.json")
        (self.root / "escape").symlink_to(outside.parent, target_is_directory=True)
        result = await runtime.execute(self.artifact, self.root, {"fields": ["run"], "maxReads": 64})
        self.assertEqual(result["report"]["scope"]["inspectedPaths"], ["visible.json"])
        self.assertEqual(result["report"]["records"][0]["values"], {"run": 1})
        self.assertEqual(result["report"]["scope"]["excludedEntries"], 5)

    async def test_invalid_utf8_and_malformed_csv_are_visible_read_errors(self):
        (self.root / "a.json").write_bytes(b'\xff\xfe')
        (self.root / "b.csv").write_text('run\n"unclosed')
        result = await runtime.execute(self.artifact, self.root, {"fields": ["run"], "maxReads": 2})
        self.assertEqual(result["report"]["status"], "partial")
        self.assertEqual(len(result["report"]["errors"]), 2)
        self.assertTrue(any("UTF-8" in item["message"] for item in result["report"]["errors"]))
        self.assertTrue(any("Unclosed CSV" in item["message"] for item in result["report"]["errors"]))

    async def test_deleted_empty_and_nonempty_csv_preserve_host_read_errors(self):
        (self.root / "empty.csv").write_text("")
        (self.root / "filled.csv").write_text("run,success\none,false\n")
        original = tools.WorkspaceReader.read

        def delete_before_read(reader, path):
            (self.root / path).unlink()
            return original(reader, path)

        with patch.object(tools.WorkspaceReader, "read", delete_before_read):
            result = await runtime.execute(self.artifact, self.root, {"fields": ["run", "success"], "maxReads": 2})
        report = result["report"]
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["records"], [])
        self.assertEqual({item["path"] for item in report["errors"]}, {"empty.csv", "filled.csv"})
        for item in report["errors"]:
            self.assertIn("could not be read safely", item["message"])
            self.assertNotIn("CSV", item["message"])
        self.assertTrue(all("could not be read safely" in item["error"] for item in result["trace"]))

    async def test_transport_request_cap_rejects_before_running_a_command(self):
        with patch.object(tools, "MAX_MESSAGE_BYTES", 100), patch.object(tools, "run") as command:
            with self.assertRaisesRegex(ValueError, "24 MB"):
                await tools.node_call({"op": "validate", "artifact": self.artifact})
        command.assert_not_called()

    async def test_changed_file_and_replaced_ancestor_cannot_escape(self):
        directory = self.root / "results"
        directory.mkdir()
        (directory / "run.json").write_text('{"run":"original"}')
        reader = tools.WorkspaceReader(self.root)
        self.addCleanup(reader.close)
        reader.discover()
        (directory / "run.json").write_text('{"run":"changed"}')
        self.assertIn("changed", reader.read("results/run.json")["error"])
        directory.rename(self.root / "moved")
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "run.json").write_text('{"run":"outside"}')
        directory.symlink_to(outside, target_is_directory=True)
        read = reader.read("results/run.json")
        self.assertIn("error", read)
        self.assertNotIn("content", read)

    async def test_pinned_root_replaced_during_validation_is_never_discovered(self):
        (self.root / "original.json").write_text('{"run":"original"}')
        identity = [self.root.stat().st_dev, self.root.stat().st_ino]
        validated, continue_run = asyncio.Event(), asyncio.Event()
        real_node_call = runtime.node_call

        async def pause_after_validation(payload):
            result = await real_node_call(payload)
            if payload["op"] == "validate":
                validated.set()
                await continue_run.wait()
            return result

        with patch.object(runtime, "node_call", pause_after_validation), patch.object(tools.WorkspaceReader, "discover") as discover, patch.object(tools.WorkspaceReader, "read") as read:
            pending = asyncio.create_task(runtime.execute(self.artifact, self.root, workspace_identity=identity))
            await validated.wait()
            self.root.rename(self.root.with_name("original-root"))
            self.root.mkdir()
            (self.root / "new.json").write_text('{"run":"unapproved-replacement"}')
            continue_run.set()
            with self.assertRaisesRegex(ValueError, "pinned workspace directory changed"):
                await pending
        discover.assert_not_called()
        read.assert_not_called()

    async def test_matching_pinned_root_identity_can_execute(self):
        (self.root / "original.json").write_text('{"run":"original"}')
        identity = [self.root.stat().st_dev, self.root.stat().st_ino]
        result = await runtime.execute(self.artifact, self.root, {"fields": ["run"], "maxReads": 1}, workspace_identity=identity)
        self.assertEqual(result["report"]["records"][0]["values"], {"run": "original"})

    async def test_file_and_scan_limits_fail_instead_of_claiming_complete(self):
        for index in range(65):
            (self.root / f"{index}.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "64 JSON/CSV"):
            await runtime.execute(self.artifact, self.root)
        with patch.object(tools, "MAX_SCAN_ENTRIES", 1):
            with self.assertRaisesRegex(ValueError, "bounded scan"):
                await runtime.execute(self.artifact, self.root)

    async def test_oversized_file_and_invalid_task_are_rejected(self):
        (self.root / "large.json").write_bytes(b'x' * 256001)
        with self.assertRaisesRegex(ValueError, "256 KB"):
            await runtime.execute(self.artifact, self.root)
        with self.assertRaisesRegex(ValueError, "1 to 64"):
            await runtime.execute(self.artifact, self.root, {"maxReads": 65})
        with self.assertRaisesRegex(ValueError, "Unknown task"):
            await runtime.execute(self.artifact, self.root, {"command": "rm -rf /"})

    async def test_artifact_tampering_and_preload_options_are_rejected(self):
        changed = json.loads(json.dumps(self.artifact))
        changed["policy"]["weights"][0] += 1
        with self.assertRaisesRegex(ValueError, "Provenance|digest"):
            await tools.node_call({"op": "validate", "artifact": changed})
        with patch.dict(os.environ, {"NODE_OPTIONS": "--require /does/not/exist.js"}):
            validation = await tools.node_call({"op": "validate", "artifact": self.artifact})
        self.assertEqual(validation["artifact"], self.artifact)
        manifest = json.loads((tools.NODE_DIR / "provenance.json").read_text())
        self.assertEqual(validation["coreSha256"], manifest["sourceSha256"])
        self.assertEqual(validation["coreSha256"], hashlib.sha256((tools.NODE_DIR / "core.mjs").read_bytes()).hexdigest())

    async def test_cancellation_before_start_never_launches_node(self):
        cancelled = asyncio.Event()
        cancelled.set()
        with patch.object(runtime, "node_call") as node:
            with self.assertRaises(asyncio.CancelledError):
                await runtime.execute(self.artifact, self.root, cancel_event=cancelled)
        node.assert_not_called()

    async def test_cancelled_transport_waits_for_bounded_child_before_cleanup(self):
        started, finish = asyncio.Event(), asyncio.Event()
        real_run = tools.run

        async def delayed_run(*args, **kwargs):
            started.set()
            await finish.wait()
            return await real_run(*args, **kwargs)

        with patch.object(tools, "run", delayed_run):
            pending = asyncio.create_task(tools.node_call({"op": "validate", "artifact": self.artifact}))
            await started.wait()
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending

    async def test_real_node_timeout_is_bounded_and_following_run_recovers(self):
        with patch.object(tools, "NODE_TIMEOUT", 0.001):
            with self.assertRaisesRegex(RuntimeError, "execution limit"):
                await tools.node_call({"op": "validate", "artifact": self.artifact})
        result = await tools.node_call({"op": "validate", "artifact": self.artifact})
        self.assertEqual(result["artifact"], self.artifact)


if __name__ == "__main__":
    unittest.main()
