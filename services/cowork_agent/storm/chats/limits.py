"""The usage-limit card (mixed-runtime plan, step 8): when a runtime's pool
closes on its limit while threads wait on it, XO asks the owner once per
pool, not once per thread: wait for the reset, or continue those threads on
another runtime.

The card is a ``usage`` review item (one open per pool), offered only when
some other runtime can take the work (allowed by the waiting threads'
projects and usable now); otherwise each waiting thread already says until
when it waits, and there is nothing to choose. ``move`` continues each
thread still waiting through ``chats.continuation`` (the owner decided), and
a thread that can't be moved says why on itself.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Mapping, Sequence

from services.cowork_agent.storm.chats import continuation, rows
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.engine import pool_agent
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.store import StoredEvent
from services.errors import ServiceError

log = logging.getLogger(__name__)

MOVE = "move"
WAIT = "wait"


def waiting_threads(conn, pool_id: str) -> list[dict[str, Any]]:
    """The live threads whose session runs in ``pool_id`` and has input
    waiting to run."""
    found = conn.execute(
        "SELECT t.chat_id, t.ts, t.title FROM threads t JOIN sessions s ON s.id = t.session_id "
        "WHERE s.pool = ? AND s.retired_at IS NULL AND s.paused IS NULL AND t.resolved_at IS NULL "
        "AND EXISTS (SELECT 1 FROM wakes k WHERE k.session_id = s.id AND k.state = 'queued') ORDER BY t.rowid",
        (pool_id,)).fetchall()
    return [{"chat_id": c, "ts": ts, "title": title} for c, ts, title in found]


def open_card(conn, pool_id: str) -> str | None:
    row = conn.execute("SELECT id FROM review_items WHERE kind = 'usage' AND state = 'open' "
                       "AND json_extract(detail, '$.pool') = ? AND json_extract(detail, '$.limit') = 'closed'",
                       (pool_id,)).fetchone()
    return row[0] if row else None


class LimitCards:
    """An engine listener: a pool closing opens the card; the owner's
    ``move`` decision is carried out by ``moved``."""

    def __init__(self, chats: Any) -> None:
        self.chats = chats

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.type == "pool" and event.data.get("state") == "closed":
                self.chats.spawn(self.closed(event.data["pool"], event.data.get("resets_at")),
                                 f"limit-card:{event.data['pool']}")

    def ephemeral(self, event: Any, epoch: int) -> None:
        return None

    async def target(self, chat_ids: set[str], blocked: str) -> str | None:
        """A runtime other than ``blocked`` that every waiting thread's
        project allows and that can take work now."""
        health = await self.chats.runtime_health()
        policies = [await self.chats.runtime_policy(await self.chats.chat(c)) for c in chat_ids]
        for h in health:
            if h.agent != blocked and h.usable and all(p.permits(h.agent) for p in policies):
                return h.agent
        return None

    async def closed(self, pool_id: str, resets_at: str | None) -> None:
        engine = self.chats.engine
        waiting = await engine.store.read(lambda conn: waiting_threads(conn, pool_id))
        if not waiting or await engine.store.read(lambda conn: open_card(conn, pool_id)):
            return
        blocked = pool_agent(pool_id)
        target = await self.target({t["chat_id"] for t in waiting}, blocked)
        if target is None:
            log.info("storm limits: %s is closed with %d thread(s) waiting, and no other runtime can take them",
                     pool_id, len(waiting))
            return
        until = f" until {resets_at}" if resets_at else ""
        count = f"{len(waiting)} thread{'s' if len(waiting) != 1 else ''}"
        spec = ItemSpec(
            kind="usage", origin="xo", blocking=False,
            title=f"{count} {'is' if len(waiting) == 1 else 'are'} waiting on {blocked}{until}: it reached its "
                  "usage limit.",
            why=(f"Waiting keeps each thread and its memory. Moving continues each one in a new thread on {target}: "
                 f"XO stops and freezes it, commits its work, and the new thread starts from a summary plus the "
                 f"code, so it re-reads files. The threads' content goes to {target}'s provider."),
            detail={"pool": pool_id, "resets_at": resets_at, "limit": "closed", "move_to": target,
                    "threads": waiting},
            options=[{"id": WAIT, "decision": WAIT, "label": f"Wait{until}"},
                     {"id": MOVE, "decision": MOVE, "label": f"Continue them on {target}"}])

        def open_(w: Writes) -> None:
            if open_card(w, pool_id) is None:
                self.chats.queue.open_item(w, spec)
        await engine.write(open_)

    async def moved(self, item: Mapping[str, Any]) -> None:
        """The owner chose ``move``: continue each thread still waiting."""
        detail = item["detail"]
        target, pool_id = detail["move_to"], detail["pool"]
        still = {(t["chat_id"], t["ts"]) for t in await self.chats.engine.store.read(
            lambda conn: waiting_threads(conn, pool_id))}
        reason = f"{pool_agent(pool_id)} reached its usage limit" + (
            f" until {detail['resets_at']}" if detail.get("resets_at") else "")
        for thread in detail.get("threads") or ():
            if (thread["chat_id"], thread["ts"]) not in still:
                continue
            try:
                chat = await self.chats.chat(thread["chat_id"])
                await continuation.continue_thread(self.chats, chat, thread["ts"], agent=target, reason=reason,
                                                   by="owner")
            except ServiceError as exc:
                log.warning("storm limits: thread %s wasn't moved (%s): %s", thread["ts"], exc.code, exc.message)
                await self.chats.engine.write(partial(_say, thread, f"Moving this thread to {target} failed: "
                                                                    f"{exc.message}"))


def _say(thread: Mapping[str, Any], text: str, w: Writes) -> None:
    rows.add_message(w, chat_id=thread["chat_id"], thread_ts=thread["ts"], author_kind="system", author_id="xo",
                     text=text)
