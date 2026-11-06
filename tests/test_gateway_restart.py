"""Gateway restart: the call returns, and the gateway it starts survives the caller.

A restart from the UI (``POST /gateway/restart``) timed out after 30 s and the
runner's timeout cleanup killed the gateway it had just started. Two things
combined: the server's event loop (uvloop) leaves an extra inheritable copy of
the child's stdout/stderr at a high descriptor, which the gateway inherited, so
the output never reached end-of-file; and the gateway stayed in the restart
call's process group, which the timeout cleanup kills.
"""

from __future__ import annotations

import asyncio
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import server  # noqa: E402  (module scope: runs server's dotenv bootstrap once)

REPO = Path(__file__).resolve().parents[1]
AGENT_SH = REPO / "config" / "agents" / "openclaw" / "agent.sh"

# A stand-in for the CLI: `gateway run` stays alive until told to stop.
STUB_CLI = """#!/usr/bin/env bash
if [ "$1" = gateway ] && [ "${2:-}" = run ]; then
  trap 'exit 0' TERM
  while true; do sleep 1; done
fi
"""


class GatewayRestartRouteTests(unittest.TestCase):
    def test_restart_captures_output_in_files_not_pipes(self) -> None:
        agent = next(p.parent.name for p in sorted((REPO / "config" / "agents").glob("*/agent.sh")))
        result = mock.Mock(timed_out=False, binary_missing=False, exception=None,
                           returncode=0, output="ok", stderr="")
        run = mock.AsyncMock(return_value=result)
        with mock.patch("services.xo_manifest.resolve_agent_name", return_value=agent), \
                mock.patch.object(server, "run", run):
            body = asyncio.run(server.gateway_restart())

        self.assertEqual(body["status"], "restarted")
        argv = run.await_args.args[0]
        self.assertEqual(argv[1:], ["restart"])
        self.assertTrue(argv[0].endswith(f"{agent}/agent.sh"))
        self.assertTrue(run.await_args.kwargs.get("output_to_file"),
                        "a pipe stays open while the started gateway holds a copy")


def _real_gateway_present() -> bool:
    if Path("/tmp/openclaw-gateway.pid").exists():
        return True
    found = subprocess.run(["pgrep", "-f", "openclaw gateway run"], capture_output=True)
    return found.returncode == 0


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("setsid") and shutil.which("flock"),
                     "agent.sh is a Linux lifecycle script")
class GatewayLauncherDetachTests(unittest.TestCase):
    def setUp(self) -> None:
        # agent.sh keeps its PID/log/lock at fixed /tmp paths and stops stray
        # gateways: never run next to a real one.
        if _real_gateway_present():
            self.skipTest("a real gateway is running on this machine")
        self.tmp = Path(tempfile.mkdtemp(prefix="gw-launcher-"))
        script_dir = self.tmp / "config" / "agents" / "openclaw"
        script_dir.mkdir(parents=True)
        # LF copy, so a CRLF checkout still runs; its own repo root keeps the
        # real .env out of the test.
        self.agent = script_dir / "agent.sh"
        self.agent.write_bytes(AGENT_SH.read_bytes().replace(b"\r\n", b"\n"))
        self.agent.chmod(0o755)
        home_bin = self.tmp / "home" / ".local" / "bin"
        home_bin.mkdir(parents=True)
        cli = home_bin / "openclaw"
        cli.write_text(STUB_CLI)
        cli.chmod(0o755)
        # Bare PATH: the script must find the CLI in ~/.local/bin by itself.
        self.env = {"HOME": str(self.tmp / "home"), "PATH": "/usr/bin:/bin"}
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        subprocess.run([str(self.agent), "stop"], env=self.env, capture_output=True, timeout=30)
        subprocess.run(["pkill", "-f", str(self.tmp)], capture_output=True)
        for suffix in ("pid", "log", "log.1", "lock"):
            Path(f"/tmp/openclaw-gateway.{suffix}").unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_returns_and_the_gateway_holds_none_of_the_callers_descriptors(self) -> None:
        read_end, write_end = os.pipe()
        # The leak, as uvloop does it: a second inheritable copy of the
        # child's stdout at a high descriptor.
        leaked = os.dup2(write_end, 23)
        proc = subprocess.Popen([str(self.agent), "start"], env=self.env, stdin=subprocess.DEVNULL,
                                stdout=write_end, stderr=write_end, pass_fds=(leaked,))
        os.close(write_end)
        os.close(leaked)
        self.addCleanup(os.close, read_end)

        deadline = time.monotonic() + 15
        eof = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select([read_end], [], [], deadline - time.monotonic())
            if ready and not os.read(read_end, 65536):
                eof = True
                break
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertTrue(eof, "the started gateway kept the caller's output open")

        wrapper = int(Path("/tmp/openclaw-gateway.pid").read_text())
        os.kill(wrapper, 0)  # the gateway loop is running
        self.assertEqual(os.getpgid(wrapper), wrapper,
                         "the gateway loop needs its own process group, out of reach of the caller's cleanup")


if __name__ == "__main__":
    unittest.main()
