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
import signal
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


# A gateway with a plugin process of its own. On SIGTERM it closes the plugin
# itself with SIGKILL, so a SIGTERM the plugin records came from the launcher.
STUB_CLI_WITH_PLUGIN = r"""#!/usr/bin/env bash
if [ "$1" = gateway ] && [ "${2:-}" = run ]; then
  bash -c 'trap "echo sigterm >> @MARK@" TERM; while :; do sleep 5 & wait $!; done' &
  plugin=$!
  trap 'sleep 0.5; kill -KILL "$plugin"; exit 0' TERM
  while :; do sleep 5 & wait $!; done
fi
"""

# A gateway that takes @SECS@ s to shut down; it reports when its trap is in place.
STUB_CLI_SLOW_STOP = r"""#!/usr/bin/env bash
if [ "$1" = gateway ] && [ "${2:-}" = run ]; then
  trap 'sleep @SECS@; exit 0' TERM
  echo ready >> @MARK@
  while :; do sleep 5 & wait $!; done
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
        subprocess.run([str(self.agent), "stop", "5"], env=self.env, capture_output=True, timeout=30)
        subprocess.run(["pkill", "-f", str(self.tmp)], capture_output=True)
        for suffix in ("pid", "log", "log.1", "lock", "stop"):
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

    def _agent(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.agent), *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30, check=True)

    def _wait_for_lines(self, path: Path, count: int) -> None:
        deadline = time.monotonic() + 10
        while (path.read_text().count("\n") if path.exists() else 0) < count:
            self.assertLess(time.monotonic(), deadline, f"{path.name} never reached {count} line(s)")
            time.sleep(0.1)

    def _slow_stopping_gateway(self, seconds: int) -> Path:
        ready = self.tmp / "gateway-ready"
        cli = self.tmp / "home" / ".local" / "bin" / "openclaw"
        cli.write_text(STUB_CLI_SLOW_STOP.replace("@MARK@", str(ready)).replace("@SECS@", str(seconds)))
        return ready

    def test_stop_waits_as_long_as_the_caller_allows(self) -> None:
        ready = self._slow_stopping_gateway(3)
        log = Path("/tmp/openclaw-gateway.log")

        # 1 s is shorter than the gateway's 3 s shutdown: force-killed.
        self._agent("start")
        self._wait_for_lines(ready, 1)
        self._agent("stop", "1")
        self.assertIn("did not stop within 1s of SIGTERM", log.read_text())

        # The default (90 s) is enough: a clean stop, no second force-kill, and
        # it returns when the gateway exits, not after the full deadline.
        self._agent("start")
        self._wait_for_lines(ready, 2)
        started = time.monotonic()
        result = self._agent("stop")
        self.assertIn("up to 90s", result.stdout)
        self.assertLess(time.monotonic() - started, 20, "stop waited out its deadline instead of returning")
        self.assertEqual(log.read_text().count("force killed"), 1, "the default stop force-killed a gateway that needed 3 s")

    def test_restart_keeps_its_10_second_deadline(self) -> None:
        # /gateway/restart times out at 30 s: restart must not inherit stop's 90 s.
        ready = self._slow_stopping_gateway(12)
        self._agent("start")
        self._wait_for_lines(ready, 1)
        started = time.monotonic()
        self._agent("restart")
        self.assertLess(time.monotonic() - started, 25, "restart outlived the restart route's 30 s timeout")
        self.assertIn("did not stop within 10s of SIGTERM", Path("/tmp/openclaw-gateway.log").read_text())

    def test_a_bad_stop_timeout_falls_back_to_the_default(self) -> None:
        for bad in ("abc", "0", "00", "-5"):
            result = self._agent("stop", bad)
            self.assertIn(f"Ignoring stop timeout '{bad}'", result.stdout)
            self.assertIn("using 90", result.stdout)

    def test_every_lifecycle_request_names_its_caller_in_the_gateway_log(self) -> None:
        for action in ("start", "restart", "stop"):
            self._agent(action)

        log = Path("/tmp/openclaw-gateway.log").read_text()
        for action in ("start", "restart", "stop"):
            self.assertRegex(log, rf"agent\.sh {action} requested by: [^\[\n]+\[{os.getpid()}\]")

    def test_stop_leaves_the_gateways_own_processes_to_the_gateway(self) -> None:
        mark = self.tmp / "plugin-signals"
        cli = self.tmp / "home" / ".local" / "bin" / "openclaw"
        cli.write_text(STUB_CLI_WITH_PLUGIN.replace("@MARK@", str(mark)))

        self._agent("start")
        deadline = time.monotonic() + 10
        while subprocess.run(["pgrep", "-f", str(mark)], capture_output=True).returncode != 0:
            self.assertLess(time.monotonic(), deadline, "the stub gateway never started its plugin")
            time.sleep(0.1)

        self._agent("stop")
        self.assertFalse(mark.exists(),
                         "the launcher signalled the gateway's plugin itself, cutting its close short")

    def test_the_shutdown_line_tells_an_agent_sh_stop_from_an_outside_signal(self) -> None:
        pid_file = Path("/tmp/openclaw-gateway.pid")
        log_file = Path("/tmp/openclaw-gateway.log")
        self._agent("start")
        # `start` returns once the wrapper is launched, not once it is running;
        # a signal before bash has even begun the script kills it outright.
        deadline = time.monotonic() + 10
        while "Starting gateway" not in (log_file.read_text() if log_file.exists() else ""):
            self.assertLess(time.monotonic(), deadline, "the wrapper never started the gateway")
            time.sleep(0.1)
        # What `kill`, `pkill -f "openclaw gateway run"` or the openclaw CLI does.
        os.kill(int(pid_file.read_text()), signal.SIGTERM)
        deadline = time.monotonic() + 15
        while pid_file.exists():
            self.assertLess(time.monotonic(), deadline, "the wrapper did not exit on SIGTERM")
            time.sleep(0.1)
        self._agent("start")
        self._agent("stop")

        log = Path("/tmp/openclaw-gateway.log").read_text()
        self.assertIn("Shutting down (signal from outside agent.sh", log)
        self.assertIn("Shutting down (stop requested through agent.sh)", log)
        self.assertEqual(log.count("Shutting down"), 2, "the cleanup ran more than once per shutdown")


if __name__ == "__main__":
    unittest.main()
