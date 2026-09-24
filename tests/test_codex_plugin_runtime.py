"""Exercise the shipped plugin launcher without network, login or a live server.

The installer is real; only git, uv, the Codex executable and the final server
are replaced. The bundle is copied to an unrelated cache directory to catch
accidental dependencies on its repository or current working directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from utils.commands import run_sync


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
HAS_DOTENV = importlib.util.find_spec("dotenv") is not None


@unittest.skipUnless(BASH, "requires Bash")
class CodexPluginRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="space plugin ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bundle = self.root / "relocated cache" / "quirq"
        self.script = self.bundle / "scripts" / "space.sh"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "plugins/quirq/scripts/space.sh", self.script)
        self.workspace = self.root / "workspace with spaces"
        self.source = self.root / "source"
        self.source.mkdir()
        shutil.copyfile(ROOT / "install.sh", self.source / "install.sh")
        (self.source / "requirements.txt").write_text("")
        (self.source / "server.py").write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "Path(os.environ['TEST_SERVER_RECORD']).write_text(json.dumps({'cwd': os.getcwd(), 'env': dict(os.environ)}))\n"
            "print('server reached')\n"
        )
        (self.source / "utils" / "commands").mkdir(parents=True)
        for relative in ("utils/__init__.py", "utils/runtime_env.py", "utils/commands/__init__.py"):
            shutil.copyfile(ROOT / relative, self.source / relative)
        self.record = self.root / "server.json"
        self.calls = self.root / "calls.jsonl"
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "QUIRQ_SOURCE_REPO": "https://example.invalid/source.git",
            "QUIRQ_SOURCE_REF": "development",
            "TEST_SOURCE": str(self.source),
            "TEST_CALLS": str(self.calls),
            "TEST_SERVER_RECORD": str(self.record),
            "QUIRQ_COMMAND_LOG": "off",
        }
        # Also isolate the command runner used by this test process itself.
        logging = patch.dict(os.environ, {"QUIRQ_COMMAND_LOG": "off"})
        logging.start()
        self.addCleanup(logging.stop)
        self._executable(self.bin / "codex", "#!/bin/bash\nexit 0\n")
        self._python_executable(self.bin / "git", """
import json, os, shutil, sys
from pathlib import Path
with open(os.environ['TEST_CALLS'], 'a') as output:
    output.write(json.dumps(['git'] + sys.argv[1:]) + '\\n')
if os.environ.get('TEST_GIT_FAIL'):
    sys.exit(1)
assert sys.argv[1] == 'clone', 'launcher must never update an existing checkout'
shutil.copytree(os.environ['TEST_SOURCE'], sys.argv[-1])
""")
        shim = (
            "#!/bin/bash\n"
            'if [ "${QUIRQ_CHECK_HOST:-}" ]; then exit 0; fi\n'
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
        )
        self._python_executable(self.bin / "uv", f"""
import json, os, sys
from pathlib import Path
with open(os.environ['TEST_CALLS'], 'a') as output:
    output.write(json.dumps(['uv'] + sys.argv[1:]) + '\\n')
if sys.argv[1] == 'venv':
    python = Path(sys.argv[2]) / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.write_text({shim!r})
    python.chmod(0o755)
""")

    def _executable(self, path, content):
        path.write_text(content)
        path.chmod(0o755)

    def _python_executable(self, path, content):
        self._executable(path, f"#!{sys.executable}\n" + content)

    def invoke(self, action, path, **overrides):
        return run_sync(
            [BASH, str(self.script), action, str(path)],
            cwd=self.root,
            env={**self.env, **overrides},
            timeout=30,
        )

    def installed_repo(self, config="AGENT_NAME=codex\n"):
        repo = self.workspace / "xo-space"
        shutil.copytree(self.source, repo)
        python = repo / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        # A symlink outside a venv loses its pyvenv.cfg on Linux and falls back
        # to system site-packages. Invoke the actual test interpreter instead.
        self._executable(python, f'#!/bin/bash\nexec {shlex.quote(sys.executable)} "$@"\n')
        (repo / ".env").write_text(config)
        return repo

    def test_install_uses_durable_workspace_and_codex_defaults(self):
        result = self.invoke("install", self.workspace)
        self.assertTrue(result.ok, result.output)
        repo = self.workspace / "xo-space"
        snapshot = json.loads(self.record.read_text())
        self.assertEqual(snapshot["cwd"], str(repo))
        for key, expected in {
            "AGENT_NAME": "codex", "AI_PROVIDER": "codex", "HOST": "127.0.0.1",
            "QUIRQ_SKIP_BOOT_INSTALL": "1", "XO_PROJECTS_ROOT": str(self.workspace),
            "AI_WORKSPACE_ROOT": str(self.workspace), "QUIRQ_STATE_ROOT": str(self.workspace / ".quirq"),
        }.items():
            self.assertEqual(snapshot["env"][key], expected)
        self.assertIn("AGENT_NAME=codex\n", (repo / ".env").read_text())
        self.assertIn("server reached", (self.workspace / ".quirq/logs/quirq.log").read_text())
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(calls[0], ["git", "clone", "--quiet", "--depth", "1", "--branch", "development", "--", self.env["QUIRQ_SOURCE_REPO"], str(repo)])
        self.assertEqual([call[1] for call in calls[1:]], ["venv", "pip"])
        self.assertEqual(list(self.bundle.iterdir()), [self.bundle / "scripts"])

    def test_install_refuses_an_existing_target_without_fetch_or_overwrite(self):
        repo = self.workspace / "xo-space"
        repo.mkdir(parents=True)
        sentinel = repo / "local-work"
        sentinel.write_text("keep me")
        result = self.invoke("install", self.workspace)
        self.assertFalse(result.ok)
        self.assertIn("already exists", result.output)
        self.assertEqual(sentinel.read_text(), "keep me")
        self.assertFalse(self.calls.exists())

    def test_install_refuses_relative_root_and_plugin_cache_paths(self):
        for path in ("relative/workspace", "/", self.bundle / "workspace"):
            with self.subTest(path=path):
                result = self.invoke("install", path)
                self.assertFalse(result.ok, result.output)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.bundle / "workspace").exists())

    def test_install_refuses_a_symlink_into_plugin_cache(self):
        alias = self.root / "workspace alias"
        alias.symlink_to(self.bundle, target_is_directory=True)
        result = self.invoke("install", alias)
        self.assertFalse(result.ok)
        self.assertIn("outside the plugin cache", result.output)
        self.assertFalse(self.calls.exists())
        result = self.invoke("install", alias / "new workspace")
        self.assertFalse(result.ok)
        self.assertFalse((self.bundle / "new workspace").exists())

    def test_missing_and_broken_codex_fail_before_installing(self):
        for cli in (str(self.root / "missing codex"), str(self.bin / "broken codex")):
            if "broken" in cli:
                self._executable(Path(cli), "#!/bin/bash\nexit 9\n")
            with self.subTest(cli=cli):
                result = self.invoke("install", self.workspace, CODEX_CLI_PATH=cli)
                self.assertFalse(result.ok)
                self.assertIn("Codex CLI", result.output)
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.calls.exists())

    def test_explicit_codex_executable_works_when_path_wrapper_is_broken(self):
        self._executable(self.bin / "codex", "#!/bin/bash\nexit 127\n")
        cli = self.root / "bundled codex"
        self._executable(cli, "#!/bin/bash\nexit 0\n")
        result = self.invoke("install", self.workspace, CODEX_CLI_PATH=str(cli))
        self.assertTrue(result.ok, result.output)
        self.assertEqual(json.loads(self.record.read_text())["env"]["CODEX_CLI_PATH"], str(cli))

    def test_clone_failure_is_actionable_and_does_not_run_installer(self):
        result = self.invoke("install", self.workspace, TEST_GIT_FAIL="1")
        self.assertFalse(result.ok)
        self.assertIn("Could not clone XO Space", result.output)
        self.assertFalse(self.record.exists())

    def test_start_without_virtualenv_does_not_install_anything(self):
        repo = self.workspace / "xo-space"
        shutil.copytree(self.source, repo)
        result = self.invoke("start", repo)
        self.assertFalse(result.ok)
        self.assertIn("Python environment is missing", result.output)
        self.assertFalse(self.calls.exists())

    @unittest.skipUnless(HAS_DOTENV, "requires project python-dotenv dependency")
    def test_start_preserves_saved_roots_and_backend_without_install_or_update(self):
        old_state = self.root / "old state"
        state = self.root / "relocated state"
        (old_state / "settings").mkdir(parents=True)
        (old_state / "settings/roots.env").write_text(f"QUIRQ_STATE_ROOT={state}\n")
        (state / "settings").mkdir(parents=True)
        (state / "settings/runtime.env").write_text("AGENT_NAME=existing_backend\n")
        # Codex is broken, but the saved backend is different and must survive.
        self._executable(self.bin / "codex", "#!/bin/bash\nexit 127\n")
        config = f"AGENT_NAME=codex\nHOST=127.0.0.1\nPORT=6123\nQUIRQ_STATE_ROOT={old_state}\n"
        repo = self.installed_repo(config)
        result = self.invoke("start", repo)
        self.assertTrue(result.ok, result.output)
        self.assertEqual((repo / ".env").read_text(), config)
        snapshot = json.loads(self.record.read_text())
        self.assertNotIn("AGENT_NAME", snapshot["env"])
        self.assertEqual(snapshot["env"]["QUIRQ_SKIP_BOOT_INSTALL"], "1")
        self.assertEqual(snapshot["env"]["QUIRQ_SECRETS_FILE"], str(state / "secrets/secrets.env"))
        self.assertTrue((state / "logs/quirq.log").exists())
        self.assertFalse(self.calls.exists())

    @unittest.skipUnless(HAS_DOTENV, "requires project python-dotenv dependency")
    def test_codex_start_checks_cli_and_keeps_dotenv_as_data(self):
        sentinel = self.root / "must not exist"
        config = f"AGENT_NAME=codex\nQUIRQ_STATE_ROOT={self.root / 'state'}\nUNTRUSTED=$(touch '{sentinel}')\n"
        repo = self.installed_repo(config)
        result = self.invoke("start", repo)
        self.assertTrue(result.ok, result.output)
        self.assertFalse(sentinel.exists())
        self.assertFalse(self.calls.exists())
        env = json.loads(self.record.read_text())["env"]
        self.assertEqual(env["AI_PROVIDER"], "codex")
        self.assertEqual(env["CODEX_CLI_PATH"], str(self.bin / "codex"))
        self.record.unlink()
        result = self.invoke("start", repo, CODEX_CLI_PATH=str(self.root / "missing"))
        self.assertFalse(result.ok)
        self.assertIn("Codex CLI was not found", result.output)
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
