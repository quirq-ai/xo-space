"""Control inside a running turn (plan W3's plumbing): the XO process marker
on every process a session starts, the sweep that finds and stops what
carries it (real child processes, pidfds, start times), and ``Engine.halt``
on the fake claude CLI: interrupt, release, sweep, one event."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm.control import marker, sweep
from services.cowork_agent.storm.control.halt import EVENT as HALT_EVENT
from services.cowork_agent.storm.control.marker import MARKER, Mark
from services.cowork_agent.storm.control.sweep import Marked
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message
from utils.commands import process_start_ticks

REPO = Path(__file__).resolve().parents[1]
SPACE = "0123456789ab"
OTHER_SPACE = "ba9876543210"
#: Long enough to outlive any test, short enough that one a failed test leaks ends soon.
SLEEP = "120"


def running(pid: int, ticks: int | None) -> bool:
    """The process ``pid`` started at ``ticks`` still runs (not reused, not a zombie)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return ticks is not None and process_start_ticks(pid) == ticks and state != "Z"


def environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return dict(var.decode().partition("=")[::2] for var in raw.split(b"\0") if var)


class Processes:
    """Marked processes a test starts, each stopped at the end if still running."""

    def __init__(self) -> None:
        self.started: list[tuple[int, int | None]] = []
        self.popens: list[subprocess.Popen] = []

    def start(self, argv: list[str], mark: Mark | None, **options) -> subprocess.Popen:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if mark is not None:
            env = marker.marked(env, mark)
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, **options)
        self.popens.append(proc)
        self.started.append((proc.pid, process_start_ticks(proc.pid)))
        return proc

    def setsid_grandchild(self, mark: Mark) -> tuple[subprocess.Popen, int]:
        """A shell that starts ``setsid sleep`` in the background (out of its
        session and group, as an agent's escaped server is), then becomes a
        sleep itself. Returns the shell and the grandchild's pid."""
        parent = self.start(["sh", "-c", f"setsid sleep {SLEEP} </dev/null >/dev/null 2>&1 & echo $!; "
                                         f"exec sleep {SLEEP}"], mark, stdout=subprocess.PIPE)
        grandchild = int(parent.stdout.readline())
        self.started.append((grandchild, process_start_ticks(grandchild)))
        deadline = time.monotonic() + 10
        while Path(f"/proc/{grandchild}/comm").read_text().strip() != "sleep" and time.monotonic() < deadline:
            time.sleep(0.01)                        # `$!` comes before setsid and the sleep have run
        return parent, grandchild

    def close(self) -> None:
        for pid, ticks in self.started:
            if running(pid, ticks):
                os.kill(pid, signal.SIGKILL)
        for proc in self.popens:
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()


class MarkerTests(unittest.TestCase):
    def test_a_marker_names_the_space_the_session_and_the_epoch(self) -> None:
        mark = Mark(SPACE, "cse_00112233aabbccdd", 4)
        self.assertEqual(mark.value, f"{SPACE}:cse_00112233aabbccdd:4")
        self.assertEqual(marker.parse(mark.value), mark)
        for junk in (None, "", "x", f"{SPACE}:cse_1", f"{SPACE}:cse_1:", f"{SPACE}:cse_1:x", ":cse_1:1"):
            self.assertIsNone(marker.parse(junk), junk)
        env = marker.marked({"PATH": "/bin", MARKER: "inherited:cse_9:1"}, mark)
        self.assertEqual(env, {"PATH": "/bin", MARKER: mark.value})   # the launcher's own replaces any inherited

    def test_the_name_passes_the_env_filters_of_the_clis(self) -> None:
        # A CLI's default tool env policy drops names containing these (case-insensitive).
        self.assertFalse(any(word in MARKER.upper() for word in ("KEY", "SECRET", "TOKEN")))

    def test_the_space_is_the_store_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, copy, link = Path(tmp, "xo.db"), Path(tmp, "copy.db"), Path(tmp, "link.db")
            store.touch()
            link.symlink_to(store)
            self.assertEqual(marker.space_of(store), marker.space_of(link))
            self.assertNotEqual(marker.space_of(store), marker.space_of(copy))
            self.assertEqual(len(marker.space_of(store)), marker.SPACE_HEX)

    def test_a_server_started_from_its_own_session_drops_that_marker_only(self) -> None:
        own = Mark(SPACE, "cse_1", 2)
        with mock.patch.dict(os.environ, {MARKER: own.value}):
            self.assertEqual(marker.forget_own(SPACE), own)
            self.assertNotIn(MARKER, os.environ)
        other = Mark(OTHER_SPACE, "cse_1", 2).value
        with mock.patch.dict(os.environ, {MARKER: other}):
            self.assertIsNone(marker.forget_own(SPACE))          # another Space's agent started it: kept
            self.assertEqual(os.environ[MARKER], other)


class SweepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.procs = Processes()

    def tearDown(self) -> None:
        self.procs.close()

    async def test_a_setsid_grandchild_is_found_and_killed(self) -> None:
        mark = Mark(SPACE, "cse_1", 3)
        parent, grandchild = self.procs.setsid_grandchild(mark)
        self.assertEqual(environ(grandchild)[MARKER], mark.value)     # inherited, and kept past setsid
        self.assertNotEqual(os.getsid(grandchild), os.getsid(parent.pid))

        swept = await sweep.sweep(SPACE, "cse_1")
        self.assertEqual(sorted(p.pid for p, _sig in swept.killed), sorted([parent.pid, grandchild]))
        self.assertEqual({(p.epoch, p.name, sig) for p, sig in swept.killed}, {(3, "sleep", "SIGTERM")})
        self.assertEqual((swept.gone, swept.survived), ((), ()))
        self.assertGreater(swept.scanned, 2)
        self.assertEqual(sweep.scan(SPACE, "cse_1")[0], [])
        self.assertEqual(parent.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(running(*self.procs.started[-1]))

    async def test_only_this_sessions_processes_of_this_space_are_swept(self) -> None:
        mine = self.procs.start(["sleep", SLEEP], Mark(SPACE, "cse_1", 1))
        earlier = self.procs.start(["sleep", SLEEP], Mark(SPACE, "cse_1", 0))     # an earlier process's leftover
        others = [self.procs.start(["sleep", SLEEP], mark) for mark in (
            Mark(SPACE, "cse_10", 1),                      # a session whose id starts with this one's
            Mark(OTHER_SPACE, "cse_1", 1),                 # the same session in a copy of the store
            None)]                                         # unmarked: the person's own
        swept = await sweep.sweep(SPACE, "cse_1")
        self.assertEqual(sorted((p.pid, p.epoch) for p, _sig in swept.killed),
                         sorted([(mine.pid, 1), (earlier.pid, 0)]))
        for proc in others:
            self.assertIsNone(proc.poll())

    async def test_a_reused_pid_is_never_signalled(self) -> None:
        bystander = self.procs.start(["sleep", SLEEP], None)
        ticks = process_start_ticks(bystander.pid)
        # Found earlier under this pid: a process that has ended, its number now the bystander's.
        stale = Marked(bystander.pid, ticks - 1, 1, "sleep")
        killed, gone, survived = await sweep.stop([stale], grace_s=0.2)
        self.assertEqual((killed, gone, survived), ([], [stale], []))
        await asyncio.sleep(0.2)
        self.assertIsNone(bystander.poll())
        self.assertTrue(running(bystander.pid, ticks))

    async def test_a_process_that_ignores_sigterm_gets_sigkill(self) -> None:
        stubborn = self.procs.start(["sh", "-c", f"trap '' TERM; exec sleep {SLEEP}"], Mark(SPACE, "cse_1", 1))
        while Path(f"/proc/{stubborn.pid}/comm").read_text().strip() != "sleep":     # past the trap
            await asyncio.sleep(0.01)
        swept = await sweep.sweep(SPACE, "cse_1", grace_s=0.3)
        self.assertEqual([(p.pid, sig) for p, sig in swept.killed], [(stubborn.pid, "SIGKILL")])
        self.assertEqual(stubborn.wait(timeout=5), -signal.SIGKILL)

    async def test_the_scanning_server_and_its_ancestors_are_spared(self) -> None:
        """A server an agent started from its shell carries that agent's
        marker: its own scan must find the agent's work, never itself."""
        mark = Mark(SPACE, "cse_1", 1)
        code = ("import json, subprocess, sys\n"
                "from services.cowork_agent.storm.control import sweep\n"
                f"child = subprocess.Popen(['sleep', '{SLEEP}'])\n"
                "found, _ = sweep.scan(sys.argv[1], sys.argv[2])\n"
                "print(json.dumps({'me': __import__('os').getpid(), 'child': child.pid, "
                "'found': [p.pid for p in found]}))\n"
                "child.kill(); child.wait()\n")
        env = marker.marked({"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)}, mark)
        done = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, SPACE, "cse_1"], env=env,
                                       cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout)
        self.assertEqual(seen["found"], [seen["child"]])


class EngineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.procs = Processes()

    async def asyncTearDown(self) -> None:
        self.procs.close()
        await self.kit.close()

    def started(self, *after: dict) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]

    async def spawned(self, count: int) -> list[dict]:
        """The processes the fake CLI started, once it has started ``count``."""
        await self.kit.until(lambda: len(self.kit.runs()) == 1 and len(self.kit.runs()[0].get("spawned", ())) >= count)
        out = self.kit.runs()[0]["spawned"]
        for entry in out:
            self.procs.started.append((entry["pid"], process_start_ticks(entry["pid"])))
        return out


class MarkerOnSessionsTests(EngineCase):
    async def test_every_process_a_session_starts_carries_its_marker(self) -> None:
        kit = self.kit
        kit.scenario(self.started({"spawn": ["sleep", SLEEP]}, {"spawn": ["sleep", SLEEP], "setsid": True},
                                  *claude.text_turn(SID, "started"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        child, escaped = await self.spawned(2)
        value = Mark(engine.space, sid, 1).value
        self.assertEqual(engine.space, marker.space_of(kit.state / "engine" / "xo.db"))
        self.assertEqual(kit.runs()[0]["env"][MARKER], value)                # the CLI, from the launcher
        self.assertEqual(environ(child["pid"])[MARKER], value)                # its child
        self.assertEqual(environ(escaped["pid"])[MARKER], value)              # and one that left with setsid
        await kit.recorder.seen("turn.finished", session_id=sid)
        await engine.halt(sid, reason="test", by="test")


class HaltTests(EngineCase):
    async def test_halt_interrupts_a_running_turn_releases_and_leaves_nothing_marked(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", {"command": "sleep 30"}),
                                  {"spawn": ["sleep", SLEEP]}, {"spawn": ["sleep", SLEEP], "setsid": True},
                                  *claude.host_control("interrupt"),
                                  claude.result(SID, "", subtype="error_during_execution",
                                                terminal_reason="aborted_tools"),
                                  claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        started = await kit.recorder.seen("turn.started", session_id=sid)
        child, escaped = await self.spawned(2)

        halted = await engine.halt(sid, reason="stopped", by="founder")
        self.assertEqual((halted.turn_id, halted.interrupted, halted.released), (started.turn_id, True, True))
        # The CLI's own child died with its process group; the one that left it, by the sweep.
        self.assertEqual([(p.pid, p.name, sig) for p, sig in halted.swept.killed],
                         [(escaped["pid"], "sleep", "SIGTERM")])
        self.assertEqual(halted.swept.survived, ())
        self.assertFalse(running(*self.procs.started[0]))
        self.assertFalse(running(*self.procs.started[1]))
        self.assertEqual(sweep.scan(engine.space, sid)[0], [])
        self.assertLess(halted.interrupt_s, 1.0)
        self.assertLess(halted.total_s, 5.0)

        # The turn was stopped, by whom; its wake consumed, never handed back; the process gone.
        stopped = await kit.recorder.seen("turn.stopped", session_id=sid)
        self.assertEqual(stopped.data["by"], "founder")
        self.assertEqual(kit.rows("SELECT status FROM turns"), [("stopped",)])
        self.assertEqual(kit.rows("SELECT state FROM wakes"), [("consumed",)])
        self.assertEqual((await engine.get_session(sid))["state"], "cold")
        (event,) = kit.recorder.of(HALT_EVENT, sid)
        self.assertEqual({k: event.data[k] for k in ("status", "reason", "by", "turn", "interrupted", "released",
                                                      "found", "gone")},
                         {"status": "cold", "reason": "stopped", "by": "founder", "turn": started.turn_id,
                          "interrupted": True, "released": True, "found": 1, "gone": 0})
        self.assertEqual(event.data["killed"], [{"pid": escaped["pid"], "epoch": 1, "name": "sleep",
                                                 "signal": "SIGTERM"}])
        self.assertEqual(set(event.data["ms"]), {"interrupt", "release", "scan", "kill", "total"})
        self.assertEqual(event.epoch, 1)
        await kit.recorder.wait(lambda: not kit.rows("SELECT pid FROM procs"))

    async def test_a_halt_on_an_idle_session_releases_it_and_still_sweeps(self) -> None:
        kit = self.kit
        kit.scenario(self.started({"spawn": ["sleep", SLEEP], "setsid": True},
                                  *claude.text_turn(SID, "server started"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        (escaped,) = await self.spawned(1)
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)

        halted = await engine.halt(sid, reason="paused", by="founder")
        self.assertEqual((halted.turn_id, halted.interrupted, halted.released), (None, False, True))
        self.assertEqual([p.pid for p, _sig in halted.swept.killed], [escaped["pid"]])
        self.assertFalse(running(*self.procs.started[0]))
        closed = await kit.recorder.seen("session", session_id=sid, status="cold", count=2)
        self.assertEqual(closed.data["reason"], "stopped")
        self.assertEqual(kit.rows("SELECT status FROM turns"), [("finished",)])
        self.assertNotIn("turn.stopped", kit.recorder.types(sid))
        await kit.recorder.wait(lambda: not kit.rows("SELECT pid FROM procs"))

    async def test_a_halt_on_a_cold_session_only_sweeps(self) -> None:
        kit = self.kit
        kit.scenario([])
        engine = await kit.boot()
        sid = await kit.session()
        # Nothing ran: the halt records that it found nothing.
        quiet = await engine.halt(sid, reason="stopped", by="cse_coordinator")
        self.assertEqual((quiet.turn_id, quiet.interrupted, quiet.released, quiet.swept.found),
                         (None, False, False, 0))
        # What an earlier process of the session left running (an escaped server) is swept.
        parent, grandchild = self.procs.setsid_grandchild(Mark(engine.space, sid, 2))
        halted = await engine.halt(sid, reason="stopped", by="founder")
        self.assertEqual(sorted(p.pid for p, _sig in halted.swept.killed), sorted([parent.pid, grandchild]))
        self.assertEqual((halted.interrupted, halted.released), (False, False))
        first, second = kit.recorder.of(HALT_EVENT, sid)
        self.assertEqual((first.data["status"], first.data["by"], first.data["found"]), ("cold", "cse_coordinator", 0))
        self.assertEqual({k["epoch"] for k in second.data["killed"]}, {2})
        self.assertEqual(kit.runs(), [])                                    # it never started one

    async def test_a_stop_that_cannot_be_recorded_still_releases_and_sweeps(self) -> None:
        kit = self.kit
        kit.scenario(self.started({"spawn": ["sleep", SLEEP], "setsid": True},
                                  claude.tool_call(SID, "t1", "Bash", {"command": "sleep 30"}), {"sleep": 60}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("step.tool", session_id=sid)
        (escaped,) = await self.spawned(1)
        live = engine._live[sid]
        with mock.patch.object(live, "stop", side_effect=RuntimeError("the store failed")):
            halted = await engine.halt(sid, reason="stopped", by="founder")
        self.assertEqual((halted.interrupted, halted.released), (False, True))
        self.assertEqual([p.pid for p, _sig in halted.swept.killed], [escaped["pid"]])
        await kit.recorder.seen(HALT_EVENT, session_id=sid)
        await kit.recorder.wait(lambda: not kit.rows("SELECT pid FROM procs"))


if __name__ == "__main__":
    unittest.main()
