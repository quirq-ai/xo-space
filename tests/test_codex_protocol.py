"""The part of codex's app-server protocol the codex runtime codes against,
checked against the installed CLI's own schema (storm ch 03 §1, §11).

codex generates its protocol's JSON Schema itself (``codex app-server
generate-json-schema``, model-free, writes only into the folder it's given).
The runtime uses a small part of it: the methods below and the fields it
reads. A codex update that renames or drops one fails here, at build time,
before a session meets it (ch 06: the last known good revision). With no
codex installed the check is skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

#: The variable the runtime reads its binary from (``runtime/core.py``).
CLI_PATH_ENV = "CODEX_CLI_PATH"

#: Requests the runtime sends.
CLIENT_REQUESTS = ("initialize", "thread/start", "thread/resume", "thread/fork", "turn/start", "turn/steer",
                   "turn/interrupt", "model/list", "account/rateLimits/read")
CLIENT_NOTIFICATIONS = ("initialized",)
#: Requests the runtime answers (the rest get a JSON-RPC error).
SERVER_REQUESTS = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                   "item/permissions/requestApproval", "item/tool/requestUserInput", "mcpServer/elicitation/request",
                   "item/tool/call", "currentTime/read", "applyPatchApproval", "execCommandApproval")
#: Notifications the runtime reads (the rest are known noise or reported as unknown).
NOTIFICATIONS = ("turn/started", "turn/completed", "thread/status/changed", "item/started", "item/completed",
                 "item/agentMessage/delta", "item/plan/delta", "item/reasoning/summaryTextDelta",
                 "item/reasoning/summaryPartAdded", "item/commandExecution/outputDelta",
                 "item/fileChange/patchUpdated", "turn/plan/updated", "thread/tokenUsage/updated",
                 "account/rateLimits/updated", "error", "model/rerouted", "mcpServer/startupStatus/updated",
                 "serverRequest/resolved")
#: The parameter fields the runtime sends, per v2 type.
SENT_FIELDS = {
    "ThreadStartParams": ("cwd", "approvalPolicy", "sandbox", "approvalsReviewer", "ephemeral", "model",
                          "developerInstructions", "baseInstructions", "config"),
    "TurnStartParams": ("threadId", "input", "approvalPolicy", "effort", "model", "outputSchema", "additionalContext",
                        "collaborationMode"),
    "TurnSteerParams": ("threadId", "expectedTurnId", "input", "additionalContext"),
}
#: Thread items the runtime reads, and their fields it uses.
ITEMS = {
    "agentMessage": ("id", "text", "phase"), "plan": ("id", "text"), "reasoning": ("id", "summary"),
    "commandExecution": ("id", "command", "cwd", "status", "aggregatedOutput", "exitCode"),
    "fileChange": ("id", "changes", "status"), "mcpToolCall": ("id", "server", "tool", "arguments", "status", "error"),
    "webSearch": ("id", "query", "action"), "collabAgentToolCall": ("id", "tool", "prompt", "receiverThreadIds"),
    "subAgentActivity": ("id", "kind", "agentThreadId", "agentPath", "model"),
}
#: The answers' decisions the runtime sends (never ``acceptWithExecpolicyAmendment``).
DECISIONS = {"CommandExecutionApprovalDecision": ("accept", "decline", "cancel"),
             "FileChangeApprovalDecision": ("accept", "decline", "cancel")}


def _methods(schema: dict) -> set[str]:
    found = set()
    for variant in schema.get("oneOf", []):
        method = variant.get("properties", {}).get("method", {})
        found.update(method.get("enum") or ([method["const"]] if "const" in method else []))
    return found


def _enum_values(definition: dict) -> set[str]:
    found = set(definition.get("enum") or [])
    for variant in definition.get("oneOf", []):
        found.update(variant.get("enum") or [])
    return found


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        binary = os.environ.get(CLI_PATH_ENV) or shutil.which("codex")
        if not binary:
            raise unittest.SkipTest("no codex CLI installed: the protocol check runs where codex is")
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "schema"
        done = subprocess.run([binary, "app-server", "generate-json-schema", "--experimental", "--out", str(out)],
                              capture_output=True, text=True, timeout=60, cwd=cls.tmp.name)
        if done.returncode != 0:
            raise unittest.SkipTest(f"this codex can't generate its schema: {done.stderr[:200]}")
        cls.out = out
        cls.v2 = json.loads((out / "codex_app_server_protocol.v2.schemas.json").read_text())["definitions"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def load(self, name: str) -> dict:
        return json.loads((self.out / name).read_text())

    def test_every_method_the_runtime_uses_exists(self) -> None:
        for file, wanted in (("ClientRequest.json", CLIENT_REQUESTS), ("ClientNotification.json", CLIENT_NOTIFICATIONS),
                             ("ServerRequest.json", SERVER_REQUESTS), ("ServerNotification.json", NOTIFICATIONS)):
            with self.subTest(file):
                self.assertEqual(sorted(set(wanted) - _methods(self.load(file))), [])

    def test_every_field_the_runtime_sends_exists(self) -> None:
        for name, fields in SENT_FIELDS.items():
            with self.subTest(name):
                self.assertEqual(sorted(set(fields) - set(self.v2[name]["properties"])), [])

    def test_every_item_and_field_the_runtime_reads_exists(self) -> None:
        variants = {v["properties"]["type"]["enum"][0]: v for v in self.v2["ThreadItem"]["oneOf"]}
        for kind, fields in ITEMS.items():
            with self.subTest(kind):
                self.assertIn(kind, variants)
                self.assertEqual(sorted(set(fields) - set(variants[kind]["properties"])), [])

    def test_the_answers_the_runtime_gives_exist(self) -> None:
        for file, definition in (("CommandExecutionRequestApprovalResponse.json", "CommandExecutionApprovalDecision"),
                                 ("FileChangeRequestApprovalResponse.json", "FileChangeApprovalDecision")):
            with self.subTest(definition):
                values = _enum_values(self.load(file)["definitions"][definition])
                self.assertEqual(sorted(set(DECISIONS[definition]) - values), [])
        reviewers = set(self.v2["ApprovalsReviewer"]["enum"])
        self.assertIn("user", reviewers)
        self.assertIn("danger-full-access", self.v2["SandboxMode"]["enum"])


if __name__ == "__main__":
    unittest.main()
