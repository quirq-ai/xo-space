"""Reading the state log back as frames (design ch 09 §4, §6): a
reconnect's replay and ``GET /api/chat/events``.

Pages are filtered by chat and session in SQL. A turn the replay meets
mid-way (its start is before the first page that folds it) is first folded
from its own earlier events, one query for every such turn on a page, so its
blocks get the same ids, order, content and places as they did live. A turn
counts as met only through an event the presenter folds: one met first
through anything else (a ``turn.stopped``, a ``run.finished``) is folded
when its next folded event comes, however the pages fall.
"""

from __future__ import annotations

import sqlite3

from services.cowork_agent.storm.store import Store, StoredEvent
from services.cowork_agent.storm.store.db import EVENT_COLUMNS, event_from_row
from services.cowork_agent.storm.stream.frames import Filter, Frame, Point
from services.cowork_agent.storm.stream.presenter import TURN_EVENTS
from services.cowork_agent.storm.stream.turns import Turns
from services.timestamps import parse_ts

#: Events read per page (ch 17 §1: lists take 1..500).
PAGE_LIMIT = 500
#: How long the shortest-lived stored events are kept (ch 08 §6: ``step.*``,
#: ``progress.*``, ``run.*``, 30 days). A resume point older than this may
#: have lost events, so the client reloads instead (ch 09 §4).
RETENTION_S = 30 * 24 * 3600


async def read_page(store: Store, filter: Filter, *, after: int = 0, before: int | None = None,
                    limit: int = PAGE_LIMIT, newest: bool = False) -> list[StoredEvent]:
    """Stored events after ``after`` (and before ``before``) that the
    filter's chat and session allow, in seq order. ``newest``: the last
    ``limit`` of them rather than the first."""
    def query(conn: sqlite3.Connection) -> list[StoredEvent]:
        rows = conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE seq > :after AND (:before IS NULL OR seq < :before) "
            "AND (:chat IS NULL OR chat_id = :chat) AND (:session IS NULL OR session_id = :session) "
            f"ORDER BY seq {'DESC' if newest else 'ASC'} LIMIT :limit",
            {"after": after, "before": before, "chat": filter.chat, "session": filter.session,
             "limit": limit}).fetchall()
        events = [event_from_row(r) for r in rows]
        return events[::-1] if newest else events
    return await store.read(query)


async def stale_point(store: Store, seq: int, *, now: float) -> tuple[str, int] | None:
    """Why a resume after ``seq`` can't be served, and the store's head to
    resume from once the client has reloaded: ``ahead`` (a point this store
    never reached: it was replaced) or ``retention`` (older than what is
    kept). None when the store has everything after it."""
    head = await store.head()
    if seq > head:
        return "ahead", head
    if seq == head:
        return None
    ts = await store.read(lambda conn: (
        conn.execute("SELECT ts FROM events WHERE seq <= ? ORDER BY seq DESC LIMIT 1", (seq,)).fetchone()
        or conn.execute("SELECT ts FROM events ORDER BY seq LIMIT 1").fetchone()))
    if ts is not None and parse_ts(ts[0]).timestamp() < now - RETENTION_S:
        return "retention", head
    return None


class Replay:
    """Frames for pages of stored events, read in order."""

    def __init__(self, store: Store, filter: Filter) -> None:
        self._store = store
        self._filter = filter
        self._turns = Turns()
        self._met: set[str] = set()

    async def frames(self, events: list[StoredEvent], *, after: Point | None = None) -> list[Frame]:
        """The page's frames the filter wants, less those ``after`` covers."""
        await self._fold_earlier(events)
        frames = [f for event in events for f in self._turns.apply(event)]
        return [f for f in frames if self._filter.matches(f) and (after is None or not after.covers(f))]

    async def _fold_earlier(self, events: list[StoredEvent]) -> None:
        """Fold, silently, the earlier events of every turn this page meets
        mid-way."""
        met = {e.turn_id for e in events if e.turn_id and e.type in TURN_EVENTS}
        mid_way = met - self._met - {e.turn_id for e in events if e.type == "turn.started"}
        self._met |= met
        if not mid_way:
            return
        first, turns = events[0].seq, sorted(mid_way)

        def query(conn: sqlite3.Connection) -> list[StoredEvent]:
            marks = ", ".join("?" * len(turns))
            rows = conn.execute(f"SELECT {EVENT_COLUMNS} FROM events WHERE turn_id IN ({marks}) AND seq < ? "
                                "ORDER BY seq", (*turns, first)).fetchall()
            return [event_from_row(r) for r in rows]
        for event in await self._store.read(query):
            self._turns.apply(event)
