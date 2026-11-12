"""The goal keeper (the plan's W6): while a goal is before its end time and
nothing is running, queued or waiting on a timer for its chat, it wakes the
chat's coordinator once with the goal's state. If that wake's input ends
with nothing new, the goal is ``stalled`` and the founder is told once, as a
system message in the chat. It can't loop: one wake per stretch without
progress, and the ``keeper.wake`` event that names the progress it saw is in
the store, so a restart never repeats it.

A goal is ``paused`` with the reason while the founder holds its scope, its
account's limit is full or no coordinator can start, and goes on when that
lifts; past its end time it is ``expired``. The keeper's wake takes the one
wake gate (``Writes.wake``): it never lifts the founder's choice or an
account's hold.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Sequence

from services.cowork_agent.storm.chats import envelope, rows
from services.cowork_agent.storm.engine import Wake
from services.cowork_agent.storm.engine.admission import HOLDS
from services.cowork_agent.storm.records import goals, intents, ledger
from services.errors import ServiceError
from services.timestamps import parse_ts

log = logging.getLogger(__name__)
#: How long the keeper waits after a pass failed.
RETRY_S = 10
#: The longest it sleeps without looking again; every commit that can move a goal wakes it sooner.
MAX_SLEEP_S = 60
#: The commits after which a goal may have something to do.
_KICKS = frozenset({"chat", "goal", "task", "task.report", "result", "timer", "input.ended", "session", "pool",
                    "intent", "message", "review.decided", "review.withdrawn", "review.expired"})


def progress(conn, chat_id: str) -> int:
    """Changes that can make another attempt useful; keeper bookkeeping cannot."""
    return conn.execute(
        "SELECT coalesce(max(seq), 0) FROM events WHERE chat_id = ? AND ("
        "type IN ('task', 'task.report', 'result') OR "
        "(type = 'timer' AND json_extract(data, '$.action') IN ('arm', 'redefine')) OR "
        "(type = 'message' AND json_extract(data, '$.author_kind') = 'owner'))", (chat_id,)).fetchone()[0]


def busy(conn, chat_id: str) -> bool:
    """Work already has a cause to continue, or waits for the founder."""
    return any(conn.execute(sql, (chat_id,)).fetchone() for sql in (
        "SELECT 1 FROM turns t JOIN sessions s ON s.id = t.session_id WHERE s.chat_id = ? "
        "AND t.status = 'running' LIMIT 1",
        "SELECT 1 FROM wakes k JOIN sessions s ON s.id = k.session_id WHERE s.chat_id = ? "
        "AND s.retired_at IS NULL AND k.state IN ('queued', 'taken') LIMIT 1",
        "SELECT 1 FROM timers WHERE chat_id = ? AND state = 'armed' AND job IS NULL LIMIT 1",   # a wake, not a job
        "SELECT 1 FROM review_items WHERE chat_id = ? AND state = 'open' AND blocking = 1 LIMIT 1"))


def reconcile(w, goal_id: str, session_id: str) -> None:
    """One transaction checks again and records either a hold, stall, expiry or wake."""
    goal = goals.get(w, goal_id)
    if goal is None or goal.state in goals.FINAL:
        return
    now = parse_ts(w.now).timestamp()
    if parse_ts(goal.ends_at).timestamp() <= now:
        goals.move(w, goal, state="expired", reason="goal end time reached", actor=ledger.XO)
        return
    held = intents.session_run(w, session_id, now)
    pool = w.execute("SELECT p.state, p.resets_at FROM pools p JOIN sessions s ON s.pool = p.id WHERE s.id = ?",
                     (session_id,)).fetchone()
    reason = f"founder {held.value}" if held else (f"account {pool[0]}" if pool and pool[0] in HOLDS else None)
    if reason:
        if (goal.state, goal.state_reason) != ("paused", reason):
            goals.move(w, goal, state="paused", reason=reason, actor=ledger.XO)
        return
    latest = w.execute("SELECT data FROM events WHERE type = 'keeper.wake' AND chat_id = ? "
                       "AND json_extract(data, '$.goal_id') = ? ORDER BY seq DESC LIMIT 1",
                       (goal.chat_id, goal.id)).fetchone()
    previous = json.loads(latest[0]) if latest else None
    moved = progress(w, goal.chat_id)
    if goal.state == "paused" or (goal.state == "stalled" and previous and moved > previous["progress"]):
        goal = goals.move(w, goal, state="active", reason=None, actor=ledger.XO)
    if goal.state != "active" or busy(w, goal.chat_id):
        return
    if previous and moved <= previous["progress"]:
        wake = w.execute("SELECT state, turn_id FROM wakes WHERE id = ?", (previous["wake_id"],)).fetchone()
        if wake is not None and wake[0] in ("queued", "taken"):
            return
        if wake is not None and wake[1] is not None and not w.execute(
                "SELECT 1 FROM events e, json_each(e.data, '$.turns') t WHERE e.type = 'input.ended' "
                "AND t.value = ? LIMIT 1", (wake[1],)).fetchone():
            return  # Wait for the reply gate and lifecycle to settle this input.
        goals.move(w, goal, state="stalled", reason="keeper input ended without new work or progress",
                   actor=ledger.XO)
        rows.add_message(w, chat_id=goal.chat_id, author_kind="system", author_id="xo",
                         text=f"Goal stalled: {goal.title}. No work, timer, report or result followed its recovery "
                              "wake. New activity can resume it.", client_id=f"stalled:{previous['wake_id']}")
        return
    wake_id = f"keeper:{goal.id}:{moved}"
    text = envelope.goal_wake(goal, w.now)
    if w.wake(session_id, Wake(id=wake_id, reason="goal", text=text, origin="system")):
        w.append("keeper.wake", {"goal_id": goal.id, "progress": moved, "wake_id": wake_id},
                 chat_id=goal.chat_id, session_id=session_id)


class GoalKeeper:
    """One task, woken by the commits that can move a goal, by the next
    goal's end time, and at least every ``MAX_SLEEP_S``; an engine listener."""

    def __init__(self, chats: Any) -> None:
        self.chats = chats
        self.engine = chats.engine
        self._kicked = asyncio.Event()
        self._sleep: Any = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self.engine.subscribe(self)
        self._kicked.set()
        self._task = asyncio.create_task(self._loop(), name="storm-goal-keeper")

    async def stop(self) -> None:
        self.engine.unsubscribe(self)
        if self._sleep:
            self._sleep.cancel()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def committed(self, events: Sequence[Any]) -> None:
        if any(e.type in _KICKS for e in events):
            self._kicked.set()

    def ephemeral(self, event: Any, epoch: int) -> None:
        pass

    async def _loop(self) -> None:
        while True:
            await self._kicked.wait()
            self._kicked.clear()
            delay = MAX_SLEEP_S
            try:
                delay = await self.sweep()
            except Exception:
                log.exception("storm goal keeper: pass failed; retrying")
                delay = RETRY_S
            if self._sleep:
                self._sleep.cancel()
            self._sleep = self.engine.clock.call_later(max(0.1, delay), self._kicked.set)

    async def sweep(self) -> float:
        """Advance every goal that hasn't ended, each in its own
        transaction; how long until the keeper should look again."""
        marks = ", ".join("?" * len(goals.FINAL))
        pending = await self.engine.store.read(lambda conn: conn.execute(
            f"SELECT id, chat_id, ends_at FROM goals WHERE state NOT IN ({marks})", goals.FINAL).fetchall())
        retry = MAX_SLEEP_S
        for goal_id, chat_id, end in pending:
            try:
                await self._advance(goal_id, chat_id, end)
            except ServiceError as exc:
                def pause(w):
                    goal = goals.get(w, goal_id)
                    reason = f"coordinator unavailable ({exc.code})"
                    if goal and goal.state not in goals.FINAL and (goal.state, goal.state_reason) != ("paused", reason):
                        goals.move(w, goal, state="paused", reason=reason, actor=ledger.XO)
                await self.engine.write(pause)       # what lifts it commits an event, or the next look finds it
            except Exception:
                # An unavailable coordinator must not prevent another goal's
                # expiry or recovery. Each goal is an independent transaction.
                log.exception("storm goal keeper: goal %s could not advance", goal_id)
                retry = RETRY_S
        future = [parse_ts(end).timestamp() - self.engine.clock.time() for _, _, end in pending]
        return min([retry, *(wait for wait in future if wait > 0)])

    async def _advance(self, goal_id: str, chat_id: str, end: str) -> None:
        """Cancel a removed project's goal, expire one past its end, else
        ``reconcile`` it with the chat's coordinator (started if it has none)."""
        chat = await self.chats.chat(chat_id)
        if chat.get("retired_at"):
            def cancel(w):
                goal = goals.get(w, goal_id)
                if goal and goal.state not in goals.FINAL:
                    goals.move(w, goal, state="cancelled", reason="project removed", actor=ledger.XO)
            await self.engine.write(cancel)
            return
        # Expiry does not require creating a coordinator (a stopped scope may refuse it).
        if parse_ts(end).timestamp() <= self.engine.clock.time():
            def expire(w):
                goal = goals.get(w, goal_id)
                if goal and goal.state not in goals.FINAL:
                    goals.move(w, goal, state="expired", reason="goal end time reached", actor=ledger.XO)
            await self.engine.write(expire)
            return
        sid = await self.engine.store.read(lambda conn: conn.execute(
            "SELECT id FROM sessions WHERE chat_id = ? AND role LIKE '%-coordinator' "
            "AND retired_at IS NULL LIMIT 1", (chat_id,)).fetchone())
        if sid:
            await self.engine.write(lambda w: reconcile(w, goal_id, sid[0]))
        else:
            await self.chats.with_coordinator(chat, lambda w, session: reconcile(w, goal_id, session))
