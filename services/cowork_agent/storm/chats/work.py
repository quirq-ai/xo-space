"""A chat's work as it stands, for the founder's panel (the plan's W6,
"your panel's data"): its goals that haven't ended, with each one's pause or
stall reason; every task still open or stopped, and every task of those
goals; the timers and jobs armed in it; and the leases its sessions hold.
Read only, from the store's records in one snapshot. Goals and tasks that
ended stay in the records for the audit and aren't listed.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from services.cowork_agent.storm.records import goals, leases, tasks, timers

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.chats import Chats

#: What an overview leaves out of a record: its idempotency key, and a
#: task's brief (the thread itself carries it).
_LEFT_OUT = ("request_key", "instructions")


def _record(row: Any) -> dict[str, Any]:
    return {key: value for key, value in dataclasses.asdict(row).items() if key not in _LEFT_OUT}


async def snapshot(chats: Chats, chat_id: str) -> dict[str, Any]:
    """The chat's goals, tasks, timers and leases now, and the event seq
    they are read at (``through``). ``ChatRefused`` for a chat that isn't
    there."""
    await chats.chat(chat_id)

    def read(conn: sqlite3.Connection) -> dict[str, Any]:
        live = [g for g in goals.of_chat(conn, chat_id) if g.state not in goals.FINAL]
        theirs = {g.id for g in live}
        held = [leases.get(conn, row[0]) for row in conn.execute(
            "SELECT l.id FROM leases l JOIN sessions s ON s.id = l.holder_session "
            "WHERE s.chat_id = ? AND l.released_at IS NULL ORDER BY l.acquired_at, l.id", (chat_id,))]
        armed = sorted(timers.armed_in(conn, chat_id), key=lambda t: (t.due_at is None, t.due_at or "", t.id))
        return {"chat_id": chat_id, "through": conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0],
                "goals": [_record(g) for g in live],
                "tasks": [_record(t) for t in tasks.of_chat(conn, chat_id, limit=-1)
                          if t.state in ("open", "stopped") or t.goal_id in theirs],
                "timers": [{**_record(t), "job": t.definition, "fired": t.last} for t in armed],
                "leases": [_record(lease) for lease in held]}
    return await chats.engine.store.read(read)
