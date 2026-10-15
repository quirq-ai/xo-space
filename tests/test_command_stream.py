"""The executor's process ownership: `run` kills on cancel, `spawn_stream`
starts long-lived processes that are listed until they exit, and
`reap_orphan` stops only the exact process a registry row names.

Every test checks the outcome the design asks for (no process left behind,
grandchildren included) by looking the pids up in /proc, not by asserting
that a kill was called.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import utils.commands as commands
from utils.commands import (
    LineTooLong,
    live_processes,
    process_start_ticks,
    reap_orphan,
    run,
    spawn_stream,
)

PY = sys.executable

# A child that starts a grandchild in the same process group, prints both pids
# and then sleeps; the grandchild sleeps too.
WITH_GRANDCHILD = textwrap.dedent("""
    import subprocess, sys, time
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print(f"{g.pid}", flush=True)
    time.sleep(60)
""")


def alive(pid: int, ticks: int | None) -> bool:
    """The process (pid, ticks) still exists and is not a zombie."""
    if ticks is None or process_start_ticks(pid) != ticks:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


class _Async(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": str(self.tmp / "commands.log"),
                                      "QUIRQ_COMMAND_LOG": ""})
        env.start()
        self.addCleanup(env.stop)
        self.env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}

    async def spawn(self, code: str, label: str = "test") -> commands.StreamProcess:
        proc = await spawn_stream([PY, "-c", code], cwd=self.tmp, env=self.env, label=label)
        self.addAsyncCleanup(proc.terminate, 1.0)
        return proc

    async def first_line(self, proc: commands.StreamProcess) -> bytes:
        return await asyncio.wait_for(anext(proc.lines()), timeout=10)


class RunCancelTests(_Async):
    async def test_cancelled_run_leaves_no_process_behind(self) -> None:
        marker = self.tmp / "pids"
        code = WITH_GRANDCHILD.replace('print(f"{g.pid}", flush=True)',
                                       f'open({str(marker)!r}, "w").write(f"{{__import__(\'os\').getpid()}} {{g.pid}}")')
        task = asyncio.create_task(run([PY, "-c", code]))
        for _ in range(200):
            if marker.exists() and marker.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pids = [int(p) for p in marker.read_text().split()]
        ticks = [process_start_ticks(p) for p in pids]
        self.assertTrue(all(alive(p, t) for p, t in zip(pids, ticks)))

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(any(alive(p, t) for p, t in zip(pids, ticks)), "child or grandchild survived")


class SpawnStreamTests(_Async):
    async def test_lines_write_and_exit(self) -> None:
        proc = await self.spawn("import sys\nfor l in sys.stdin: print(l.strip().upper(), flush=True)")
        lines = proc.lines()
        await proc.write(b"hello\n")
        self.assertEqual(await asyncio.wait_for(anext(lines), 10), b"HELLO")
        await proc.close_stdin()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(anext(lines), 10)
        self.assertEqual(await proc.wait(), 0)

    async def test_large_line_and_a_last_line_without_newline(self) -> None:
        proc = await self.spawn("import sys; sys.stdout.write('x' * (8 << 20) + '\\n' + 'tail')")
        got = [line async for line in proc.lines()]
        self.assertEqual([len(got[0]), got[1]], [8 << 20, b"tail"])

    async def test_line_over_the_limit_is_a_typed_error_and_the_pipe_still_drains(self) -> None:
        with patch.object(commands, "STREAM_LINE_LIMIT", 1024):
            proc = await self.spawn("print('y' * 5000, flush=True)\nfor i in range(1000): print(i, flush=True)")
        with self.assertRaises(LineTooLong):
            await self.first_line(proc)
        # Nobody reads the rest; the process still finishes and is reaped.
        self.assertEqual(await asyncio.wait_for(proc.wait(), 10), 0)

    async def test_terminate_returns_when_the_reader_walked_away(self) -> None:
        proc = await self.spawn("import time\nfor i in range(200000): print(i)\ntime.sleep(60)")
        self.assertEqual(await self.first_line(proc), b"0")
        code = await asyncio.wait_for(proc.terminate(grace_s=1.0), 10)
        self.assertEqual(code, -signal.SIGTERM)
        self.assertFalse(alive(proc.pid, proc.start_ticks))

    async def test_a_slow_reader_gets_every_line_in_order(self) -> None:
        proc = await self.spawn("for i in range(5000): print(i)")
        got = []
        async for line in proc.lines():
            got.append(int(line))
            if len(got) % 500 == 0:
                await asyncio.sleep(0.01)
        self.assertEqual(got, list(range(5000)))

    async def test_stderr_is_drained_so_a_chatty_child_never_blocks(self) -> None:
        # 4 MB of stderr is far beyond a pipe's buffer; nobody reads stderr().
        proc = await self.spawn(textwrap.dedent("""
            import sys
            for i in range(40000): sys.stderr.write(f"warn {i} " + "z" * 90 + "\\n")
            print("done", flush=True)
        """))
        self.assertEqual(await self.first_line(proc), b"done")
        self.assertEqual(await asyncio.wait_for(proc.wait(), 10), 0)
        self.assertIn("warn 39999", proc.stderr_tail())
        self.assertNotIn("warn 0 ", proc.stderr_tail())
        self.assertGreater(proc.stderr_dropped, 0)

    async def test_stderr_lines_reach_a_reader(self) -> None:
        proc = await self.spawn("import sys; sys.stderr.write('a\\nb\\n')")
        self.assertEqual([line async for line in proc.stderr()], [b"a", b"b"])

    async def test_terminate_kills_the_group_even_if_sigterm_is_ignored(self) -> None:
        code = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + WITH_GRANDCHILD
        proc = await self.spawn(code)
        grandchild = int(await self.first_line(proc))
        g_ticks = process_start_ticks(grandchild)
        self.assertTrue(alive(grandchild, g_ticks))

        code = await proc.terminate(grace_s=0.3)
        self.assertEqual(code, -signal.SIGKILL)
        self.assertFalse(alive(proc.pid, proc.start_ticks))
        self.assertFalse(alive(grandchild, g_ticks))
        self.assertEqual(await proc.terminate(grace_s=0.3), code, "terminate is idempotent")

    async def test_signal_reaches_the_process(self) -> None:
        proc = await self.spawn(textwrap.dedent("""
            import signal, sys, time
            signal.signal(signal.SIGINT, lambda *a: (print("interrupted", flush=True), sys.exit(3)))
            print("ready", flush=True)
            time.sleep(60)
        """))
        lines = proc.lines()
        self.assertEqual(await asyncio.wait_for(anext(lines), 10), b"ready")
        await proc.signal(signal.SIGINT)
        self.assertEqual(await asyncio.wait_for(anext(lines), 10), b"interrupted")
        self.assertEqual(await asyncio.wait_for(proc.wait(), 10), 3)

    async def test_registry_lists_a_process_until_it_exits(self) -> None:
        proc = await self.spawn("import sys; sys.stdin.read()", label="registry")
        entry = [p for p in live_processes() if p.pid == proc.pid]
        self.assertEqual(len(entry), 1)
        self.assertEqual((entry[0].label, entry[0].start_ticks), ("registry", process_start_ticks(proc.pid)))
        await proc.close_stdin()
        await asyncio.wait_for(proc.wait(), 10)
        self.assertNotIn(proc.pid, [p.pid for p in live_processes()])

    async def test_env_is_the_whole_environment(self) -> None:
        with patch.dict(os.environ, {"XO_SERVER_ONLY_SECRET": "x"}):
            proc = await self.spawn("import os; print(sorted(k for k in os.environ if k != 'LC_CTYPE'), flush=True)")
            self.assertEqual(await self.first_line(proc), b"['PATH', 'PYTHONUNBUFFERED']")

    async def test_the_command_log_gets_the_command_but_never_the_stream(self) -> None:
        proc = await self.spawn("import sys; print('secret answer', flush=True); sys.stdin.read()", label="logcheck")
        self.assertEqual(await self.first_line(proc), b"secret answer")
        await proc.close_stdin()
        await asyncio.wait_for(proc.wait(), 10)
        log = (self.tmp / "commands.log").read_text()
        self.assertIn("logcheck", log)
        self.assertIn("[output not logged]", log)
        self.assertNotIn("secret answer", log.split("$ ", 1)[1].split("\n", 1)[1])

    async def test_missing_binary(self) -> None:
        with self.assertRaises(FileNotFoundError):
            await spawn_stream(["/nonexistent/cli"], cwd=self.tmp, env=self.env, label="missing")


class ReapOrphanTests(_Async):
    async def test_reaps_the_recorded_process_and_its_group(self) -> None:
        proc = await self.spawn("import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + WITH_GRANDCHILD)
        grandchild = int(await self.first_line(proc))
        g_ticks = process_start_ticks(grandchild)

        self.assertTrue(await reap_orphan(proc.pid, proc.start_ticks, grace_s=0.3))
        await asyncio.wait_for(proc.wait(), 10)
        self.assertFalse(alive(grandchild, g_ticks))

    async def test_a_reused_pid_or_unknown_start_time_is_left_alone(self) -> None:
        proc = await self.spawn("import time; time.sleep(60)")
        self.assertFalse(await reap_orphan(proc.pid, proc.start_ticks + 1, grace_s=0.3))
        self.assertFalse(await reap_orphan(proc.pid, None, grace_s=0.3))
        self.assertTrue(alive(proc.pid, proc.start_ticks))

    async def test_a_process_already_gone_is_reported_not_reaped(self) -> None:
        proc = await self.spawn("pass")
        await asyncio.wait_for(proc.wait(), 10)
        self.assertFalse(await reap_orphan(proc.pid, proc.start_ticks, grace_s=0.3))


if __name__ == "__main__":
    unittest.main()
