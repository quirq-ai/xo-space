"""XO's agy plugin (storm ch 04 §2–§3): its installer, and its hook and MCP
relay outside an XO session (they must do nothing there) and when XO doesn't
answer (the hook refuses).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.adapters.antigravity.runtime import install

class InstallTests(unittest.TestCase):
    def test_install_is_idempotent_keeps_names_and_backs_up_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, backups = Path(tmp, "config"), Path(tmp, "backups")
            first = install.ensure(["xo_comms"], root=root, backups=backups)
            where = install.plugin_dir(root)
            self.assertEqual(sorted(p.name for p in where.iterdir()), sorted(
                ["hooks.json", "mcp_config.json", "plugin.json", "xo_agy_hook.py", "xo_agy_mcp.py"]))
            self.assertEqual(install.ensure([], root=root, backups=backups), first)      # nothing new, nothing written
            self.assertFalse(backups.exists())
            install.ensure(["composio"], root=root, backups=backups)
            self.assertEqual(install.installed_names(where), {"xo_comms", "composio"})
            self.assertEqual(len(list(backups.iterdir())), 1)
            hooks = json.loads((where / "hooks.json").read_text())["xo"]
            self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["timeout"], install.HOOK_TIMEOUT_S)
            self.assertEqual(sorted(hooks), ["PreInvocation", "PreToolUse", "SessionStart", "Stop"])
            with self.assertRaises(install.InstallFailed):
                install.ensure(["bad name"], root=root, backups=backups)

    def test_the_hook_is_inert_outside_xo_and_fails_closed_inside(self) -> None:
        script = str(install.SOURCE / "xo_agy_hook.py")
        env = {k: v for k, v in os.environ.items() if k != "XO_AGY_SOCKET"}
        run = lambda event, env: json.loads(subprocess.run(  # noqa: E731
            [sys.executable, "-I", script, event], input='{"toolCall": {"name": "run_command"}}', text=True,
            capture_output=True, env=env, check=True).stdout)
        self.assertEqual(run("PreToolUse", env), {"decision": "allow"})      # the person's own agy: no opinion
        self.assertEqual(run("SessionStart", env), {})
        broken = {**env, "XO_AGY_SOCKET": "/nonexistent/agy.sock"}
        self.assertEqual(run("PreToolUse", broken)["decision"], "deny")
        self.assertEqual(run("PreInvocation", broken), {})

    def test_the_relay_lists_nothing_outside_xo(self) -> None:
        script = str(install.SOURCE / "xo_agy_mcp.py")
        env = {k: v for k, v in os.environ.items() if k != "XO_AGY_SOCKET"}
        lines = "\n".join(json.dumps(m) for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})) + "\n"
        out = subprocess.run([sys.executable, "-I", script, "xo_comms"], input=lines, text=True, capture_output=True,
                             env=env, check=True, timeout=10).stdout.splitlines()
        self.assertEqual([json.loads(line)["id"] for line in out], [1, 2])
        self.assertEqual(json.loads(out[1])["result"]["tools"], [])


if __name__ == "__main__":
    unittest.main()
