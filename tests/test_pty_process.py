"""utils.commands.spawn_pty / PtyProcess on a real pseudo-terminal.

Every test runs twice: under plain asyncio and under uvloop, which is what
uvicorn runs in production. The two differ exactly where terminals are
fragile (uvloop raises in ``preexec_fn`` and its pipe transports break on a
tty), and FastAPI's TestClient only ever uses asyncio.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform.startswith("win") or not hasattr(os, "login_tty"):  # pragma: no cover - platform gate
    raise unittest.SkipTest("terminals need POSIX and Python 3.11+")

from utils import commands
from utils.commands import spawn_pty

try:
    import uvloop
except ImportError:  # pragma: no cover - optional
    uvloop = None

BASH = shutil.which("bash") or "/bin/bash"
DASH = shutil.which("dash")


class _Harness:
    """Collects a terminal's output and exit, with helpers to wait on them."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.code: int | None = None
        self.exited = asyncio.Event()

    def on_output(self, data: bytes) -> None:
        self.out += data

    def on_exit(self, code: int) -> None:
        self.code = code
        self.exited.set()

    async def wait_for(self, needle: bytes, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while needle not in self.out:
            if time.monotonic() > deadline:
                raise AssertionError(f"{needle!r} never appeared in {bytes(self.out[-400:])!r}")
            await asyncio.sleep(0.02)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for our purposes.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().split(b") ", 1)[1][:1] != b"Z"
    except OSError:
        return True


class PtyProcessTests(unittest.TestCase):
    loop_name = "asyncio"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.log = self.home / "commands.log"
        self._env = patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": str(self.log)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def run_async(self, coro):
        if self.loop_name == "uvloop":
            return uvloop.run(coro)
        return asyncio.run(coro)

    def env(self, **extra: str) -> dict:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
                "TERM": "xterm-256color", "PS1": "$ ", **extra}

    async def spawn(self, argv=None, **kwargs):
        h = _Harness()
        proc = await spawn_pty(argv or [BASH, "--norc", "--noprofile", "-i"], cwd=self.home, env=self.env(),
                               on_output=h.on_output, on_exit=h.on_exit, **kwargs)
        return proc, h

    def test_shell_has_a_controlling_terminal_and_the_given_folder(self) -> None:
        async def go():
            proc, h = await self.spawn(rows=33, cols=101)
            proc.write(b"tty; stty size; pwd; : </dev/tty && echo devtty-$((1+1))\n")
            await h.wait_for(b"devtty-2")
            text = h.out.decode(errors="replace")
            self.assertIn("/dev/pts/", text) if sys.platform.startswith("linux") else None
            self.assertIn("33 101", text)
            self.assertIn(str(self.home), text)
            self.assertEqual(proc.foreground(), "bash") if os.path.isdir("/proc") else None
            await proc.terminate()
        self.run_async(go())

    def test_resize_and_redraw_signal_the_foreground_job(self) -> None:
        async def go():
            proc, h = await self.spawn()
            proc.write(b"trap 'echo winch-$((2*21))' WINCH\n")
            await asyncio.sleep(0.2)
            proc.resize(50, 132)
            proc.write(b"stty size\n")
            await h.wait_for(b"50 132")
            await h.wait_for(b"winch-42")
            h.out.clear()
            proc.redraw()  # same size: only the explicit SIGWINCH repaints
            await h.wait_for(b"winch-42")
            await proc.terminate()
        self.run_async(go())

    def test_ctrl_c_interrupts_the_foreground_job_and_exit_code_is_reported(self) -> None:
        async def go():
            proc, h = await self.spawn()
            proc.write(b"sleep 30; echo after-$((3*3))\n")
            await asyncio.sleep(0.4)
            proc.write(b"\x03")
            proc.write(b"echo alive-$((4*4))\n")
            await h.wait_for(b"alive-16")
            self.assertNotIn(b"after-9", h.out)
            proc.write(b"exit 7\n")
            await asyncio.wait_for(h.exited.wait(), 5)
            self.assertEqual(h.code, 7)
            self.assertEqual(proc.returncode, 7)
            self.assertFalse(proc.write(b"x"), "input after exit is refused")
        self.run_async(go())

    def _assert_terminate_takes_the_whole_session(self, argv) -> None:
        async def go():
            proc, h = await self.spawn(argv)
            marker = self.home / "pids"
            proc.write(f"sleep 301 & echo $! > {marker}.bg; nohup sleep 302 > /dev/null 2>&1 & echo $! > {marker}.nohup\n".encode())
            deadline = time.monotonic() + 5
            while not (Path(f"{marker}.bg").exists() and Path(f"{marker}.nohup").exists()
                       and Path(f"{marker}.nohup").read_text().strip()):
                if time.monotonic() > deadline:
                    raise AssertionError(f"jobs never started: {bytes(h.out)!r}")
                await asyncio.sleep(0.05)
            pids = [int(Path(f"{marker}.bg").read_text()), int(Path(f"{marker}.nohup").read_text())]
            await proc.terminate(grace=1.0)
            await asyncio.sleep(0.2)
            self.assertEqual([p for p in pids if _alive(p)], [], "background and nohup jobs must go with the terminal")
            self.assertIsNotNone(proc.returncode)
        self.run_async(go())

    def test_terminate_takes_background_and_nohup_jobs_under_bash(self) -> None:
        self._assert_terminate_takes_the_whole_session([BASH, "--norc", "--noprofile", "-i"])

    @unittest.skipUnless(DASH, "dash not installed")
    def test_terminate_takes_background_and_nohup_jobs_under_dash(self) -> None:
        self._assert_terminate_takes_the_whole_session([DASH, "-i"])

    def test_jobs_left_behind_when_the_shell_exits_are_hung_up(self) -> None:
        async def go():
            proc, h = await self.spawn()
            marker = self.home / "bg"
            proc.write(f"sleep 303 & echo $! > {marker}; exit 0\n".encode())
            await asyncio.wait_for(h.exited.wait(), 5)
            await asyncio.sleep(0.2)
            self.assertFalse(_alive(int(marker.read_text())))
        self.run_async(go())

    def test_a_flooding_program_is_held_to_its_read_budget(self) -> None:
        async def go():
            budget = 256 * 1024
            proc, h = await self.spawn(["yes"], rate=budget)
            await asyncio.sleep(0.2)
            h.out.clear()
            t0, cpu0 = time.monotonic(), time.process_time()
            await asyncio.sleep(1.0)
            elapsed, cpu = time.monotonic() - t0, time.process_time() - cpu0
            self.assertLess(len(h.out), budget * elapsed * 1.6 + 8192, "read more than the budget")
            self.assertGreater(len(h.out), budget * elapsed * 0.3, "starved below the budget")
            self.assertLess(cpu / elapsed, 0.25, "draining a flood must not spin the event loop")
            await proc.terminate(grace=0.5)
        self.run_async(go())

    def test_pending_input_is_capped(self) -> None:
        async def go():
            proc, h = await self.spawn(["sleep", "30"])  # never reads its input
            accepted = 0
            for _ in range(64):
                if not proc.write(b"x" * 65536):
                    break
                accepted += 65536
            self.assertLessEqual(accepted, commands.PTY_MAX_PENDING_INPUT)
            await proc.terminate(grace=0.5)
        self.run_async(go())

    def test_a_missing_program_reports_inside_the_terminal(self) -> None:
        async def go():
            proc, h = await self.spawn(["/nonexistent/shell"])
            await asyncio.wait_for(h.exited.wait(), 5)
            self.assertEqual(h.code, 127)
            self.assertIn(b"cannot start /nonexistent/shell", h.out)
        self.run_async(go())

    def test_nothing_typed_or_printed_reaches_the_command_log(self) -> None:
        async def go():
            proc, h = await self.spawn()
            proc.write(b"echo hunter2-$((5*5))\n")
            await h.wait_for(b"hunter2-25")
            await proc.terminate()
        self.run_async(go())
        logged = self.log.read_text()
        self.assertIn("pty", logged)
        self.assertNotIn("hunter2", logged)


@unittest.skipUnless(uvloop, "uvloop not installed")
class PtyProcessUvloopTests(PtyProcessTests):
    loop_name = "uvloop"


if __name__ == "__main__":
    unittest.main()
