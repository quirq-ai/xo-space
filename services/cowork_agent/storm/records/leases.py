"""Leases: one holder at a time of a shared thing, a browser seat or a port
(record design v2 §2, §4, §5 rules 7 and 9, §6 invariant 4; w5-w7-design W6).

A session takes one with ``lease`` and gives it back with ``release``. Its
holder is that session and the process that uses the thing: by default the
session's own CLI process, or one it names (a server it started on the
port). The lease also ends when that process exits, when its time is up,
and when its task or goal ends (``orphans``); ``released_why`` says which.
The store's unique index allows one live holder per resource, so a second
taker is refused (``LeaseHeld``) with who holds it and since when; the
holder taking it again gets its own lease back.

Each change writes the row, one ``lease`` event and one ledger entry in the
caller's transaction.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from services.cowork_agent.storm.records import RecordInvalid, ledger
from services.errors import ServiceError

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.ports import Writes

#: Why a lease ended.
WHY = ("released", "process_exited", "expired", "task_ended", "goal_ended")
_SELECT = f"SELECT {', '.join(ledger.LEASE_COLUMNS)} FROM leases"


@dataclass(frozen=True)
class Lease:
    """One ``leases`` row; its fields are the table's columns, in order."""

    id: str
    resource: str
    holder_session: str
    holder_pid: int | None
    holder_start_ticks: int | None
    task_id: str | None
    acquired_at: str
    expires_at: str | None
    released_at: str | None
    released_why: str | None


class LeaseHeld(ServiceError):
    """Someone else holds the resource: who, and since when."""

    def __init__(self, lease: Lease) -> None:
        until = f", until {lease.expires_at}" if lease.expires_at else ""
        super().__init__("lease_held", f"{lease.resource} is held by session {lease.holder_session} since "
                                       f"{lease.acquired_at}{until} (lease {lease.id}).", status=409)
        self.lease = lease


def get(conn: sqlite3.Connection | Writes, lease_id: str) -> Lease | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", (lease_id,)).fetchone()
    return Lease(*row) if row else None


def held(conn: sqlite3.Connection | Writes, resource: str) -> Lease | None:
    """The live lease on ``resource``, if any."""
    row = conn.execute(f"{_SELECT} WHERE resource = ? AND released_at IS NULL", (resource,)).fetchone()
    return Lease(*row) if row else None


def live(conn: sqlite3.Connection | Writes) -> list[Lease]:
    return [Lease(*row) for row in conn.execute(f"{_SELECT} WHERE released_at IS NULL ORDER BY acquired_at, id")]


def of_session(conn: sqlite3.Connection | Writes, session_id: str) -> list[Lease]:
    """The live leases ``session_id`` holds."""
    return [Lease(*row) for row in conn.execute(
        f"{_SELECT} WHERE holder_session = ? AND released_at IS NULL ORDER BY acquired_at, id", (session_id,))]


def of_process(conn: sqlite3.Connection | Writes, pid: int, start_ticks: int) -> list[Lease]:
    """The live leases whose holder process is this one."""
    return [Lease(*row) for row in conn.execute(
        f"{_SELECT} WHERE holder_pid = ? AND holder_start_ticks = ? AND released_at IS NULL ORDER BY id",
        (pid, start_ticks))]


def of_tasks(conn: sqlite3.Connection | Writes, task_ids: Sequence[str]) -> list[Lease]:
    """The live leases taken for these tasks."""
    marks = ", ".join("?" * len(task_ids))
    return [Lease(*row) for row in conn.execute(
        f"{_SELECT} WHERE task_id IN ({marks}) AND released_at IS NULL ORDER BY id", tuple(task_ids))]


def expired(conn: sqlite3.Connection | Writes, now: str) -> list[Lease]:
    """The live leases whose time is up at ``now``."""
    return [Lease(*row) for row in conn.execute(
        f"{_SELECT} WHERE released_at IS NULL AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at, id",
        (now,))]


def next_expiry(conn: sqlite3.Connection | Writes) -> str | None:
    return conn.execute("SELECT min(expires_at) FROM leases WHERE released_at IS NULL").fetchone()[0]


def acquire(w: Writes, *, resource: str, holder_session: str, holder_pid: int | None = None,
            holder_start_ticks: int | None = None, task_id: str | None = None,
            expires_at: str | None = None) -> tuple[Lease, bool]:
    """``resource`` for ``holder_session``, and whether this call took it:
    the holder asking again gets its live lease back, as it is; anyone else
    is refused with ``LeaseHeld``."""
    if (current := held(w, resource)) is not None:
        if current.holder_session == holder_session:
            return current, False
        raise LeaseHeld(current)
    lease = Lease(f"lse_{uuid.uuid4().hex[:16]}", resource, holder_session, holder_pid, holder_start_ticks, task_id,
                  w.now, expires_at, None, None)
    w.execute(f"INSERT INTO leases ({', '.join(ledger.LEASE_COLUMNS)}) VALUES "
              f"({', '.join('?' * len(ledger.LEASE_COLUMNS))})", dataclasses.astuple(lease))
    _record(w, lease, action="acquire", actor=holder_session)
    return lease, True


def release(w: Writes, lease: Lease, *, why: str, actor: str) -> Lease:
    """End a live lease, saying why; one already released is returned as it
    is, and nothing is written."""
    if why not in WHY:
        raise RecordInvalid(f"A lease ends {', '.join(WHY)}; {why!r} isn't one.")
    if lease.released_at is not None:
        return lease
    ended = dataclasses.replace(lease, released_at=w.now, released_why=why)
    w.execute("UPDATE leases SET released_at = ?, released_why = ? WHERE id = ?", (w.now, why, lease.id))
    _record(w, ended, action="release", actor=actor)
    return ended


def _record(w: Writes, lease: Lease, *, action: str, actor: str) -> None:
    w.append("lease", {"action": action, "id": lease.id, "resource": lease.resource,
                       "holder_session": lease.holder_session, "holder_pid": lease.holder_pid,
                       "holder_start_ticks": lease.holder_start_ticks, "task_id": lease.task_id,
                       "expires_at": lease.expires_at, "why": lease.released_why, "by": actor},
             session_id=lease.holder_session)
    ledger.append_row(w, actor=actor, action=action, kind="lease", row_id=lease.id)
