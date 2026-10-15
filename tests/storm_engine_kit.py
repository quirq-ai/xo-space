"""A storm engine on the fake claude CLI, in a temp state root, home and
projects root, with a manual clock (no real sleeps for timers) and a memory
reader the test controls. Shared by the engine and route tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine.memory import MemoryGuard, MemoryReading
from services.cowork_agent.storm.engine.model import AlreadyDecided, Ask
from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.store import StoredEvent
from tests.fakes import claude, install, scenario
from utils.commands import live_processes

AGENT = "claude_code"
SID = "native-1"


class ManualClock:
    """``ports.Clock`` that moves only when the test says so."""

    def __init__(self) -> None:
        import time
        self.base = time.time()
        self.offset = 0.0
        self._timers: list[list] = []

    def time(self) -> float:
        return self.base + self.offset

    def monotonic(self) -> float:
        return 1_000.0 + self.offset

    def call_later(self, delay: float, callback: Callable[[], None]):
        entry = [self.offset + max(0.0, delay), callback, False]
        self._timers.append(entry)

        class Handle:
            def cancel(_self) -> None:
                entry[2] = True
        return Handle()

    def advance(self, seconds: float) -> None:
        self.offset += seconds
        due = sorted((t for t in self._timers if t[0] <= self.offset and not t[2]), key=lambda t: t[0])
        self._timers = [t for t in self._timers if t not in due and not t[2]]
        for _, callback, _ in due:
            callback()

    def pending(self) -> int:
        return sum(1 for t in self._timers if not t[2])


class Recorder:
    """A ``ports.Listener`` that keeps what it hears and lets a test wait for
    an event type."""

    def __init__(self) -> None:
        self.committed_events: list[StoredEvent] = []
        self.ephemerals: list[c.Event] = []
        self.epochs: list[int] = []
        self.order: list[str] = []
        self._changed = asyncio.Event()

    def committed(self, events) -> None:
        self.committed_events.extend(events)
        self.order.extend(e.type for e in events)
        self._changed.set()

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        self.ephemerals.append(event)
        self.epochs.append(epoch)
        self.order.append(type(event).__name__)
        self._changed.set()

    def types(self, session_id: str | None = None) -> list[str]:
        return [e.type for e in self.committed_events if session_id is None or e.session_id == session_id]

    def of(self, type_: str, session_id: str | None = None) -> list[StoredEvent]:
        return [e for e in self.committed_events if e.type == type_
                and (session_id is None or e.session_id == session_id)]

    async def wait(self, predicate: Callable[[], bool], timeout: float = 15.0) -> None:
        async def loop() -> None:
            while not predicate():
                self._changed.clear()
                await self._changed.wait()
        await asyncio.wait_for(loop(), timeout)

    async def seen(self, type_: str, *, count: int = 1, session_id: str | None = None, **data: Any) -> StoredEvent:
        def match() -> list[StoredEvent]:
            return [e for e in self.of(type_, session_id) if all(e.data.get(k) == v for k, v in data.items())]
        await self.wait(lambda: len(match()) >= count)
        return match()[count - 1]


@dataclass
class Desk:
    """A ``ReviewDesk`` test double: items are rows in the store, as PR 9's
    will be, so recovery and the engine see them."""

    opened: list[tuple[str, Ask]] = field(default_factory=list)
    parked: list[tuple[str, str]] = field(default_factory=list)
    withdrawn: list[tuple[str, str]] = field(default_factory=list)
    superseded: list[tuple[str, str]] = field(default_factory=list)       # (old, by)
    on_open: Callable[[str, Ask], None] | None = None
    expires_at: str | None = None           # what PR 9 sets (7 days); tests set it to see the hold end
    loop: asyncio.AbstractEventLoop | None = None

    def open(self, w: Writes, ask: Ask) -> str:
        item = f"rv_{len(self.opened) + 1:012d}"
        native = json.dumps({"request_id": (ask.request or ask.question).request_id}) \
            if (ask.request or ask.question) else None
        detail = json.dumps({"input_sha256": ask.call.input_sha256 if ask.call else None,
                             "call_kind": ask.call.kind if ask.call else None})
        seq = w.append("review.opened", {"item": item, "kind": ask.kind}, session_id=ask.session_id)
        w.execute("INSERT INTO review_items (id, seq, session_id, turn_id, epoch, origin, kind, title, state, native, "
                  "detail, created_at, expires_at) VALUES (?, ?, ?, ?, ?, 'cli', ?, ?, 'open', ?, ?, ?, ?)",
                  (item, seq, ask.session_id, ask.turn_id, ask.epoch, ask.kind, ask.title, native, detail, w.now,
                   self.expires_at))
        self.opened.append((item, ask))
        if self.on_open:            # open runs on the store's thread
            self.loop.call_soon_threadsafe(self.on_open, item, ask)
        return item

    def park(self, w: Writes, item_id: str, reason: str) -> None:
        w.execute("UPDATE review_items SET parked = 1 WHERE id = ?", (item_id,))
        self.parked.append((item_id, reason))

    def withdraw(self, w: Writes, item_id: str, reason: str) -> None:
        w.execute("UPDATE review_items SET state = 'withdrawn' WHERE id = ?", (item_id,))
        self.withdrawn.append((item_id, reason))

    def supersede(self, w: Writes, session_id: str, *, by: str, call=None) -> list[str]:
        """What PR 9's queue does: withdraw the open parked items of the
        session (of the same call, when one is given)."""
        rows = w.execute("SELECT id, json_extract(detail, '$.input_sha256'), json_extract(detail, '$.call_kind') "
                         "FROM review_items WHERE session_id = ? AND state = 'open' AND parked = 1 AND id <> ?",
                         (session_id, by)).fetchall()
        old = [item for item, sha, kind in rows
               if call is None or (sha == call.input_sha256 and kind == call.kind)]
        for item in old:
            w.execute("UPDATE review_items SET state = 'withdrawn' WHERE id = ?", (item,))
            w.append("review.withdrawn", {"item": item, "reason": "superseded", "by": by}, session_id=session_id)
            self.superseded.append((item, by))
        return old

    def record(self, item_id: str, decision: str):
        """What PR 9's decision route writes in the engine's transaction:
        compare-and-set on the item being open."""
        def write(w: Writes, delivery: str) -> int:
            if w.execute("UPDATE review_items SET state = 'decided', decision = json(?), delivery = ? "
                         "WHERE id = ? AND state = 'open'",
                         (json.dumps({"decision": decision}), delivery, item_id)).rowcount != 1:
                raise AlreadyDecided(item_id)
            return w.append("review.decided", {"item": item_id, "delivery": delivery})
        return write


class Kit:
    """Boot an engine on a fake claude scenario."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.state = self.root / "state"
        self.projects = self.root / "projects"
        self.work = self.projects / "demo"
        self.work.mkdir(parents=True)
        self.clock = ManualClock()
        self.recorder = Recorder()
        self.reading = MemoryReading()
        self.engine: storm_engine.Engine | None = None
        self.fake = None
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "QUIRQ_STATE_ROOT": str(self.state),
                                                "XO_PROJECTS_ROOT": str(self.projects)})
        self.env.start()

    def scenario(self, steps: list[dict], *, argv: list[dict] | None = None) -> None:
        """Install the fake; it answers the model-free probes (``describe()``:
        the version and the login) unless ``argv`` says otherwise."""
        answers = argv if argv is not None else [claude.version(), claude.auth_status()]
        self.fake = install("claude", self.root / "bin", scenario(steps, argv=answers))
        os.environ["CLAUDE_CLI_PATH"] = str(self.fake.path)

    def runs(self) -> list[dict]:
        """The fake's session processes (not the model-free probes, which
        have no per-session settings file)."""
        return [run for run in self.fake.invocations() if "--settings" in run["argv"]]

    async def boot(self, *, desk: Any = None, listeners: Sequence[Any] = ()) -> storm_engine.Engine:
        runtime = ClaudeRuntime(claude_home=self.home / ".claude")
        return await self.boot_with(runtime, desk=desk, listeners=listeners)

    async def boot_with(self, runtime: Any, *, desk: Any = None, listeners: Sequence[Any] = ()) -> storm_engine.Engine:
        """``listeners``: more ``ports.Listener``s beside the recorder (the stream's bus)."""
        self.engine = await storm_engine.boot(
            runtimes=lambda agent: runtime if agent == AGENT else None, providers=[AGENT], clock=self.clock,
            desk=desk, memory=MemoryGuard(reader=lambda: self.reading), listeners=[self.recorder, *listeners])
        assert self.engine is not None, "the engine did not start"
        return self.engine

    async def session(self, **spec: Any) -> str:
        spec.setdefault("permissions", c.PermissionPolicy(mode="ask"))
        return await self.engine.create_session(c.SessionSpec(cwd=str(self.work), **spec), agent=AGENT,
                                                role="plain", preset="plain_chat")

    async def until(self, predicate: Callable[[], bool], timeout: float = 15.0) -> None:
        """Wait for something the engine does after a commit (a timer it
        sets): the commit's event can reach the test first."""
        async def loop() -> None:
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(loop(), timeout)

    def pace(self) -> None:
        """Let spawn pacing (about two new processes a second) start the next
        process."""
        self.clock.advance(0.5)

    @contextlib.asynccontextmanager
    async def pacing(self, on_tick: Callable[[], None] | None = None):
        """Let time pass for spawn pacing while the block runs."""
        async def pace() -> None:
            while True:
                if on_tick is not None:
                    on_tick()
                self.pace()
                await asyncio.sleep(0.02)
        task = asyncio.create_task(pace())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def idle_timer(self) -> None:
        await self.until(lambda: any(key.startswith("cse_") for key in self.engine._timers))

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Change the store behind the engine's back (only while it is down)."""
        conn = sqlite3.connect(self.state / "engine" / "xo.db")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = sqlite3.connect(self.state / "engine" / "xo.db")
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    async def close(self) -> None:
        """Shut the engine down and check it left nothing running: no process
        of this server and no row in the process registry."""
        await storm_engine.shutdown()
        left = live_processes()
        registered = self.rows("SELECT pid FROM procs") if (self.state / "engine" / "xo.db").exists() else []
        for proc in left:                   # by pid only, never by name
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        self.env.stop()
        os.environ.pop("CLAUDE_CLI_PATH", None)
        self.tmp.cleanup()
        assert not left, f"processes left running: {[(p.pid, p.label) for p in left]}"
        assert not registered, f"process registry rows left: {registered}"


def message(n: int, text: str = "hello", **kw: Any) -> storm_engine.Wake:
    return storm_engine.Wake(id=f"msg:cmsg_{n}", text=text, priority="now", reason="message", origin="person", **kw)
