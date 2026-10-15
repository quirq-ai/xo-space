"""Conformance: every runtime, run against its fake CLI, behaves as its
``check()`` and feature list say (storm ch 01 §7, ch 06 §2).

The suite finds runtimes through the capability loader
(``adapters/<agent>/runtime/``), so a runtime joins it by existing. It checks
that each runtime's feature list names every contract feature (unsupported
ones too) and that ``check()`` starts nothing, then drives a session of each
runtime through its fake CLI, speaking only the contract:

* ``turn``: one turn with a tool call; events in a valid order
  (``event_order_problems``), ending with ``SessionClosed``;
* ``coordinator_write``: a coordinator's write reaches the host and is
  denied; the limits it asks for are enforced by the CLI, a hook or the host,
  never the prompt;
* ``held_permission``: a check is held, with no timeout, until the host
  answers, then the turn goes on;
* ``interrupt``: the turn ends ``interrupted`` and the session takes the next
  message;
* ``close``: no process is left, and ``close`` is idempotent;
* ``unknown_wire``: an unknown line becomes a ``Notice``, never a crash.

Every runtime must have ``BINDINGS[agent]``: the fake CLI, the env variable
the runtime reads its binary from, and a scenario per case, written in that
CLI's wire format. A runtime with no binding fails here rather than passing
unchecked.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.registry.adapter_registry import list_adapters
from tests.fakes import claude, install, scenario
from tests.fakes.host import ScriptedHost
from utils.commands import live_processes

#: Every feature a runtime's feature list must name (ch 01 §4.8), with the
#: names its SupportReport items use: the spec's fields, the session
#: operations, and the integrity check's input.
FEATURE_KEYS = (
    *(f.name for f in fields(c.SessionSpec)),
    "permissions.mode", "tools.deny", "send_while_running", "interrupt", "update.model", "update.effort",
    "update.permission_mode", "runs.list", "runs.read", "runs.stop", "runs.message", "models", "usage",
    "oneshot", "project_config_paths",
)


@dataclass(frozen=True)
class Binding:
    """How the suite drives one agent's runtime against its fake."""

    fake_cli: str                       # tests.fakes.<fake_cli>
    cli_path_env: str                   # the variable the runtime reads its binary from
    scenarios: dict[str, Callable[[], dict]]   # case name -> scenario


CASES = ("turn", "coordinator_write", "held_permission", "interrupt", "close", "unknown_wire")


def _claude_scenarios() -> dict[str, Callable[[], dict]]:
    sid = "native-conf"
    started = [*claude.initialize(), claude.user_message(), claude.init(sid)]

    def tool(call_id: str, name: str, args: dict) -> list[dict]:
        return [claude.tool_call(sid, call_id, name, args, message_id=f"m_{call_id}")]

    return {
        "turn": lambda: scenario([
            *started, *tool("t1", "Bash", {"command": "ls", "description": "List files"}),
            claude.tool_result(sid, "t1", "README.md"), *claude.text_turn(sid, "done"), claude.idle(sid)]),
        "coordinator_write": lambda: scenario([
            *started, *tool("w1", "Write", {"file_path": "/work/x.txt", "content": "hi"}),
            claude.can_use_tool("p1", "Write", {"file_path": "/work/x.txt", "content": "hi"}, tool_use_id="w1"),
            claude.permission_answer("p1", "deny"),
            claude.tool_result(sid, "w1", "denied", is_error=True), *claude.text_turn(sid, "ok"), claude.idle(sid)]),
        "held_permission": lambda: scenario([
            *started, *tool("h1", "Bash", {"command": "git push"}),
            claude.can_use_tool("p2", "Bash", {"command": "git push"}, tool_use_id="h1"),
            claude.permission_answer("p2", "allow"),
            claude.tool_result(sid, "h1", "pushed"), *claude.text_turn(sid, "pushed"), claude.idle(sid)]),
        "interrupt": lambda: scenario([
            *started, *tool("i1", "Bash", {"command": "sleep 60"}),
            *claude.host_control("interrupt"),
            claude.tool_result(sid, "i1", "[Request interrupted by user]", is_error=True),
            claude.result(sid, "", subtype="error_during_execution", terminal_reason="aborted_tools"),
            claude.idle(sid), claude.user_message(), *claude.text_turn(sid, "still here"), claude.idle(sid)]),
        "close": lambda: scenario([*claude.initialize()]),
        "unknown_wire": lambda: scenario([
            *started, {"emit": {"type": "brand_new_line", "x": 1}}, {"emit_raw": "not json at all"},
            *claude.text_turn(sid, "fine"), claude.idle(sid)]),
    }


#: Filled in by each runtime's PR (claude_code in B2, codex and antigravity in B4).
BINDINGS: dict[str, Binding] = {
    "claude_code": Binding(fake_cli="claude", cli_path_env="CLAUDE_CLI_PATH", scenarios=_claude_scenarios()),
}


def runtimes() -> list[tuple[str, c.Runtime]]:
    found = []
    for agent in list_adapters():
        module = try_load_capability("runtime", agent=agent)
        if module is not None:
            found.append((agent, module.runtime()))
    return found


def event_order_problems(events: list[c.Event]) -> list[str]:
    """What is wrong with one session's event sequence, or [] if nothing:
    per-session ``seq`` strictly increasing; every turn starts before it
    finishes and finishes once; every started tool call finishes; nothing
    after ``SessionClosed``."""
    problems: list[str] = []
    last_seq = None
    open_turns: set[str] = set()
    finished_turns: set[str] = set()
    open_tools: set[str] = set()
    closed = False
    for ev in events:
        name = type(ev).__name__
        if closed:
            problems.append(f"{name} after SessionClosed")
        if last_seq is not None and ev.seq <= last_seq:
            problems.append(f"{name} seq {ev.seq} after {last_seq}")
        last_seq = ev.seq
        if isinstance(ev, c.TurnStarted):
            open_turns.add(ev.turn_id)
        elif isinstance(ev, c.TurnFinished):
            if ev.turn_id in finished_turns:
                problems.append(f"turn {ev.turn_id} finished twice")
            elif ev.turn_id not in open_turns:
                problems.append(f"turn {ev.turn_id} finished without starting")
            open_turns.discard(ev.turn_id)
            finished_turns.add(ev.turn_id)
        elif isinstance(ev, c.ToolStarted):
            open_tools.add(ev.call_id)
        elif isinstance(ev, c.ToolFinished):
            if ev.call_id not in open_tools:
                problems.append(f"tool {ev.call_id} finished without starting")
            open_tools.discard(ev.call_id)
        elif isinstance(ev, c.SessionClosed):
            closed = True
    problems += [f"turn {t} never finished" for t in sorted(open_turns)]
    problems += [f"tool {t} never finished" for t in sorted(open_tools)]
    return problems


class ConformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.found = runtimes()
        if not self.found:
            self.skipTest("no adapter ships a runtime yet (the claude_code runtime arrives in B2)")

    def test_every_runtime_has_a_binding(self) -> None:
        for agent, _ in self.found:
            with self.subTest(agent):
                self.assertIn(agent, BINDINGS, f"add BINDINGS[{agent!r}] so the suite can drive it")

    def test_feature_list_names_every_contract_feature(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                listed = runtime.features()
                self.assertEqual((listed.agent, listed.contract_version), (agent, c.CONTRACT_VERSION))
                self.assertEqual(sorted(set(FEATURE_KEYS) - set(listed.features)), [])

    def test_check_starts_nothing(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                before = {p.pid for p in live_processes()}
                runtime.check(c.SessionSpec(cwd="/"))
                self.assertEqual({p.pid for p in live_processes()} - before, set())


class SessionConformanceTests(unittest.IsolatedAsyncioTestCase):
    """Each case drives every runtime through its fake with the contract only."""

    def setUp(self) -> None:
        self.found = runtimes()
        if not self.found:
            self.skipTest("no adapter ships a runtime yet")

    async def run_case(self, agent: str, runtime: c.Runtime, case: str, *, spec: dict | None = None,
                       host: ScriptedHost | None = None, drive=None) -> tuple[list[c.Event], ScriptedHost]:
        binding = BINDINGS[agent]
        host = host or ScriptedHost(decide=lambda req: c.PermissionDecision(allow=True))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = install(binding.fake_cli, root / "bin", binding.scenarios[case]())
            work = root / "work"
            work.mkdir()
            env = {binding.cli_path_env: str(fake.path), "QUIRQ_STATE_ROOT": str(root / "state"),
                   "HOME": str(root / "home")}
            with mock.patch.dict(os.environ, env):
                session = await runtime.open(c.SessionSpec(cwd=str(work), **(spec or {})), host,
                                             session_id=f"cse_conf_{case}")
                events: list[c.Event] = []

                async def consume() -> None:
                    async for ev in session.events():
                        events.append(ev)

                reader = asyncio.create_task(consume())
                try:
                    await (drive or self.one_turn)(session, events)
                finally:
                    await asyncio.wait_for(session.close(), 20)
                    await asyncio.wait_for(reader, 20)
            mismatches = [run.get("mismatch") for run in fake.invocations() if run.get("mismatch")]
            self.assertEqual(mismatches, [], f"{agent}: the fake saw something it didn't expect")
            self.assertEqual(sorted(p.name for p in work.iterdir()), [], "the fake session wrote into cwd")
        return events, host

    async def one_turn(self, session: c.Session, events: list[c.Event]) -> None:
        await session.send(c.UserInput(text="hello"))
        await wait_for_event(events, c.SessionIdle, after=c.TurnFinished)

    def check_order(self, events: list[c.Event]) -> None:
        self.assertEqual(event_order_problems(events), [])
        self.assertIsInstance(events[0], c.SessionStarted)
        self.assertIsInstance(events[-1], c.SessionClosed)

    async def test_turn(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                events, _ = await self.run_case(agent, runtime, "turn")
                self.check_order(events)
                kinds = [type(e).__name__ for e in events]
                for name in ("TurnStarted", "ToolStarted", "ToolFinished", "TurnFinished"):
                    self.assertIn(name, kinds)
                finished = [e for e in events if isinstance(e, c.TurnFinished)]
                self.assertEqual([e.status for e in finished], ["completed"])

    async def test_coordinator_write_reaches_the_host_and_is_denied(self) -> None:
        spec = {"orchestration": "coordinator", "permissions": c.PermissionPolicy(mode="deny_unasked"),
                "tools": c.ToolPolicy(preset="read_only")}
        for agent, runtime in self.found:
            with self.subTest(agent):
                report = runtime.check(c.SessionSpec(cwd="/", **spec))
                for feature in ("orchestration", "permissions.mode"):
                    self.assertIn(report.weakest(feature).enforced_by, ("cli", "hook", "host"), feature)
                read_only = ("read", "search", "list", "subagent")
                host = ScriptedHost(decide=lambda req: c.PermissionDecision(
                    allow=req.kind in read_only, message="coordinators are read-only"))
                events, host = await self.run_case(agent, runtime, "coordinator_write", spec=spec, host=host)
                self.check_order(events)
                writes = [r for r in host.permission_requests if r.kind not in read_only]
                self.assertTrue(writes, "the write never reached the host")
                denied = {e.request_id for e in events if isinstance(e, c.PermissionResolved) and not e.decision.allow}
                self.assertEqual({r.request_id for r in writes} - denied, set())
                calls = {r.call_id for r in writes}
                self.assertTrue(all(e.status == "denied" for e in events
                                    if isinstance(e, c.ToolFinished) and e.call_id in calls))

    async def test_held_permission_waits_for_the_host(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                release = asyncio.Event()

                async def decide(req: c.PermissionRequest) -> c.PermissionDecision:
                    await release.wait()
                    return c.PermissionDecision(allow=True)

                async def drive(session: c.Session, events: list[c.Event]) -> None:
                    await session.send(c.UserInput(text="push it"))
                    await wait_for_event(events, c.PermissionRequested)
                    await asyncio.sleep(1.0)             # held: nothing resolves it meanwhile
                    self.assertFalse(any(isinstance(e, (c.PermissionResolved, c.TurnFinished)) for e in events))
                    self.assertEqual(session.state, "waiting_host")
                    release.set()
                    await wait_for_event(events, c.SessionIdle, after=c.TurnFinished)

                events, _ = await self.run_case(agent, runtime, "held_permission",
                                                host=ScriptedHost(decide=decide), drive=drive)
                self.check_order(events)
                resolved = [e for e in events if isinstance(e, c.PermissionResolved)]
                self.assertEqual([e.decision.allow for e in resolved], [True])
                self.assertEqual([e.status for e in events if isinstance(e, c.TurnFinished)], ["completed"])

    async def test_interrupt_keeps_the_session(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                async def drive(session: c.Session, events: list[c.Event]) -> None:
                    await session.send(c.UserInput(text="sleep"))
                    await wait_for_event(events, c.ToolStarted)
                    await session.interrupt(grace_s=10)
                    await wait_for_event(events, c.TurnFinished)
                    await session.send(c.UserInput(text="still there?"))
                    await wait_for_event(events, c.TurnFinished, count=2)

                events, _ = await self.run_case(agent, runtime, "interrupt", drive=drive)
                self.check_order(events)
                self.assertEqual([e.status for e in events if isinstance(e, c.TurnFinished)],
                                 ["interrupted", "completed"])
                self.assertEqual([e.reason for e in events if isinstance(e, c.SessionClosed)], ["closed"])

    async def test_close_leaves_no_process(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                host = ScriptedHost()

                async def drive(session: c.Session, events: list[c.Event]) -> None:
                    pids = [p.pid for p in host.launcher.spawned]
                    self.assertTrue(pids)
                    await session.close()
                    await session.close()                # idempotent
                    live = {p.pid for p in live_processes()}
                    for pid in pids:
                        self.assertNotIn(pid, live)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)

                events, _ = await self.run_case(agent, runtime, "close", host=host, drive=drive)
                self.check_order(events)

    async def test_unknown_wire_data_is_a_notice(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                events, _ = await self.run_case(agent, runtime, "unknown_wire")
                self.check_order(events)
                self.assertTrue(any(isinstance(e, c.Notice) and e.code == "unknown_wire" for e in events))
                self.assertEqual([e.status for e in events if isinstance(e, c.TurnFinished)], ["completed"])


async def wait_for_event(events: list[c.Event], kind: type, *, after: type | None = None, count: int = 1,
                         timeout: float = 20) -> None:
    """Until ``count`` events of ``kind`` (after the first ``after``) arrived."""
    async def seen() -> None:
        while True:
            start = next((i for i, e in enumerate(events) if isinstance(e, after)), None) if after else 0
            if start is not None and sum(isinstance(e, kind) for e in events[start:]) >= count:
                return
            await asyncio.sleep(0.02)
    await asyncio.wait_for(seen(), timeout)


class EventOrderTests(unittest.TestCase):
    ENV = dict(session="cse_1", ts="2026-10-15T12:00:00Z")

    def ev(self, cls, seq: int, **kw) -> c.Event:
        return cls(**self.ENV, seq=seq, **kw)

    def test_a_valid_turn(self) -> None:
        events = [
            self.ev(c.SessionStarted, 1, model=None, cwd="/w", cli_version="1"),
            self.ev(c.TurnStarted, 2, turn_id="t1", origin="person"),
            self.ev(c.ToolStarted, 3, turn_id="t1", call_id="k", tool="Bash", kind="command", title="ls", target="ls"),
            self.ev(c.ToolFinished, 4, turn_id="t1", call_id="k", status="ok"),
            self.ev(c.TurnFinished, 5, turn_id="t1", status="completed"),
            self.ev(c.SessionClosed, 6, reason="closed"),
        ]
        self.assertEqual(event_order_problems(events), [])

    def test_each_kind_of_disorder_is_named(self) -> None:
        events = [
            self.ev(c.TurnFinished, 2, turn_id="t0", status="completed"),
            self.ev(c.TurnStarted, 2, turn_id="t1", origin="person"),
            self.ev(c.ToolStarted, 3, turn_id="t1", call_id="k", tool="x", kind="other", title="x", target=None),
            self.ev(c.ToolFinished, 4, call_id="z", status="ok"),
            self.ev(c.SessionClosed, 5, reason="closed"),
            self.ev(c.Notice, 6, code="late", message="after close"),
        ]
        self.assertEqual(event_order_problems(events), [
            "turn t0 finished without starting",
            "TurnStarted seq 2 after 2",
            "tool z finished without starting",
            "Notice after SessionClosed",
            "turn t1 never finished",
            "tool k never finished",
        ])


if __name__ == "__main__":
    unittest.main()
