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
3. Turns still marked running become ``interrupted`` (reason ``restart``),
   and sub-agent runs still working become ``lost``: their process is gone
   (ch 14 §2.2, §5.3); the session's next input says how many
   (``sessions.runs_lost_untold``).
4. Wakes those turns had taken go back to the queue, at least once, with a
   note naming the last step the turn got to. A turn that recorded its
   result already consumed its wakes, in the same transaction.
   A wake handed back ``MAX_REDELIVERIES`` times is dropped instead, and the
   person is told.
5. Calls held waiting for the person are reported for the review queue to
   park (``ReviewDesk.park``, when the engine starts): the process holding
   them is gone, so the decision will come back as a grant and a wake. Then
   every session that had a process (or work steps 4–5 touched) gets a new
   epoch, so anything left of the old process is fenced off, and goes
   ``parked`` if it has a held call, else ``cold``. Its ``session`` event
   names the wakes requeued (and dropped), the calls to park and the runs
   lost (the stream's vocabulary, ch 09 §3).
6. The sessions with queued wakes are returned, oldest first, for the engine
   to admit (four at a time).
7. Per-session folders under ``~/.quirq/run/`` are removed, except those of
   sessions whose process may have survived step 2. One that can't be
   removed is logged and left, so it never stops the boot.

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

from services.cowork_agent.storm.runs import records as runs
from services.cowork_agent.storm.store.db import Store, Tx
from services.cowork_agent.storm.store.lock import EngineLock
from services.storage.layout import run_dir
from services.timestamps import now_iso
from utils.commands import boot_time, reap_orphan

log = logging.getLogger(__name__)

RESUME_NOTE = "Your previous turn was interrupted after {step} {why}; check the state before redoing anything."
RESTARTED = "because the server restarted"
#: A wake whose turn failed this many times (a retryable error, a crash) is
#: dropped the next time, with a `session` event the person sees, so a bad
#: message can't loop (a bound on retries, not a cap on work, §0.17). A
#: restart or shutdown says nothing about the message and doesn't count.
MAX_REDELIVERIES = 3
#: A session's own folder under run/ is named by its id.
SESSION_FOLDER_PREFIX = "cse_"


@dataclass(frozen=True)
class RecoveryReport:
    orphans_cleared: int
    orphans_left: int
    turns_interrupted: tuple[str, ...]
    sessions_fenced: tuple[str, ...]
    wakes_requeued: tuple[str, ...]
    calls_parked: tuple[str, ...]           # for the review queue to park (``ReviewDesk.park``)
    runs_lost: tuple[str, ...]
    sessions_to_rekick: tuple[str, ...]
    folders_removed: int


async def recover(store: Store, lock: EngineLock, *, folders: Path | None = None) -> RecoveryReport:
    if not lock.held:
        raise RuntimeError("boot recovery runs only under the engine lock")
    cleared, survivors = await _reap_orphans(store)
    settled = await store.write(_settle)
    removed = await asyncio.to_thread(_clear_session_folders, folders or run_dir(), survivors)
    report = RecoveryReport(orphans_cleared=cleared, orphans_left=len(survivors), folders_removed=removed, **settled)
    log.info("storm recovery: %d orphan(s) cleared, %d left running, %d turn(s) interrupted, %d run(s) lost, "
             "%d wake(s) requeued, %d call(s) to park, %d session(s) to re-kick, %d folder(s) removed",
             report.orphans_cleared, report.orphans_left, len(report.turns_interrupted), len(report.runs_lost),
             len(report.wakes_requeued), len(report.calls_parked), len(report.sessions_to_rekick),
             report.folders_removed)
    return report


async def _reap_orphans(store: Store) -> tuple[int, list[str | None]]:
    """Stop each registered process that is still running. Returns how many
    rows were cleared, and the session of each row kept because its process
    may have survived (None for a process no session owns)."""
    rows = await store.read(lambda conn: conn.execute(
        "SELECT pid, start_ticks, started_at, session_id FROM procs").fetchall())
    booted = boot_time()
    gone = await asyncio.gather(*(_gone(pid, ticks, started_at, booted) for pid, ticks, started_at, _ in rows))
    cleared = [(pid, ticks) for (pid, ticks, _, _), done in zip(rows, gone) if done]
    survivors = []
    for (pid, ticks, _, session_id), done in zip(rows, gone):
        if not done:
            log.warning("storm recovery: process %d (registered %s) could not be confirmed gone; its row is kept",
                        pid, ticks)
            survivors.append(session_id)

    def forget(tx: Tx) -> None:
        tx.conn.executemany("DELETE FROM procs WHERE pid = ? AND start_ticks = ?", cleared)
    await store.write(forget)
    return len(cleared), survivors


async def _gone(pid: int, start_ticks: int, started_at: str, booted: float | None) -> bool:
    """A row written before this boot names nothing that runs now (start
    ticks count from the boot); any other is checked and stopped."""
    try:
        if booted is not None and datetime.fromisoformat(started_at).timestamp() < booted:
            return True
    except ValueError:
        pass                # unreadable time: check the process instead
    try:
        return await reap_orphan(pid, start_ticks)
    except Exception:  # noqa: BLE001 - one row must never stop the boot; the row is kept and retried
        log.exception("storm recovery: checking process %d failed; its row is kept", pid)
        return False


def _settle(tx: Tx) -> dict[str, tuple[str, ...]]:
    now = now_iso()
    turns = tx.execute("SELECT id, session_id, epoch FROM turns WHERE status = 'running' ORDER BY started_at").fetchall()
    for turn_id, session_id, epoch in turns:
        tx.execute("UPDATE turns SET status = 'interrupted', ended_at = ?, stop_reason = 'restart' WHERE id = ?",
                   (now, turn_id))
        tx.append("turn.finished", {"status": "interrupted", "reason": "restart"},
                  session_id=session_id, turn_id=turn_id, epoch=epoch)
    lost = runs.lose(tx, now=now)

    requeued: dict[str, list[str]] = {}
    taken = tx.execute("SELECT turn_id, session_id FROM wakes WHERE state = 'taken' GROUP BY turn_id "
                       "ORDER BY min(created_at)").fetchall()
    for turn_id, session_id in taken:
        back, _ = hand_back(tx, turn_id, RESUME_NOTE.format(step=last_step(tx.conn, turn_id), why=RESTARTED),
                            now=now, counts=False)
        requeued.setdefault(session_id, []).extend(back)

    # Held calls died with their process: the review queue parks them when the
    # engine starts (the decision then comes back as a grant and a wake).
    parked: dict[str, list[str]] = {}
    held = tx.execute("SELECT id, session_id FROM review_items "
                      "WHERE state = 'open' AND parked = 0 AND native IS NOT NULL ORDER BY created_at").fetchall()
    for item_id, session_id in held:
        parked.setdefault(session_id, []).append(item_id)

    # Every session that had a process, or work the restart touched: a new
    # epoch fences off anything left of the old process. One with a parked
    # call is `parked` (it waits on the person); the rest are `cold`.
    sessions = tx.execute("SELECT id, chat_id, thread_ts, status FROM sessions WHERE retired_at IS NULL "
                          "ORDER BY id").fetchall()
    fenced = []
    for session_id, chat_id, thread_ts, status in sessions:
        touched = session_id in requeued or session_id in parked or session_id in lost
        if status in ("cold", "parked") and not touched:
            continue                # no process, nothing touched: already settled
        new_status = "parked" if session_id in parked else "cold"
        epoch = tx.execute("UPDATE sessions SET status = ?, epoch = epoch + 1, updated_at = ? WHERE id = ? "
                           "RETURNING epoch", (new_status, now, session_id)).fetchone()[0]
        tx.append("session", {"status": new_status, "reason": "restart",
                              "wakes_requeued": requeued.get(session_id, []), "parked": parked.get(session_id, []),
                              "runs_lost": lost.get(session_id, [])},
                  chat_id=chat_id, session_id=session_id, thread_ts=thread_ts, epoch=epoch)
        fenced.append(session_id)

    rekick = tx.execute("SELECT session_id FROM wakes WHERE state = 'queued' GROUP BY session_id "
                        "ORDER BY min(created_at)").fetchall()
    return {
        "turns_interrupted": tuple(t[0] for t in turns),
        "sessions_fenced": tuple(fenced),
        "wakes_requeued": tuple(w for ws in requeued.values() for w in ws),
        "calls_parked": tuple(h[0] for h in held),
        "runs_lost": tuple(r for rs in lost.values() for r in rs),
        "sessions_to_rekick": tuple(r[0] for r in rekick),
    }


def hand_back(tx: Tx, turn_id: str, note: str, *, now: str, counts: bool) -> tuple[list[str], list[str]]:
    """Give a turn's taken wakes back to the queue with ``note`` before them,
    at least once (ch 07 §9.3). ``counts``: the turn ended on a failure (a
    retryable error, a crash), so the hand-back counts toward
    ``MAX_REDELIVERIES``; a wake already at the bound is dropped instead. A
    restart's doesn't count. One note per hand-back: it goes before the
    first wake (they run in that order, batched), and an earlier note is
    removed, never stacked: the wake's ``meta.resume_note`` marks one, so a
    person's own words are never taken for it. Every wake handed back is
    marked ``meta.handed_back`` (a session waiting on a decision runs only a
    new message, ch 07 §6). Returns (requeued, dropped). Boot recovery and
    the engine use it alike."""
    requeued, dropped = [], []
    for wake_id, envelope, redelivery, meta in tx.execute(
            "SELECT id, envelope, redelivery, meta FROM wakes WHERE turn_id = ? AND state = 'taken' ORDER BY rowid",
            (turn_id,)).fetchall():
        if counts and redelivery >= MAX_REDELIVERIES:
            tx.execute("UPDATE wakes SET state = 'dropped', done_at = ? WHERE id = ?", (now, wake_id))
            dropped.append(wake_id)
            continue
        body = envelope or ""
        if json.loads(meta).get("resume_note"):
            body = body.partition("\n\n")[2]
        noted = not requeued            # the first wake handed back carries the one note
        text = (f"{note}\n\n{body}" if body else note) if noted else body
        tx.execute("UPDATE wakes SET state = 'queued', turn_id = NULL, taken_at = NULL, redelivery = redelivery + ?, "
                   "envelope = ?, meta = json_set(meta, '$.resume_note', json(?), '$.handed_back', json('true')) "
                   "WHERE id = ?", (1 if counts else 0, text, "true" if noted else "false", wake_id))
        requeued.append(wake_id)
    return requeued, dropped


def dropped_message(why: str) -> str:
    """What the person reads when a wake was dropped."""
    return (f"Couldn't run your message: it was tried {MAX_REDELIVERIES + 1} times and each try ended {why}. "
            "Send it again once that is fixed.")


def last_step(conn: sqlite3.Connection, turn_id: str | None) -> str:
    """The title of the turn's last tool step, or "it started"."""
    row = conn.execute("SELECT data FROM events WHERE turn_id = ? AND type = 'step.tool' "
                       "ORDER BY seq DESC LIMIT 1", (turn_id,)).fetchone() if turn_id else None
    title = json.loads(row[0]).get("title") if row else None
    return f"“{title}”" if title else "it started"


def _clear_session_folders(folder: Path, survivors: list[str | None]) -> int:
    """Remove each session's folder, except those of sessions whose process
    may still be running: it may still use its folder (an agy hook socket)."""
    if not folder.is_dir():
        return 0
    removed = 0
    for child in folder.iterdir():
        if not child.name.startswith(SESSION_FOLDER_PREFIX) or child.name in survivors:
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
