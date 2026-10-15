"""Conformance: every runtime, run against its fake CLI, behaves as its
``check()`` and feature list say (storm ch 01 §7, ch 06 §2).

The suite finds runtimes through the capability loader
(``adapters/<agent>/runtime/``), so a runtime joins it by existing. Today it
checks that each runtime's feature list names every contract feature
(unsupported ones too) and that ``check()`` starts nothing.
``event_order_problems`` is the order check the turn cases will use.

Every runtime must have ``BINDINGS[agent]``: the fake CLI, the env variable
the runtime reads its binary from, and a scenario per case. A runtime with no
binding fails here rather than passing unchecked. The cases that drive a
session through its fake (event order, coordinator writes blocked,
``interrupt``, ``close`` leaving no process, unknown wire data) arrive with
the first runtime, in its PR.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, fields
from typing import Callable

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.registry.adapter_registry import list_adapters
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


#: Filled in by each runtime's PR (claude_code in B2, codex and antigravity in B4).
BINDINGS: dict[str, Binding] = {}


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
