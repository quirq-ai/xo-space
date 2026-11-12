"""Keep a bounded goal moving, with one recovery wake per period without progress.

The durable keeper.wake event names the progress sequence it observed.
After that input ends, no new work means stalled, with one system message.
Restarting never resets that decision. The keeper uses the normal wake
gate and never lifts founder intent or an account hold.
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
from services.timestamps import parse_ts

log = logging.getLogger(__name__)
RETRY_S = 10
MAX_SLEEP_S = 60


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
        "SELECT 1 FROM timers WHERE chat_id = ? AND state = 'armed' LIMIT 1",
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
    def __init__(self, chats: Any) -> None:
        self.chats = chats
        self.engine = chats.engine
        self._kicked = asyncio.Event()
        self._sleep = None
        self._task = None

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
        if any(e.type in ("goal", "task", "task.report", "result", "timer", "input.ended", "session", "pool",
                          "intent", "message", "review.closed") for e in events):
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
        pending = await self.engine.store.read(lambda conn: conn.execute(
            "SELECT id, chat_id, ends_at FROM goals WHERE state NOT IN ('done', 'failed', 'cancelled', 'expired')"
        ).fetchall())
        for goal_id, chat_id, end in pending:
            # Expiry does not require creating a coordinator (a stopped scope may refuse it).
            if parse_ts(end).timestamp() <= self.engine.clock.time():
                def expire(w):
                    goal = goals.get(w, goal_id)
                    if goal and goal.state not in goals.FINAL:
                        goals.move(w, goal, state="expired", reason="goal end time reached", actor=ledger.XO)
                await self.engine.write(expire)
                continue
            sid = await self.engine.store.read(lambda conn: conn.execute(
                "SELECT id FROM sessions WHERE chat_id = ? AND role LIKE '%-coordinator' "
                "AND retired_at IS NULL LIMIT 1", (chat_id,)).fetchone())
            if sid:
                await self.engine.write(lambda w: reconcile(w, goal_id, sid[0]))
            else:
                await self.chats.with_coordinator(await self.chats.chat(chat_id),
                                                   lambda w, session: reconcile(w, goal_id, session))
        future = [parse_ts(end).timestamp() - self.engine.clock.time() for _, _, end in pending]
        return min([MAX_SLEEP_S, *(wait for wait in future if wait > 0)])
