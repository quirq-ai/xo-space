"""The part of agy the agy runtime codes against, checked against the
installed CLI, model-free (storm ch 04 §11; ch 06: the last known good
revision).

* every flag the runtime passes is in ``agy --help``;
* agy's own plugin validator (``agy plugin validate --json``) accepts XO's
  plugin as the installer writes it, with its hook and an MCP server.

agy updates itself unless told not to, so these run with
``AGY_CLI_DISABLE_AUTO_UPDATE=true``. With no agy installed they are
skipped. The wire's shapes are pinned by the fake (``tests/fakes/agy``),
written from agy 1.4.10's live output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.adapters.antigravity.runtime import argv as a, install
from services.cowork_agent.adapters.contract import SessionSpec

CLI_PATH_ENV = "AGY_CLI_PATH"
FLAGS = ("--input-format", "--output-format", "--disable-slash-commands", "--model", "--effort", "--mode", "--conversation", "--add-dir",
         "--json-schema", "--print")


def _binary() -> str | None:
    found = os.environ.get(CLI_PATH_ENV) or shutil.which("agy")
    local = Path.home() / ".local" / "bin" / "agy"
    if not found and local.is_file():
        found = str(local)
    if not found:
        return None
    probe = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=30, env=_env())
    return found if probe.returncode == 0 and probe.stdout.strip()[:1].isdigit() else None   # not a fake


def _env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "NO_COLOR": "1",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}


AGY = _binary()


@unittest.skipIf(AGY is None, "agy isn't installed")
class AgyProtocolTests(unittest.TestCase):
    def test_every_flag_the_runtime_passes_is_agys(self) -> None:
        out = subprocess.run([AGY, "--help"], capture_output=True, text=True, timeout=60, env=_env())
        text = out.stdout + out.stderr
        self.assertEqual([f for f in FLAGS if f not in text], [])
        self.assertIn("stream-json", text)
        for word in ("accept-edits", "auto", "plan"):
            self.assertIn(word, text)
        argv = a.argv("agy", SessionSpec(cwd="/w", add_dirs=("/d",)), mode="plan", resume="x", model="m",
                      effort="low", schema_file="/s")
        self.assertEqual({x for x in argv if x.startswith("--")} - {f"{f}=" if f == "--print" else f for f in FLAGS},
                         set())

    def test_agys_validator_accepts_xos_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install.ensure(["xo_comms"], root=Path(tmp), backups=Path(tmp) / "backups")
            out = subprocess.run([AGY, "plugin", "validate", str(install.plugin_dir(Path(tmp))), "--json"],
                                 capture_output=True, text=True, timeout=60, env=_env())
        report = json.loads(out.stdout[out.stdout.find("{"):])
        errors = [f for f in report.get("findings", []) if f.get("severity") == "error"]
        self.assertEqual(errors, [])
        self.assertEqual((report["components"]["hooks"], report["components"]["mcpServers"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
