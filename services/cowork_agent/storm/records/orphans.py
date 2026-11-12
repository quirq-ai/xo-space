"""No orphan work (record design v2 §5 rule 9, §6 invariant 6): when a
task or a goal ends, its timers expire, its leases are released and the
wakes its timers queued that no session has taken yet are dropped, all in
the transaction that ends it. ``tasks.move`` and ``goals.move`` call it.

A goal's work is its own timers and its tasks': their timers and leases.
The other wakes queued for a task's thread (the founder's message, its
coordinator's follow-up) are new input, not the ended task's work, and stay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from services.cowork_agent.storm.records import leases, ledger, timers
from services.cowork_agent.storm.store import scrub

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine.ports import Writes


def settle(w: Writes, *, task_ids: Sequence[str], goal_id: str | None = None, why: str, said: str) -> None:
    """End what ``task_ids`` (and ``goal_id``) leave behind: ``why`` is the
    leases' ``released_why`` and the dropped wakes' reason, ``said`` the
    expired timers' reason, in words."""
    owned = timers.of_work(w, task_ids=task_ids, goal_id=goal_id)
    for timer in owned:
        timers.end(w, timer, state="expired", reason=said, actor=ledger.XO)
    patterns = [f"timer:{timer.id}:*" for timer in owned]          # each timer's wakes: timer:<id>:<slot>
    if goal_id is not None:
        patterns.append(f"keeper:{goal_id}:*")
    if patterns:
        gone = w.execute(f"UPDATE wakes SET state = 'dropped', done_at = ? WHERE state = 'queued' AND "
                         f"({' OR '.join('id GLOB ?' for _ in patterns)}) RETURNING id, session_id",
                         (w.now, *patterns)).fetchall()
        if gone:
            scrub(w.tx, [wake_id for wake_id, _session in gone])
            for session_id in dict.fromkeys(session for _wake, session in gone):
                w.append("wake.dropped", {"ids": [wake_id for wake_id, s in gone if s == session_id], "reason": why},
                         session_id=session_id)
    for lease in leases.of_tasks(w, task_ids):
        leases.release(w, lease, why=why, actor=ledger.XO)
