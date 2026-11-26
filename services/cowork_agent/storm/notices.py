"""Space advisory notices (plan §9): a crossed memory level becomes a workspace message and a coordinator wake."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Sequence

from services.advisory import Snapshot
from services.cowork_agent.storm.chats import envelope, rows
from services.cowork_agent.storm.engine import Wake, Writes
from services.errors import ServiceError

log = logging.getLogger(__name__)

#: The shares of the memory limit that send a notice, highest first.
LEVELS = (0.95, 0.90)
#: Under this share the next crossing is noticed again.
REARM = 0.85
#: The notice's author (``author_kind`` "system"), shown as "Space advisory".
AUTHOR = "space-advisory"
#: How many of the largest users a notice names.
TOP = 3
GIB = 1 << 30


def crossing(share: float, noticed: float | None) -> tuple[float | None, float | None]:
    """The level now noticed and the one to notify (None: nothing new) for ``share`` of the limit."""
    if share < REARM:
        return None, None
    reached = next((level for level in LEVELS if share >= level), None)
    if reached is not None and (noticed is None or reached > noticed):
        return reached, reached
    return noticed, None


def users(memory: Mapping[str, int], places: Mapping[str, tuple[str | None, str | None, str | None]]
          ) -> list[tuple[str, int, int]]:
    """``(who, bytes, threads)`` of the largest users, by project, from each session's memory and place."""
    found: dict[str, list[int]] = {}
    for session_id, used in memory.items():
        kind, project, thread_ts = places.get(session_id, (None, None, None))
        if kind == rows.PROJECT:
            who = project
        elif kind == rows.WORKSPACE:
            who = "the workspace coordinator"
        else:
            who = "other sessions"
        total = found.setdefault(who, [0, 0])
        total[0] += used
        total[1] += thread_ts is not None
    return sorted(((who, used, threads) for who, (used, threads) in found.items()), key=lambda u: -u[1])[:TOP]


def text(used: int, limit: int, largest: Sequence[tuple[str, int, int]], waiting: int) -> str:
    """The notice's words: the level, the largest users, and whether new sessions are held."""
    def size(n: int) -> str:
        return f"{n / GIB:.1f}".removesuffix(".0") + " GB"
    named = "; ".join(f"{who} {size(n)}" + (f" across {t} thread{'s' * (t != 1)}" if t else "")
                      for who, n, t in largest)
    if waiting == 0:
        held = "No new session is held yet."
    else:
        held = f"{waiting} new session{' is' if waiting == 1 else 's are'} held until memory frees up."
    largest_users = f" Largest users: {named}." if named else ""
    return f"Memory at {used * 100 // limit}% of {size(limit)}.{largest_users} {held}"


def _places(conn, session_ids: list[str]) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Each session's chat kind, project and thread."""
    if not session_ids:
        return {}
    marks = ", ".join("?" * len(session_ids))
    return {r[0]: r[1:] for r in conn.execute(
        f"SELECT s.id, c.kind, c.project_name, s.thread_ts FROM sessions s LEFT JOIN chats c ON c.id = s.chat_id "
        f"WHERE s.id IN ({marks})", session_ids)}


class MemoryNotices:
    """The advisory's listener that turns a crossed memory level into a notice."""

    def __init__(self, chats: Any) -> None:
        self.chats = chats
        self._noticed: float | None = None
        self._unwatch = None

    def start(self) -> None:
        self._unwatch = self.chats.engine.advisory.on_change(lambda snapshot, _accounts: self.observe(snapshot))

    def stop(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None

    def observe(self, snapshot: Snapshot) -> asyncio.Task | None:
        """Send a notice when ``snapshot`` crosses a level; the task sending it, else None."""
        now = self.chats.engine.advisory.now()
        used, limit = snapshot.machine.memory_working_set.at(now), snapshot.machine.memory_limit.at(now)
        if not (used.known and limit.known and limit.value):
            return None
        self._noticed, level = crossing(used.value / limit.value, self._noticed)
        if level is None:
            return None
        return self.chats.spawn(self._notify(level, snapshot, used.value, limit.value),
                                f"memory-notice:{level:.0%}")

    async def _notify(self, level: float, snapshot: Snapshot, used: int, limit: int) -> None:
        chats = self.chats
        memory = {sid: s.memory_pss.value for sid, s in snapshot.sessions.items() if s.memory_pss.known}
        places = await chats.engine.store.read(lambda conn: _places(conn, list(memory)))
        said = text(used, limit, users(memory, places), chats.engine.waiting_for_memory())
        workspace = await chats.workspace()
        key = f"advisory:memory:{round(level * 100)}:{snapshot.as_of}"

        def post(w: Writes, coordinator: str | None = None) -> None:
            message = rows.add_message(w, chat_id=workspace["id"], author_kind="system", author_id=AUTHOR,
                                       text=said, client_id=key)
            if coordinator is not None:
                w.wake(coordinator, Wake(id=key, reason="advisory", origin="system", delivery="add",
                                         text=envelope.wake(reason="advisory", chat=workspace, trigger=message,
                                                            now=w.now, trust="data", mention=False)))
        try:
            await chats.with_coordinator(workspace, post)
        except ServiceError as exc:     # no coordinator may start: the notice still shows
            log.info("storm notices: the workspace coordinator can't be woken (%s); the notice is only posted",
                     exc.message)
            await chats.engine.write(post)
