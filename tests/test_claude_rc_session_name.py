"""Claude Remote Control session name: the default label and the prefix passed to the CLI.

POSIX only: remote_control imports fcntl. Run under WSL with the repo venv.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.claude_code import remote_control as rc

NAME_VARS = ("XO_WORKSPACE_NAME", "CODER_WORKSPACE_NAME")


def _env_without_name_vars(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in NAME_VARS}
    env.update(extra)
    return env


class DefaultNameTests(unittest.TestCase):
    def name_for(self, hostname: str, **env: str) -> str:
        with mock.patch.dict(os.environ, _env_without_name_vars(**env), clear=True), \
                mock.patch.object(rc.socket, "gethostname", return_value=hostname):
            return rc._default_name()

    def test_pod_hostname_becomes_the_workspace_name(self):
        self.assertEqual(self.name_for("coder-demo-5d8c7b9f4c-k2x7q"), "demo")

    def test_workspace_name_with_hyphens_is_kept_whole(self):
        self.assertEqual(self.name_for("coder-team-demo-ws-5d8c7b9f4c-k2x7q"), "team-demo-ws")

    def test_other_hostnames_are_used_as_is(self):
        for host in ("dev-laptop", "coder-laptop", "coder-demo-hello-world"):
            with self.subTest(host=host):
                self.assertEqual(self.name_for(host), host)

    def test_xo_workspace_name_wins(self):
        self.assertEqual(
            self.name_for("coder-demo-5d8c7b9f4c-k2x7q",
                          XO_WORKSPACE_NAME="Team box", CODER_WORKSPACE_NAME="demo"),
            "Team box",
        )

    def test_coder_workspace_name_beats_the_hostname(self):
        self.assertEqual(self.name_for("some-host", CODER_WORKSPACE_NAME="demo"), "demo")

    def test_blank_variables_are_ignored(self):
        self.assertEqual(
            self.name_for("coder-demo-5d8c7b9f4c-k2x7q",
                          XO_WORKSPACE_NAME="  ", CODER_WORKSPACE_NAME=""),
            "demo",
        )


class StartPassesNameTests(unittest.TestCase):
    """start() hands the label to the CLI as --name (via RC_NAME) and as the
    prefix for sessions the CLI names itself."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        files = {
            "PID_FILE": tmp / "pid", "URL_FILE": tmp / "url", "ERR_FILE": tmp / "err",
            "NAME_FILE": tmp / "name", "LOCK_FILE": tmp / "lock",
        }
        for attr, path in files.items():
            self._patch(mock.patch.object(rc, attr, path))
        self._patch(mock.patch.object(
            rc, "_STATE_FILES",
            (files["PID_FILE"], files["URL_FILE"], files["ERR_FILE"], files["NAME_FILE"])))
        self._patch(mock.patch.object(rc, "native_login_present", return_value=True))
        self._patch(mock.patch.object(rc, "ensure_gates_seeded"))
        self._patch(mock.patch.object(rc, "resolve_binary", return_value="/usr/bin/claude"))
        self._patch(mock.patch.object(rc, "_launch_dir", return_value=tmp))
        self._patch(mock.patch.object(rc, "_running_pid", return_value=None))
        self.popen = self._patch(mock.patch.object(rc.subprocess, "Popen"))
        self.popen.return_value.pid = 4242

    def _patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def launched_env(self) -> dict[str, str]:
        return self.popen.call_args.kwargs["env"]

    def test_default_label_is_used_for_name_and_prefix(self):
        with mock.patch.object(rc, "_default_name", return_value="demo"):
            result = rc.start()
        self.assertTrue(result["ok"])
        env = self.launched_env()
        self.assertEqual(env["RC_NAME"], "demo")
        self.assertEqual(env["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"], "demo")
        self.assertEqual(rc.NAME_FILE.read_text(), "demo")

    def test_given_name_is_used_for_name_and_prefix(self):
        rc.start(name="  Night shift  ")
        env = self.launched_env()
        self.assertEqual(env["RC_NAME"], "Night shift")
        self.assertEqual(env["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"], "Night shift")


if __name__ == "__main__":
    unittest.main()
