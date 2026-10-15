"""Boot recovery: put the store back in order after the server stopped
without warning (ch 07 §9.3). Runs once, under the engine lock, before any
route may write through the engine.

1. The engine lock is held (the caller took it).
2. Processes an earlier run started and lost are stopped, with whatever is
   left in their sessions: only rows of the process registry, each by pid and
   start time, so a recycled pid or the person's own CLI is never touched. A
   row is cleared once its process is gone (rows from before this machine's
   boot name nothing that runs); a row whose process survived stays, and the
   next boot tries again.
3. Turns still marked running become ``interrupted`` (reason ``restart``); every
   session that had a process goes ``cold`` and its epoch moves, so anything
   left of the old process is fenced off.
4. Wakes those turns had taken go back to the queue, at least once, with a
   note naming the last step the turn got to. A turn that recorded its result
   already consumed its wakes, in the same transaction.
5. Calls held waiting for the person are parked: the process holding them is
   gone, so the decision will come back as a grant and a wake.
6. The sessions with queued wakes are returned, oldest first, for the engine
   to admit (four at a time).
7. Per-session folders under ``~/.quirq/run/`` are removed: none has a live
   owner now. One that can't be removed is logged and left, so it never
   stops the boot.

Steps 3–5 are one transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from services.cowork_agent.storm.store.db import Store, Tx
from services.cowork_agent.storm.store.lock import EngineLock
from services.storage.layout import run_dir
from services.timestamps import now_iso
from utils.commands import boot_time, reap_orphan

log = logging.getLogger(__name__)

RESUME_NOTE = ("Your previous turn was interrupted after {step} because the server restarted; "
               "check the state before redoing anything.")
#: A session's own folder under run/ is named by its id.
SESSION_FOLDER_PREFIX = "cse_"


@dataclass(frozen=True)
class RecoveryReport:
    orphans_cleared: int
    orphans_left: int
    turns_interrupted: tuple[str, ...]
    sessions_fenced: tuple[str, ...]
    wakes_requeued: tuple[str, ...]
    calls_parked: tuple[str, ...]
    sessions_to_rekick: tuple[str, ...]
    folders_removed: int


async def recover(store: Store, lock: EngineLock, *, folders: Path | None = None) -> RecoveryReport:
    if not lock.held:
        raise RuntimeError("boot recovery runs only under the engine lock")
    cleared, left = await _reap_orphans(store)
    settled = await store.write(_settle)
    removed = await asyncio.to_thread(_clear_session_folders, folders or run_dir())
    report = RecoveryReport(orphans_cleared=cleared, orphans_left=left, folders_removed=removed, **settled)
    log.info("storm recovery: %d orphan(s) cleared, %d left running, %d turn(s) interrupted, %d wake(s) requeued, "
             "%d call(s) parked, %d session(s) to re-kick, %d folder(s) removed",
             report.orphans_cleared, report.orphans_left, len(report.turns_interrupted), len(report.wakes_requeued),
             len(report.calls_parked), len(report.sessions_to_rekick), report.folders_removed)
    return report


async def _reap_orphans(store: Store) -> tuple[int, int]:
    """Stop each registered process that is still running; returns how many
    rows were cleared and how many stay because their process survived."""
    rows = await store.read(lambda conn: conn.execute("SELECT pid, start_ticks, started_at FROM procs").fetchall())
    booted = boot_time()
    gone = await asyncio.gather(*(_gone(pid, ticks, started_at, booted) for pid, ticks, started_at in rows))
    cleared = [(pid, ticks) for (pid, ticks, _), done in zip(rows, gone) if done]
    for (pid, ticks, _), done in zip(rows, gone):
        if not done:
            log.warning("storm recovery: process %d (registered %s) could not be confirmed gone; its row is kept", pid, ticks)

    def forget(tx: Tx) -> None:
        tx.conn.executemany("DELETE FROM procs WHERE pid = ? AND start_ticks = ?", cleared)
    await store.write(forget)
    return len(cleared), len(rows) - len(cleared)


async def _gone(pid: int, start_ticks: int, started_at: str, booted: float | None) -> bool:
    """A row written before this boot names nothing that runs now (start
    ticks count from the boot); any other is checked and stopped."""
    try:
        if booted is not None and datetime.fromisoformat(started_at).timestamp() < booted:
            return True
    except ValueError:
        pass
    return await reap_orphan(pid, start_ticks)


def _settle(tx: Tx) -> dict[str, tuple[str, ...]]:
    now = now_iso()
    turns = tx.execute("SELECT id, session_id, epoch FROM turns WHERE status = 'running' ORDER BY started_at").fetchall()
    for turn_id, session_id, epoch in turns:
        tx.execute("UPDATE turns SET status = 'interrupted', ended_at = ?, stop_reason = 'restart' WHERE id = ?",
                   (now, turn_id))
        tx.append("turn.finished", {"status": "interrupted", "reason": "restart"},
                  session_id=session_id, turn_id=turn_id, epoch=epoch)

    sessions = tx.execute(
        "SELECT id, chat_id, thread_ts FROM sessions WHERE status <> 'cold' AND retired_at IS NULL ORDER BY id"
    ).fetchall()
    for session_id, chat_id, thread_ts in sessions:
        epoch = tx.execute("UPDATE sessions SET status = 'cold', epoch = epoch + 1, updated_at = ? WHERE id = ? "
                           "RETURNING epoch", (now, session_id)).fetchone()[0]
        tx.append("session", {"status": "cold", "reason": "restart"},
                  chat_id=chat_id, session_id=session_id, thread_ts=thread_ts, epoch=epoch)

    wakes = tx.execute("SELECT id, session_id, turn_id, envelope FROM wakes WHERE state = 'taken' "
                       "ORDER BY created_at").fetchall()
    for wake_id, session_id, turn_id, envelope in wakes:
        note = RESUME_NOTE.format(step=_last_step(tx.conn, turn_id))
        tx.execute("UPDATE wakes SET state = 'queued', turn_id = NULL, taken_at = NULL, "
                   "redelivery = redelivery + 1, envelope = ? WHERE id = ?",
                   (f"{note}\n\n{envelope}" if envelope else note, wake_id))
        tx.append("wake.requeued", {"wake": wake_id, "reason": "restart"}, session_id=session_id,
                  turn_id=turn_id)

    held = tx.execute("SELECT id, chat_id, thread_ts, session_id FROM review_items "
                      "WHERE state = 'open' AND parked = 0 AND native IS NOT NULL ORDER BY created_at").fetchall()
    for item_id, chat_id, thread_ts, session_id in held:
        tx.execute("UPDATE review_items SET parked = 1 WHERE id = ?", (item_id,))
        tx.append("review.parked", {"item": item_id, "reason": "restart"}, chat_id=chat_id,
                  session_id=session_id, thread_ts=thread_ts)

    rekick = tx.execute("SELECT session_id FROM wakes WHERE state = 'queued' GROUP BY session_id "
                        "ORDER BY min(created_at)").fetchall()
    return {
        "turns_interrupted": tuple(t[0] for t in turns),
        "sessions_fenced": tuple(s[0] for s in sessions),
        "wakes_requeued": tuple(w[0] for w in wakes),
        "calls_parked": tuple(h[0] for h in held),
        "sessions_to_rekick": tuple(r[0] for r in rekick),
    }


def _last_step(conn: sqlite3.Connection, turn_id: str | None) -> str:
    """The title of the turn's last recorded step, or "it started"."""
    row = conn.execute("SELECT data FROM events WHERE turn_id = ? AND type LIKE 'step.%' "
                       "ORDER BY seq DESC LIMIT 1", (turn_id,)).fetchone() if turn_id else None
    title = json.loads(row[0]).get("title") if row else None
    return f"“{title}”" if title else "it started"


def _clear_session_folders(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    removed = 0
    for child in folder.iterdir():
        if not child.name.startswith(SESSION_FOLDER_PREFIX):
            continue
        try:
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        except OSError as exc:
            log.warning("storm recovery: could not remove %s (%s); it is left as it is", child, exc)
            continue
        removed += 1
    return removed
