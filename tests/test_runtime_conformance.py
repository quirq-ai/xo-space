"""Conformance: every runtime, run against its fake CLI, behaves as its
``check()`` and feature list say (storm ch 01 §7, ch 06 §2).

The suite finds runtimes through the capability loader
(``adapters/<agent>/runtime/``), so a runtime joins it by existing. It checks
that each runtime's feature list names every contract feature (unsupported
ones too) and that ``check()`` starts nothing, then drives a session of each
runtime through its fake CLI, speaking only the contract:

* ``turn``: one turn with a tool call; events in a valid order
  (``event_order_problems``: the input ends with ``SessionIdle``), ending
  with ``SessionClosed``;
* ``coordinator_write``: a coordinator's write reaches the host and is
  denied; the limits it asks for are enforced by the CLI, a hook or the host,
  never the prompt;
* ``held_permission``: a check is held, with no timeout, until the host
  answers, then the turn goes on;
* ``interrupt``: the turn ends ``interrupted`` and the session takes the next
  message;
* ``close``: no process is left, and ``close`` is idempotent;
* ``unknown_wire``: an unknown line becomes a ``Notice``, never a crash;
* ``never_idle``: the suite's own proof that its ``SessionIdle`` check
  bites: the CLI's wire for a turn with the idle left out must be named a
  problem (the engine keeps such an input open, ch 01 §5.2).

It also reads a sub-agent's transcript from the CLI's own files, whole and a
step at a time: every item is the contract's ``TranscriptItem`` (§0.110),
the pages join up, and a cursor no page gave is refused
(``transcript_problems``, ``run_transcript_problems``).

Every runtime must have ``BINDINGS[agent]``: the fake CLI, the env variable
the runtime reads its binary from, a scenario per case written in that CLI's
wire format, and that CLI's files for a session with a sub-agent run. A
runtime with no binding fails here rather than passing unchecked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import shutil
import tempfile
import typing
import unittest
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Sequence
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.registry.adapter_registry import list_adapters
from tests.fakes import claude, install, scenario
from tests.fakes.host import ScriptedHost
from utils.commands import live_processes

#: Every feature a runtime's feature list must name (ch 01 §4.8), with the
#: names its SupportReport items use: the spec's fields, the session
#: operations, the integrity check's input, and the live tool bytes the
#: stream's presenter offers (ch 09).
FEATURE_KEYS = (
    *(f.name for f in fields(c.SessionSpec)),
    "permissions.mode", "permissions.auto", "tools.deny", "send_while_running", "interrupt", "update.model", "update.effort",
    "update.permission_mode", "runs.list", "runs.read", "runs.stop", "runs.message", "models", "usage",
    "oneshot", "project_config_paths", "tool_input_delta", "tool_output_delta",
)


@dataclass(frozen=True)
class Binding:
    """How the suite drives one agent's runtime against its fake."""

    fake_cli: str                       # tests.fakes.<fake_cli>
    cli_path_env: str                   # the variable the runtime reads its binary from
    scenarios: dict[str, Callable[[], dict]]   # case name -> scenario
    # Writes the CLI's own files under a home folder, for a session with a
    # sub-agent run holding every kind of step; returns (native_id, run_id).
    transcript: Callable[[Path], tuple[str, str]]


CASES = ("turn", "coordinator_write", "held_permission", "interrupt", "close", "unknown_wire", "never_idle")
#: What ``event_order_problems`` says of an input that never ended.
NO_IDLE = "an input ended without SessionIdle"


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
        "never_idle": lambda: scenario([*started, *claude.text_turn(sid, "done")]),
    }


def _claude_transcript(home: Path) -> tuple[str, str]:
    shutil.copytree(Path(__file__).parent / "fixtures" / "claude-home", home / ".claude")
    return "11111111-1111-4111-8111-111111111111", "a1b2"


#: Filled in by each runtime's PR (claude_code in B2, codex and antigravity in B4).
BINDINGS: dict[str, Binding] = {
    "claude_code": Binding(fake_cli="claude", cli_path_env="CLAUDE_CLI_PATH", scenarios=_claude_scenarios(),
                           transcript=_claude_transcript),
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
    finishes and finishes once; every started tool call finishes, and no
    ``ToolInputDelta`` comes after its ``ToolFinished``; nothing after
    ``SessionClosed``; every input ends with ``SessionIdle`` (the CLI has
    nothing left to do for it) after its last ``TurnFinished``, before the
    session closes. A ``ToolInputDelta`` may come *before* its
    ``ToolStarted``: a CLI can stream a call's input before the call is whole
    (claude does)."""
    problems: list[str] = []
    last_seq = None
    open_turns: set[str] = set()
    finished_turns: set[str] = set()
    open_tools: set[str] = set()
    finished_tools: set[str] = set()
    closed = False
    awaiting_idle = False                   # a turn finished and no SessionIdle has come since
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
            awaiting_idle = True
        elif isinstance(ev, c.SessionIdle):
            awaiting_idle = False
        elif isinstance(ev, c.ToolStarted):
            open_tools.add(ev.call_id)
        elif isinstance(ev, c.ToolFinished):
            if ev.call_id not in open_tools:
                problems.append(f"tool {ev.call_id} finished without starting")
            open_tools.discard(ev.call_id)
            finished_tools.add(ev.call_id)
        elif isinstance(ev, c.ToolInputDelta) and ev.call_id in finished_tools:
            problems.append(f"tool {ev.call_id} input after it finished")
        elif isinstance(ev, c.SessionClosed):
            closed = True
    if awaiting_idle:
        problems.append(NO_IDLE)
    problems += [f"turn {t} never finished" for t in sorted(open_turns)]
    problems += [f"tool {t} never finished" for t in sorted(open_tools)]
    return problems


#: The fields each transcript item type uses (contract ``TranscriptItem``);
#: every other field keeps its default.
ITEM_FIELDS: dict[str, frozenset[str]] = {
    "input": frozenset({"text"}),
    "text": frozenset({"text"}),
    "thinking": frozenset({"text", "steps", "duration_ms"}),
    "tool_call": frozenset({"call_id", "tool", "kind", "title", "target", "plan"}),
    "tool_result": frozenset({"call_id", "status", "text", "bytes", "truncated"}),
    "error": frozenset({"text"}),
    "other": frozenset({"text"}),
}
#: Of those, the ones an item of the type must fill.
ITEM_REQUIRED: dict[str, frozenset[str]] = {"tool_call": frozenset({"call_id", "tool", "kind", "title"}),
                                            "tool_result": frozenset({"call_id", "status"}),
                                            "error": frozenset({"text"}),
                                            "other": frozenset({"text"})}
#: The item types every CLI's sub-agent has, so a binding's run must show them.
ITEM_TYPES_EVERY_RUN_HAS = frozenset({"input", "text", "tool_call", "tool_result"})
_STATUS = typing.get_type_hints(c.TranscriptItem)["status"]      # Literal[...] | None
RESULT_STATUSES = typing.get_args(typing.get_args(_STATUS)[0])
PLAN_STATES = typing.get_args(typing.get_type_hints(c.PlanItem)["state"])
UTC_SECOND = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
#: A cursor no page gave, and a run no CLI keeps.
FOREIGN_CURSOR = "\u2603 not a cursor"
MISSING_RUN = "no-such-run"


def _text_or_none(value: object) -> bool:
    return value is None or isinstance(value, str)


def _count_or_none(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


#: Each field of ``TranscriptItem``: what its annotation allows, and the test.
FIELD_TYPES: dict[str, tuple[str, Callable[[object], bool]]] = {
    "id": ("a str", lambda v: isinstance(v, str)),
    "type": ("a str", lambda v: isinstance(v, str)),
    "ts": ("a str or None", _text_or_none),
    "text": ("a str", lambda v: isinstance(v, str)),
    "steps": ("a tuple of str", lambda v: isinstance(v, tuple) and all(isinstance(x, str) for x in v)),
    "duration_ms": ("a whole number or None", _count_or_none),
    "call_id": ("a str or None", _text_or_none),
    "tool": ("a str or None", _text_or_none),
    "kind": ("a str or None", _text_or_none),
    "title": ("a str or None", _text_or_none),
    "target": ("a str or None", _text_or_none),
    "plan": ("a tuple of PlanItem", lambda v: isinstance(v, tuple) and all(
        isinstance(x, c.PlanItem) and isinstance(x.text, str) and x.state in PLAN_STATES for x in v)),
    "status": ("a str or None", _text_or_none),
    "bytes": ("a whole number or None", _count_or_none),
    "truncated": ("a bool", lambda v: isinstance(v, bool)),
}


def _defaults() -> dict[str, object]:
    return {f.name: f.default if f.default is not dataclasses.MISSING else f.default_factory()
            for f in fields(c.TranscriptItem) if f.name not in ("id", "type", "ts")}


def transcript_problems(items: Sequence[object]) -> list[str]:
    """What is wrong with a transcript's items, read in order, or [] if
    nothing: each is a ``TranscriptItem`` whose every field has the type its
    annotation allows (``FIELD_TYPES``), with a unique id, a type from the
    contract's set, ``ts`` a UTC second ending in Z (or None), its type's
    required fields filled and no other type's set; a call's ``kind`` from
    ``ToolKind``, a ``plan`` only on a plan call; a result's status from the
    contract's set, made after its call, and ``bytes``/``truncated`` that
    agree with its text."""
    problems: list[str] = []
    defaults = _defaults()
    ids: set[str] = set()
    calls: set[str] = set()
    for n, item in enumerate(items):
        if not isinstance(item, c.TranscriptItem):
            problems.append(f"item {n} is a {type(item).__name__}, not a TranscriptItem")
            continue
        name = f"item {n} ({item.id!r})"
        wrong = [f"{name} has {k} {getattr(item, k)!r}, not {what}" for k, (what, ok) in FIELD_TYPES.items()
                 if not ok(getattr(item, k))]
        if wrong:
            problems += wrong
            continue
        if not item.id:
            problems.append(f"{name} has no id")
        elif item.id in ids:
            problems.append(f"{name} repeats an id")
        ids.add(item.id)
        if item.type not in ITEM_FIELDS:
            problems.append(f"{name} has type {item.type!r}, not one of the contract's")
            continue
        if item.ts is not None and not (isinstance(item.ts, str) and UTC_SECOND.fullmatch(item.ts)):
            problems.append(f"{name} has ts {item.ts!r}, not a UTC second ending in Z")
        stray = sorted(k for k, default in defaults.items()
                       if k not in ITEM_FIELDS[item.type] and getattr(item, k) != default)
        if stray:
            problems.append(f"{name} is {item.type} but sets {', '.join(stray)}")
        missing = sorted(k for k in ITEM_REQUIRED.get(item.type, ()) if not getattr(item, k))
        if missing:
            problems.append(f"{name} is {item.type} without {', '.join(missing)}")
        if item.type == "tool_call":
            if item.kind not in typing.get_args(c.ToolKind):
                problems.append(f"{name} has kind {item.kind!r}, not a ToolKind")
            if item.plan and item.kind != "plan":
                problems.append(f"{name} carries a plan on a call of kind {item.kind!r}")
            calls.add(item.call_id)
        elif item.type == "tool_result":
            if item.status not in RESULT_STATUSES:
                problems.append(f"{name} has status {item.status!r}, not one of {', '.join(RESULT_STATUSES)}")
            if item.call_id not in calls:
                problems.append(f"{name} answers call {item.call_id!r}, which no earlier item made")
            size = len(item.text.encode())
            if item.bytes is not None and item.bytes < size:
                problems.append(f"{name} has bytes={item.bytes}, less than its {size}-byte text")
            elif item.truncated != (item.bytes is not None and item.bytes > size):
                problems.append(f"{name} says truncated={item.truncated} with bytes={item.bytes} for a "
                                f"{size}-byte text")
    return problems


async def run_transcript_problems(binding: Binding, runtime: c.Runtime) -> list[str]:
    """What is wrong with reading the binding's sub-agent run: its items
    read whole (``transcript_problems``), the same items read a step at a
    time, a cursor no page gave refused with ``RuntimeFailure("protocol")``,
    a run the CLI keeps no transcript of with ``"session_not_found"``, and a
    run that shows at least every item type each CLI's runs have."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        native_id, run_id = binding.transcript(home)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            whole = await runtime.read_run(native_id, run_id, limit=10_000)
            problems += transcript_problems(whole.items)
            if whole.next is not None:
                problems.append("one read of the whole run still gives a next page")
            paged, after = [], None
            for _ in range(len(whole.items) + 1):        # a page holds at least one item until the end
                page = await runtime.read_run(native_id, run_id, after=after, limit=1)
                paged += page.items
                if (after := page.next) is None:
                    break
            else:
                problems.append("reading a step at a time never reached the end")
            if paged != list(whole.items):
                problems.append("reading a step at a time gives other items than one read")
            problems += await _refusal("a cursor no page gave", "protocol",
                                       runtime.read_run(native_id, run_id, after=FOREIGN_CURSOR))
            problems += await _refusal("a run the CLI keeps no transcript of", "session_not_found",
                                       runtime.read_run(native_id, MISSING_RUN))
    shown = {item.type for item in whole.items if isinstance(item, c.TranscriptItem)}
    if not ITEM_TYPES_EVERY_RUN_HAS <= shown:
        problems.append(f"the binding's run has no {', '.join(sorted(ITEM_TYPES_EVERY_RUN_HAS - shown))} item")
    return problems


async def _refusal(what: str, code: str, read: typing.Awaitable[c.TranscriptPage]) -> list[str]:
    """[] when ``read`` raises ``RuntimeFailure(code)``; else what it did,
    which core couldn't map to its answer (anything but RuntimeFailure is a 500)."""
    try:
        await read
    except c.RuntimeFailure as exc:
        return [] if exc.code == code else [f"{what} failed {exc.code!r}, not {code!r}"]
    except Exception as exc:                # reported, so the suite names the runtime's mistake
        return [f"{what} raised {type(exc).__name__}, not RuntimeFailure({code!r})"]
    return [f"{what} was read"]


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
                    await wait_for_event(events, c.SessionIdle, after=c.TurnFinished, count=2)

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

    async def test_an_input_without_session_idle_is_named(self) -> None:
        """The check above, proven against each CLI's wire: a turn whose
        idle never comes fails it."""
        for agent, runtime in self.found:
            with self.subTest(agent):
                async def drive(session: c.Session, events: list[c.Event]) -> None:
                    await session.send(c.UserInput(text="hello"))
                    await wait_for_event(events, c.TurnFinished)

                events, _ = await self.run_case(agent, runtime, "never_idle", drive=drive)
                self.assertEqual(event_order_problems(events), [NO_IDLE])

    async def test_unknown_wire_data_is_a_notice(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                events, _ = await self.run_case(agent, runtime, "unknown_wire")
                self.check_order(events)
                self.assertTrue(any(isinstance(e, c.Notice) and e.code == "unknown_wire" for e in events))
                self.assertEqual([e.status for e in events if isinstance(e, c.TurnFinished)], ["completed"])


class TranscriptConformanceTests(unittest.IsolatedAsyncioTestCase):
    """Each runtime's ``read_run`` gives the contract's items (§0.110)."""

    def setUp(self) -> None:
        self.found = runtimes()
        if not self.found:
            self.skipTest("no adapter ships a runtime yet")

    async def test_a_run_reads_as_the_contract_s_items(self) -> None:
        for agent, runtime in self.found:
            with self.subTest(agent):
                self.assertEqual(await run_transcript_problems(BINDINGS[agent], runtime), [])

    async def test_a_runtime_that_hands_back_its_cli_s_records_fails(self) -> None:
        class CliShaped:
            """Delegates to a real runtime, then breaks its items the ways
            §0.110 rules out: the CLI's own records, a CLI's own type."""

            def __init__(self, inner: c.Runtime) -> None:
                self.inner = inner

            async def read_run(self, native_id: str, run_id: str, *, after: str | None = None,
                               limit: int = 50) -> c.TranscriptPage:
                page = await self.inner.read_run(native_id, run_id, after=after, limit=limit)
                items = [dataclasses.asdict(item) if item.type == "input" else
                         dataclasses.replace(item, type="assistant") if item.type == "text" else item
                         for item in page.items]
                return c.TranscriptPage(items=tuple(items), next=page.next)

        for agent, runtime in self.found:
            with self.subTest(agent):
                problems = await run_transcript_problems(BINDINGS[agent], CliShaped(runtime))
                self.assertTrue(any("not a TranscriptItem" in p for p in problems), problems)
                self.assertTrue(any("'assistant', not one of the contract's" in p for p in problems), problems)
                self.assertIn("the binding's run has no input, text item", problems)

    async def test_a_runtime_that_misreports_a_missing_run_fails(self) -> None:
        class WrongMissing:
            """Delegates, but a run it doesn't know is a KeyError (core: a 500), and
            any cursor a timeout."""

            def __init__(self, inner: c.Runtime) -> None:
                self.inner = inner

            async def read_run(self, native_id: str, run_id: str, *, after: str | None = None,
                               limit: int = 50) -> c.TranscriptPage:
                if run_id == MISSING_RUN:
                    raise KeyError(run_id)
                if after == FOREIGN_CURSOR:
                    raise c.RuntimeFailure("timeout", "slow")
                return await self.inner.read_run(native_id, run_id, after=after, limit=limit)

        for agent, runtime in self.found:
            with self.subTest(agent):
                problems = await run_transcript_problems(BINDINGS[agent], WrongMissing(runtime))
                self.assertEqual(problems, [
                    "a cursor no page gave failed 'timeout', not 'protocol'",
                    "a run the CLI keeps no transcript of raised KeyError, not RuntimeFailure('session_not_found')"])


class TranscriptProblemsTests(unittest.TestCase):
    def test_a_conforming_run(self) -> None:
        items = [
            c.TranscriptItem(id="1", type="input", ts="2026-10-15T12:00:00Z", text="find it"),
            c.TranscriptItem(id="2", type="thinking", steps=("Look in src.",), duration_ms=900),
            c.TranscriptItem(id="3", type="tool_call", call_id="k", tool="TodoWrite", kind="plan", title="Plan",
                             plan=(c.PlanItem(text="t", state="todo"),)),
            c.TranscriptItem(id="4", type="tool_result", call_id="k", status="ok", text="ok"),
            c.TranscriptItem(id="5", type="tool_result", call_id="k", status="error", text="cut", bytes=9_000,
                             truncated=True),
            c.TranscriptItem(id="6", type="other", text="image"),
            c.TranscriptItem(id="7", type="error", text="You've hit your limit"),
            c.TranscriptItem(id="8", type="text", text="found"),
        ]
        self.assertEqual(transcript_problems(items), [])

    def test_each_kind_of_nonconformance_is_named(self) -> None:
        items = [
            {"id": "0", "type": "user", "message": {"content": "hi"}},
            c.TranscriptItem(id="", type="input"),
            c.TranscriptItem(id="2", type="assistant"),
            c.TranscriptItem(id="3", type="text", ts="2026-10-15T12:00:00.250Z", call_id="k"),
            c.TranscriptItem(id="3", type="tool_call", tool="Bash", kind="shell", title="Run"),
            c.TranscriptItem(id="5", type="tool_call", call_id="k", tool="Bash", kind="command", title="Run",
                             plan=(c.PlanItem(text="t", state="todo"),)),
            c.TranscriptItem(id="6", type="tool_result", call_id="nope", status="failed", text="out"),
            c.TranscriptItem(id="7", type="tool_result", call_id="k", status="ok", text="out", truncated=True),
            c.TranscriptItem(id="8", type="tool_result", call_id="k", status="ok", text="out", bytes=999),
            c.TranscriptItem(id="9", type="other"),
            c.TranscriptItem(id="10", type="thinking", steps=("Look", 2)),
            c.TranscriptItem(id="11", type="tool_result", call_id="k", status="ok", text="out", bytes=1),
            c.TranscriptItem(id="12", type="tool_result", call_id="k", status="ok", text="out", bytes="9"),
            c.TranscriptItem(id="13", type="thinking", duration_ms=True),
            c.TranscriptItem(id="14", type="tool_call", call_id="p", tool="TodoWrite", kind="plan", title="Plan",
                             plan=({"content": "a", "status": "in_progress"},)),
            c.TranscriptItem(id="15", type="tool_call", call_id="p", tool="TodoWrite", kind="plan", title="Plan",
                             plan=(c.PlanItem(text="a", state="in_progress"),)),
            c.TranscriptItem(id="16", type="tool_call", call_id="b", tool="Bash", kind="command",
                             title={"command": "ls"}),
            c.TranscriptItem(id="17", type="error"),
        ]
        self.assertEqual(transcript_problems(items), [
            "item 0 is a dict, not a TranscriptItem",
            "item 1 ('') has no id",
            "item 2 ('2') has type 'assistant', not one of the contract's",
            "item 3 ('3') has ts '2026-10-15T12:00:00.250Z', not a UTC second ending in Z",
            "item 3 ('3') is text but sets call_id",
            "item 4 ('3') repeats an id",
            "item 4 ('3') is tool_call without call_id",
            "item 4 ('3') has kind 'shell', not a ToolKind",
            "item 5 ('5') carries a plan on a call of kind 'command'",
            "item 6 ('6') has status 'failed', not one of ok, error, denied, cancelled",
            "item 6 ('6') answers call 'nope', which no earlier item made",
            "item 7 ('7') says truncated=True with bytes=None for a 3-byte text",
            "item 8 ('8') says truncated=False with bytes=999 for a 3-byte text",
            "item 9 ('9') is other without text",
            "item 10 ('10') has steps ('Look', 2), not a tuple of str",
            "item 11 ('11') has bytes=1, less than its 3-byte text",
            "item 12 ('12') has bytes '9', not a whole number or None",
            "item 13 ('13') has duration_ms True, not a whole number or None",
            "item 14 ('14') has plan ({'content': 'a', 'status': 'in_progress'},), not a tuple of PlanItem",
            "item 15 ('15') has plan (PlanItem(text='a', state='in_progress'),), not a tuple of PlanItem",
            "item 16 ('16') has title {'command': 'ls'}, not a str or None",
            "item 17 ('17') is error without text",
        ])

    def test_every_field_of_the_item_is_type_checked(self) -> None:
        self.assertEqual(set(FIELD_TYPES), {f.name for f in fields(c.TranscriptItem)})


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
            self.ev(c.ToolInputDelta, 3, turn_id="t1", call_id="k", bytes_so_far=10),
            self.ev(c.ToolStarted, 4, turn_id="t1", call_id="k", tool="Write", kind="create_file", title="a",
                    target="a"),
            self.ev(c.ToolFinished, 5, turn_id="t1", call_id="k", status="ok"),
            self.ev(c.TurnFinished, 6, turn_id="t1", status="completed"),
            self.ev(c.SessionIdle, 7),
            self.ev(c.SessionClosed, 8, reason="closed"),
        ]
        self.assertEqual(event_order_problems(events), [])
        self.assertEqual(event_order_problems([*events[:6], events[7]]), [NO_IDLE])

    def test_each_kind_of_disorder_is_named(self) -> None:
        events = [
            self.ev(c.TurnFinished, 2, turn_id="t0", status="completed"),
            self.ev(c.TurnStarted, 2, turn_id="t1", origin="person"),
            self.ev(c.ToolStarted, 3, turn_id="t1", call_id="k", tool="x", kind="other", title="x", target=None),
            self.ev(c.ToolFinished, 4, call_id="z", status="ok"),
            self.ev(c.ToolInputDelta, 5, call_id="z", bytes_so_far=3),
            self.ev(c.SessionClosed, 6, reason="closed"),
            self.ev(c.Notice, 7, code="late", message="after close"),
        ]
        self.assertEqual(event_order_problems(events), [
            "turn t0 finished without starting",
            "TurnStarted seq 2 after 2",
            "tool z finished without starting",
            "tool z input after it finished",
            "Notice after SessionClosed",
            NO_IDLE,
            "turn t1 never finished",
            "tool k never finished",
        ])


if __name__ == "__main__":
    unittest.main()
