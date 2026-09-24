"""Exercise the shipped discovery script without network or Python in its PATH."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.commands import run_sync

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugin" / "scripts" / "discover.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH and shutil.which("awk"), "requires bash and POSIX awk")
class PluginDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        command_log = patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": str(self.root / "commands.log")})
        command_log.start()
        self.addCleanup(command_log.stop)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.config = self.root / 'config with "quotes" and \\slashes'
        self.pointer = self.config / "quirq" / "install.json"
        self.pointer.parent.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("bash", "awk", "cat"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.responses = self.root / "responses"
        self.responses.mkdir()
        self.calls = self.root / "calls"
        curl = self.bin / "curl"
        curl.write_text(
            '#!/usr/bin/env bash\n'
            'url="${!#}"\n'
            'printf "%s\\n" "$url" >> "$DISCOVERY_CALLS"\n'
            'relative="${url#http://127.0.0.1:}"\n'
            'port="${relative%%/*}"\n'
            'path="${relative#*/}"\n'
            'response="$DISCOVERY_RESPONSES/$port-${path:-root}"\n'
            '[ -f "$response" ] || exit 7\n'
            'cat "$response"\n',
            encoding="utf-8",
        )
        curl.chmod(0o755)

    def checkout(self, name: str = "checkout") -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        for file in ("server.py", "requirements.txt", "cowork-api.sh"):
            (path / file).touch()
        return path

    def write_pointer(self, repo: Path, port: object = 54321) -> dict:
        value = {
            "repo_dir": str(repo),
            "projects_root": str(self.root / 'Projects "personal" \\ ü 🪐'),
            "state_root": str(self.root / "State\tdata\n"),
            "port": port,
        }
        self.pointer.write_text(json.dumps(value), encoding="utf-8")
        return value

    def response(self, port: int, *, health: object = None, identity: object = None) -> None:
        if health is None:
            health = {"status": "healthy", "auth": {"enabled": False}, "other": [1, None, True]}
        if identity is None:
            identity = {"status": "XO Space API running"}
        (self.responses / f"{port}-health").write_text(json.dumps(health), encoding="utf-8")
        (self.responses / f"{port}-root").write_text(json.dumps(identity), encoding="utf-8")

    def discover(self, ports: str = "54322", *, cwd: Path | None = None) -> dict:
        result = run_sync(
            [BASH, str(SCRIPT)],
            cwd=cwd or self.cwd,
            timeout=10,
            env={
                "PATH": str(self.bin),
                "HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.config),
                "QUIRQ_DISCOVER_PORTS": ports,
                "DISCOVERY_CALLS": str(self.calls),
                "DISCOVERY_RESPONSES": str(self.responses),
            },
            log_path=self.root / "commands.jsonl",
            separate_stderr=True,
        )
        self.assertTrue(result.ok, result.output + result.stderr)
        self.assertEqual(result.stderr, "")
        value = json.loads(result.output)
        self.assertEqual(set(value), {"state", "base_url", "repo_dir", "projects_root", "state_root", "pointer_file"})
        self.assertEqual(value["pointer_file"], str(self.pointer))
        return value

    def test_absent_install(self) -> None:
        self.assertEqual(self.discover()["state"], "not_installed")

    def test_escaped_and_unicode_paths_round_trip_without_python(self) -> None:
        repo = self.checkout('a "quoted" \\ café 🪐 checkout\n')
        pointer = self.write_pointer(repo)
        result = self.discover()
        self.assertEqual(result["state"], "installed")
        for field in ("repo_dir", "projects_root", "state_root"):
            self.assertEqual(result[field], pointer[field])

    def test_live_pointer_port_takes_precedence_over_override(self) -> None:
        repo = self.checkout()
        self.write_pointer(repo)
        self.response(54321)
        self.response(54322)
        result = self.discover()
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["base_url"], "http://127.0.0.1:54321")
        self.assertEqual(result["repo_dir"], str(repo))
        self.assertNotIn("54322", self.calls.read_text())

    def test_fallback_does_not_inherit_another_install_paths(self) -> None:
        self.write_pointer(self.checkout(), 54321)
        self.response(54322)
        result = self.discover()
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["base_url"], "http://127.0.0.1:54322")
        self.assertEqual([result[key] for key in ("repo_dir", "projects_root", "state_root")], ["", "", ""])

    def test_missing_pointer_checkout_does_not_report_stale_paths(self) -> None:
        self.write_pointer(self.root / "removed")
        self.assertEqual(self.discover()["state"], "not_installed")
        self.response(54321)
        result = self.discover()
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["repo_dir"], "")

    def test_generic_healthy_service_is_not_quirq(self) -> None:
        self.response(54322, identity={"status": "Another API running"})
        self.assertEqual(self.discover()["state"], "not_installed")

    def test_unhealthy_service_is_not_running(self) -> None:
        self.response(54322, health={"status": "unhealthy"})
        self.assertEqual(self.discover()["state"], "not_installed")
        self.assertEqual(self.calls.read_text().splitlines(), ["http://127.0.0.1:54322/health"])

    def test_malformed_pointer_is_ignored(self) -> None:
        pointer = self.write_pointer(self.checkout())
        for payload in (json.dumps(pointer)[:-1], json.dumps(pointer) + " garbage", '[{"port":54321}]', '{"repo_dir":"bad\\q"}'):
            with self.subTest(payload=payload):
                self.pointer.write_text(payload, encoding="utf-8")
                self.assertEqual(self.discover()["state"], "not_installed")

    def test_malformed_service_response_is_ignored(self) -> None:
        self.response(54322)
        (self.responses / "54322-health").write_text('{"status":"healthy"', encoding="utf-8")
        self.assertEqual(self.discover()["state"], "not_installed")

    def test_invalid_ports_are_not_probed(self) -> None:
        for port in (-1, 0, 65536, 5002.5, "54321", True):
            with self.subTest(port=port):
                self.write_pointer(self.root / "absent", port)
                self.assertEqual(self.discover("-1 0 65536 123abc 1:5002 0001 *")["state"], "not_installed")
        self.assertFalse(self.calls.exists())

    def test_duplicate_ports_are_probed_once(self) -> None:
        self.write_pointer(self.root / "absent", 54321)
        self.discover("54321 54321 54322")
        self.assertEqual(self.calls.read_text().splitlines(), ["http://127.0.0.1:54321/health", "http://127.0.0.1:54322/health"])

    def test_checkout_fallback_in_current_or_child_directory(self) -> None:
        for name in ("work", "work/xo-space"):
            with self.subTest(name=name):
                repo = self.checkout(name)
                result = self.discover()
                self.assertEqual(result["state"], "installed")
                self.assertEqual(result["repo_dir"], str(repo))
                for file in ("server.py", "requirements.txt", "cowork-api.sh"):
                    (repo / file).unlink()


if __name__ == "__main__":
    unittest.main()
