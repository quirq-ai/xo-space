"""Space advisory notices: a crossed memory level becomes a workspace message
and a coordinator wake (plan §9); a runtime's change is recorded and has the
project coordinators it concerns list their tools again (plan §5.5, §5.6)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Sequence

from services.advisory import RuntimeFacts, Snapshot
from services.cowork_agent.storm import runtime_policy
from services.cowork_agent.storm.chats import envelope, rows
from services.cowork_agent.storm.engine import Wake, Writes
from services.cowork_agent.storm.engine.session import Meta
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


#: The event recording a runtime's change of install, sign-in or account.
RUNTIMES_CHANGED = "runtimes.changed"
#: The role whose tools list the runtimes (``create_task``).
LISTS_RUNTIMES = "project-coordinator"


def runtime_state(facts: RuntimeFacts) -> dict[str, Any]:
    return {"installed": facts.installed, "signed_in": facts.signed_in, "account": facts.account}


def runtime_line(runtime: str, before: Mapping[str, Any], after: Mapping[str, Any], at: str) -> str | None:
    """What a coordinator is told of a ``runtimes.changed``; None when ``create_task``'s list stays the same."""
    offered = bool(after["installed"] and after["signed_in"])
    if offered == bool(before["installed"] and before["signed_in"]):
        return None
    if offered:
        what = "signed in" if before["installed"] else "was installed, signed in"
    else:
        what = "was uninstalled" if not after["installed"] else "signed out"
    return f"Runtime {runtime} {what} at {at}; create_task {'now offers it' if offered else 'no longer offers it'}."


class RuntimeChanges:
    """The advisory's listener that records each runtime's change
    (``runtimes.changed``) and recycles, at its next idle moment and with its
    conversation kept, each live project coordinator whose ``create_task``
    list it changes, so it lists its tools again (A13)."""

    def __init__(self, chats: Any) -> None:
        self.chats = chats
        self._seen: Mapping[str, RuntimeFacts] = chats.engine.advisory.snapshot().runtimes
        self._unwatch = None

    def start(self) -> None:
        self._unwatch = self.chats.engine.advisory.on_change(lambda snapshot, _accounts: self.observe(snapshot))

    def stop(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None

    def observe(self, snapshot: Snapshot) -> asyncio.Task | None:
        """Record the runtimes ``snapshot`` changed (a first reading is no change); the task doing it, else None."""
        before, self._seen = self._seen, snapshot.runtimes
        changed = [(name, before[name], after) for name, after in snapshot.runtimes.items()
                   if name in before and runtime_state(before[name]) != runtime_state(after)]
        return self.chats.spawn(self._record(changed), "runtimes-changed") if changed else None

    async def _record(self, changed: list[tuple[str, RuntimeFacts, RuntimeFacts]]) -> None:
        engine = self.chats.engine
        flipped = [name for name, old, new in changed if old.usable != new.usable]
        space, projects = await engine.store.read(runtime_policy.stored) if flipped else ({}, {})

        def concerned(meta: Meta) -> bool:
            if meta.role != LISTS_RUNTIMES or not flipped:
                return False
            policy = runtime_policy.effective(space, projects.get(meta.chat_id))
            return any(policy.permits(name) for name in flipped)

        def record(w: Writes, ids: list[str]) -> None:
            for name, old, new in changed:
                w.append(RUNTIMES_CHANGED, {"runtime": name, "before": runtime_state(old),
                                            "after": runtime_state(new), "recycle": ids if name in flipped else []})
        await engine.recycle(concerned, record)
