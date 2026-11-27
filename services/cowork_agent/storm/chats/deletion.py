"""Delete a thread: soft at once (its branch always kept), undoable for ``UNDO_S``, purged after ``PURGE_AFTER_S``."""
# Unconfirmed loss is 409 would_lose_work; the purge keeps the ledger, task, reports, review items and turn records.

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from services.cowork_agent.storm.chats import ChatRefused, attachments, rows, thread_scope
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.records import leases, ledger, tasks, timers
from services.cowork_agent.storm.worktrees import service as worktrees, trees
from services.errors import ServiceError
from services.timestamps import parse_ts
from services.timestamps import iso as iso_dt
from services.xo_structure import XO_DIRNAME

log = logging.getLogger(__name__)

#: How long a deleted thread can be brought back (the UI's Undo).
UNDO_S = 10
#: How long after its deletion a thread's content is removed, and how often
#: the purge looks (its first look a few minutes after boot).
PURGE_AFTER_S = 3600
PURGE_EVERY_S = 600.0
PURGE_FIRST_S = 300.0
#: The session's pause while its thread is deleted: only a restore lifts it.
PAUSE_REASON = "thread_deleted"
#: How long a halted session has to let go of its process (the reaper drops
#: the process's row once its exit is seen), checked every ``SETTLE_STEP_S``.
SETTLE_S = 10.0
SETTLE_STEP_S = 0.05
#: Who a delete, a restore and a purge name.
BY = "person"
DELETED = "thread_deleted"
#: The events a purge keeps: the records' own (the ledger chains the same
#: changes) and the deletion, so a client resuming from before it still
#: drops the thread.
KEPT_EVENTS = (DELETED, "task", "task.report", "result", "timer", "lease", "intent", "goal")
#: The events of what its turns did, kept by the purge too, by prefix.
KEPT_EVENT_FAMILIES = ("step.", "turn.", "run.")
#: Rows one transaction of the purge's event removal deletes (as the
#: retention prune's).
BATCH_ROWS = 500
#: The kinds of work a delete can lose, in the order its summary names them.
GIT_KINDS = ("changed", "untracked", "ignored", "commits")
WORK_KINDS = ("running", "runs", "wakes", "timers", "asks")

_locks: dict[tuple[str, str], asyncio.Lock] = {}


class ThreadWouldLoseWork(ServiceError):
    """Deleting the thread would lose work: ``409 would_lose_work`` with what, and the confirm that goes ahead."""

    def __init__(self, what: dict[str, Any], confirm: str) -> None:
        super().__init__("would_lose_work", f"Deleting this thread would lose {what['summary']}. To delete it "
                                            f"anyway, send the same request with confirm: {confirm}.", status=409)
        self.what, self.confirm = what, confirm


def _lock(chat_id: str, ts: str) -> asyncio.Lock:
    """One delete, restore or purge of a thread at a time."""
    return _locks.setdefault((chat_id, ts), asyncio.Lock())


# ── who may delete ───────────────────────────────────────────────────────────

#: Whether the person can delete a thread: ``rows.deletable``.
can_delete = rows.deletable


def refuse_deleted(conn, chat_id: str, ts: str) -> None:
    """Raise ``404 thread_deleted`` for a deleted thread: nothing reads or acts on it but its restore."""
    if rows.deleted(conn, chat_id, ts) is not None:
        raise ChatRefused(DELETED, f"Thread {ts!r} was deleted.", status=404)


def undo_until(deleted_at: str) -> str:
    when = parse_ts(deleted_at)
    assert when is not None, deleted_at
    return iso_dt(when + timedelta(seconds=UNDO_S))


# ── delete ───────────────────────────────────────────────────────────────────

async def delete(chats: Any, chat_id: str, ts: str, *, confirm: str | None = None) -> dict[str, Any]:
    """Delete the thread (the module's docstring): ``{deleted, undo_until}``."""
    chat = await chats.chat(chat_id)
    chats.writable(chat)
    engine = chats.engine
    trees_service = worktrees.current()
    async with _lock(chat_id, ts):
        def read(conn) -> tuple:
            row = conn.execute("SELECT session_id, deleted_at, purged_at, branch FROM threads WHERE chat_id = ? AND "
                               "ts = ?", (chat_id, ts)).fetchone()
            return row, row is not None and can_delete(conn, chat_id, ts)
        row, deletable = await engine.store.read(read)
        if row is None:
            raise ChatRefused("thread_not_found", f"There is no thread {ts!r} in this chat.", status=404)
        session_id, deleted_at, purged_at, branch = row
        if deleted_at is not None:
            until = undo_until(deleted_at)
            if purged_at is None and _before(engine, until):
                return {"deleted": True, "undo_until": until}          # the same request again
            raise ChatRefused(DELETED, f"Thread {ts!r} was deleted.", status=404)
        if not deletable:
            raise ChatRefused("thread_protected", "This thread can't be deleted here.", status=409)
        what, kinds = await _what(chats, trees_service, chat_id, ts, session_id, branch)
        if kinds and confirm != _confirmation(chat_id, ts, kinds):
            raise ThreadWouldLoseWork(what, _confirmation(chat_id, ts, kinds))
        paused_here = await _hold(engine, session_id)
        try:
            # Read again once nothing new can start: an input that arrived meanwhile, or a turn admitted
            # before the pause, is work the person wasn't shown.
            again, now = await _what(chats, trees_service, chat_id, ts, session_id, branch)
            if set(now) - set(kinds):
                raise ThreadWouldLoseWork(again, _confirmation(chat_id, ts, now))
            if session_id is not None:
                await engine.halt(session_id, reason=DELETED, by=BY)
                await _settle(trees_service, session_id)
            try:
                removed = await trees_service.remove_keeping_branch(chat_id, ts, session_id,
                                                                    accept=lambda loss: _accepts(loss, kinds))
            except worktrees.WouldLoseWork:
                # The stopped session left work of a kind the person didn't confirm: shown, with its own confirm.
                again, now = await _what(chats, trees_service, chat_id, ts, session_id, branch)
                raise ThreadWouldLoseWork(again, _confirmation(chat_id, ts, now)) from None

            def record(w: Writes) -> str:
                w.execute("UPDATE threads SET deleted_at = ? WHERE chat_id = ? AND ts = ?", (w.now, chat_id, ts))
                w.append(DELETED, {"chat_id": chat_id, "ts": ts}, chat_id=chat_id, thread_ts=ts,
                         session_id=session_id)
                return w.now
            deleted_at = await engine.write(record)
        except BaseException:
            if paused_here:
                await asyncio.shield(_unhold(engine, session_id))
            raise
    log.info("storm chats: thread %s of %s deleted (%s); its worktree %s", ts, chat_id,
             what["summary"] or "nothing to lose",
             f"removed, branch {removed['branch']} kept" if removed else "wasn't there")
    return {"deleted": True, "undo_until": undo_until(deleted_at)}


async def _what(chats: Any, trees_service: worktrees.Worktrees, chat_id: str, ts: str, session_id: str | None,
                branch: str | None) -> tuple[dict[str, Any], list[str]]:
    """What deleting the thread now would lose (worktree loss plus the thread's own work), and its kinds."""
    engine = chats.engine
    loss = await trees_service.deletion_loss(chat_id, ts) or trees.Loss()
    view = await engine.get_session(session_id) if session_id is not None else None

    def counts(conn) -> dict[str, int]:
        sessions = _sessions(conn, chat_id, ts)
        marks = ", ".join("?" * len(sessions)) or "NULL"
        return {
            "runs": conn.execute(f"SELECT count(*) FROM runs WHERE session_id IN ({marks}) "
                                 "AND state IN ('running', 'waiting')", sessions).fetchone()[0],
            "wakes": conn.execute(f"SELECT count(*) FROM wakes WHERE session_id IN ({marks}) "
                                  "AND state IN ('queued', 'requeued')", sessions).fetchone()[0],
            "timers": len([t for t in timers.addressed(conn, chat_id=chat_id, role="thread", thread_ts=ts)
                           if t.state == "armed"]),
            "asks": conn.execute(f"SELECT count(*) FROM review_items WHERE state = 'open' AND (session_id IN "
                                 f"({marks}) OR (chat_id = ? AND thread_ts = ?))", (*sessions, chat_id, ts)
                                 ).fetchone()[0],
        }
    found = await engine.store.read(counts)
    running = bool(view and (view["current_turn"] is not None or view["state"] in ("starting", "running",
                                                                                     "stopping")))
    found["runs"] = max(found["runs"], view["live_runs"] if view else 0)
    what = {**loss.what(), "branch": branch, "running": running, **found}     # the branch it keeps
    kinds = [k for k in GIT_KINDS if what[k] and (k == "commits" or what[k]["count"])]
    kinds += [k for k in WORK_KINDS if what[k]]
    what["summary"] = _summary(loss, running, found)
    return what, kinds


#: How the summary names each count: (one, several).
_NOUNS = {"runs": ("background run", "background runs"), "wakes": ("message waiting for it", "messages waiting for it"),
          "timers": ("armed timer", "armed timers"), "asks": ("open question to you", "open questions to you")}


def _summary(loss: trees.Loss, running: bool, found: Mapping[str, int]) -> str:
    parts = [loss.summary()] if not loss.empty else []
    if running:
        parts.append("a running turn (what it hasn't committed goes with it)")
    for key, (one, several) in _NOUNS.items():
        if found[key]:
            parts.append(f"{found[key]} {one if found[key] == 1 else several}")
    return ", ".join(parts)


def _confirmation(chat_id: str, ts: str, kinds: Iterable[str]) -> str:
    """What the person confirms: this thread, and the kinds of work it loses."""
    return trees.confirmation({"chat_id": chat_id, "ts": ts, "kinds": sorted(kinds)})


def _accepts(loss: trees.Loss, kinds: Iterable[str]) -> bool:
    """Whether the re-read worktree loss is what was confirmed; anything, once a running turn or runs was."""
    kinds = set(kinds)
    if kinds & {"running", "runs"}:
        return True
    what = loss.what()
    return all(k in kinds for k in GIT_KINDS if what[k] and (k == "commits" or what[k]["count"]))


async def _hold(engine: Any, session_id: str | None) -> bool:
    """Pause the session for the deletion unless already paused; whether this call paused it."""
    if session_id is None:
        return False
    if (await engine.get_session(session_id))["paused"]:
        return False
    await engine.pause(session_id, reason=PAUSE_REASON)
    return True


async def _unhold(engine: Any, session_id: str | None, also: Any = None) -> None:
    try:
        await engine.resume(session_id, also=also, lifting=PAUSE_REASON)
    except Exception:  # noqa: BLE001 - said in the log; the person sees the failure that brought us here
        log.exception("storm chats: session %s stays paused after a failed thread deletion", session_id)


async def _settle(trees_service: worktrees.Worktrees, session_id: str) -> None:
    """Wait until the halted session holds no process; ``session_busy`` (409) after ``SETTLE_S``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_S
    while not await trees_service.let_go(session_id):
        if loop.time() >= deadline:
            raise ServiceError("session_busy", "This thread's session was stopped but still holds a process; "
                                               "nothing was deleted. Try again in a moment.", status=409)
        await asyncio.sleep(SETTLE_STEP_S)


def _before(engine: Any, until: str) -> bool:
    when = parse_ts(until)
    return when is not None and engine.clock.time() < when.timestamp()


# ── restore ──────────────────────────────────────────────────────────────────

async def restore(chats: Any, chat_id: str, ts: str) -> dict[str, Any]:
    """Undo a deletion: ``{worktree: {path, branch} | null}``; a live thread as is; past undo, ``410 undo_expired``."""
    chat = await chats.chat(chat_id)
    chats.writable(chat)
    engine = chats.engine
    async with _lock(chat_id, ts):
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT session_id, deleted_at, purged_at, worktree_path, branch FROM threads WHERE chat_id = ? AND "
            "ts = ?", (chat_id, ts)).fetchone())
        if row is None:
            raise ChatRefused("thread_not_found", f"There is no thread {ts!r} in this chat.", status=404)
        session_id, deleted_at, purged_at, path, branch = row
        if deleted_at is None:                      # restored already (a second Undo)
            there = path is not None and await asyncio.to_thread(os.path.lexists, path)
            return {"worktree": {"path": path, "branch": branch} if there else None}
        if purged_at is not None or not _before(engine, undo_until(deleted_at)):
            raise ChatRefused("undo_expired", f"Thread {ts!r} was deleted more than {UNDO_S} seconds ago and can't "
                                              "be brought back.", status=410)
        tree = await worktrees.current().bring_back(chat_id, ts)

        def record(w: Writes) -> None:
            w.execute("UPDATE threads SET deleted_at = NULL WHERE chat_id = ? AND ts = ?", (chat_id, ts))
            view = rows.get_thread(w, chat_id, ts)
            w.append("thread", {**view, "can_delete": can_delete(w, chat_id, ts)}, chat_id=chat_id, thread_ts=ts,
                     session_id=session_id)
        paused = session_id is not None and (await engine.get_session(session_id))["paused"] == PAUSE_REASON
        if paused:
            await engine.resume(session_id, also=record, lifting=PAUSE_REASON)   # one transaction: restored, runnable
        else:
            await engine.write(record)
    log.info("storm chats: thread %s of %s restored; its worktree %s", ts, chat_id,
             f"is back on {tree.branch}" if tree else "isn't")
    return {"worktree": {"path": str(tree.path), "branch": tree.branch} if tree else None}


# ── purge ────────────────────────────────────────────────────────────────────

async def purge_due(chats: Any) -> int:
    """Purge every thread deleted more than ``PURGE_AFTER_S`` ago; how many."""
    engine = chats.engine
    cutoff = iso(engine.clock.time() - PURGE_AFTER_S)
    due = await engine.store.read(lambda conn: conn.execute(
        "SELECT chat_id, ts FROM threads WHERE deleted_at IS NOT NULL AND purged_at IS NULL AND deleted_at < ? "
        "ORDER BY deleted_at", (cutoff,)).fetchall())
    purged = 0
    for chat_id, ts in due:
        try:
            purged += await purge(chats, chat_id, ts)
        except (ServiceError, OSError) as exc:
            log.warning("storm chats: deleted thread %s of %s wasn't purged (the next pass tries again): %s", ts,
                        chat_id, getattr(exc, "message", exc))
    return purged


async def purge(chats: Any, chat_id: str, ts: str) -> bool:
    """Purge a deleted thread's content; False when it isn't deleted, is purged already, or is still undoable."""
    engine = chats.engine
    async with _lock(chat_id, ts):
        def read(conn) -> tuple | None:
            row = conn.execute("SELECT deleted_at, purged_at, worktree_path FROM threads WHERE chat_id = ? AND "
                               "ts = ?", (chat_id, ts)).fetchone()
            if row is None or row[0] is None or row[1] is not None:
                return None
            chat = rows.get_chat(conn, chat_id)
            sessions = _sessions(conn, chat_id, ts)
            named = conn.execute(
                "SELECT DISTINCT json_extract(j.value, '$.id') FROM messages m, json_each(m.extra, '$.attachments') "
                "j WHERE m.chat_id = ? AND (m.thread_ts = ? OR m.id = ?)", (chat_id, ts, ts)).fetchall()
            return row, chat, sessions, {a for (a,) in named if a}
        found = await engine.store.read(read)
        if found is None:
            return False
        (deleted_at, _purged, tree_path), chat, sessions, att_ids = found
        if _before(engine, undo_until(deleted_at)):
            return False
        await _purge_events(engine, chat_id, ts, sessions)
        await engine.write(lambda w: _purge_rows(w, engine, chat_id, ts, sessions))
        for session_id in sessions:
            engine.retired(session_id)
        await engine.store.empty_wal()          # what the purge deleted would outlive it in the write-ahead log
        named = await engine.store.read(lambda conn: rows.attachment_ids(conn, chat_id))
    await asyncio.to_thread(_purge_files, chat, ts, tree_path, att_ids - named)
    log.info("storm chats: deleted thread %s of %s purged (its branch is kept)", ts, chat_id)
    return True


def _sessions(conn, chat_id: str, ts: str) -> list[str]:
    """Every session the thread had (a replaced one's successors too)."""
    return [s for (s,) in conn.execute("SELECT id FROM sessions WHERE chat_id = ? AND thread_ts = ? ORDER BY seq",
                                       (chat_id, ts))]


async def _purge_events(engine: Any, chat_id: str, ts: str, sessions: list[str]) -> None:
    """Delete the thread's and its sessions' events, except kept ones and the store's newest, in short batches."""
    marks = ", ".join("?" * len(sessions)) or "NULL"
    kept = ", ".join("?" * len(KEPT_EVENTS))
    families = " ".join("AND type NOT GLOB ?" for _ in KEPT_EVENT_FAMILIES)

    def batch(w: Writes) -> int:
        return w.execute(
            "DELETE FROM events WHERE seq IN (SELECT seq FROM events WHERE ((chat_id = ? AND thread_ts = ?) OR "
            f"session_id IN ({marks})) AND type NOT IN ({kept}) {families} "
            "AND seq < (SELECT max(seq) FROM events) LIMIT ?)",
            (chat_id, ts, *sessions, *KEPT_EVENTS, *(f"{p}*" for p in KEPT_EVENT_FAMILIES), BATCH_ROWS)).rowcount
    while await engine.write(batch) == BATCH_ROWS:
        pass


def _purge_rows(w: Writes, engine: Any, chat_id: str, ts: str, sessions: list[str]) -> None:
    """One transaction: end its records via their writers (ledgered), delete its rows, tombstone it, retire sessions."""
    marks = ", ".join("?" * len(sessions)) or "NULL"
    task = tasks.of_thread(w, chat_id, ts)
    if task is not None and task.state not in tasks.ENDED:
        tasks.move(w, task, state="cancelled", actor=ledger.FOUNDER)     # its timers and leases go with it
    for timer in timers.addressed(w, chat_id=chat_id, role="thread", thread_ts=ts):
        timers.end(w, timer, state="cancelled", reason="its thread was deleted", actor=ledger.FOUNDER)
    for session_id in sessions:
        for lease in leases.of_session(w, session_id):
            leases.release(w, lease, why="thread_deleted", actor=ledger.FOUNDER)
    for (item,) in w.execute(f"SELECT id FROM review_items WHERE state = 'open' AND (session_id IN ({marks}) OR "
                             "(chat_id = ? AND thread_ts = ?))", (*sessions, chat_id, ts)).fetchall():
        engine.desk.withdraw(w, item, DELETED)
    # The root's own events, on the timeline.
    w.execute("DELETE FROM events WHERE chat_id = ? AND thread_ts IS NULL AND type IN ('message', 'message.updated', "
              "'reaction') AND (json_extract(data, '$.id') = ? OR json_extract(data, '$.message_id') = ?)",
              (chat_id, ts, ts))
    w.execute("DELETE FROM messages WHERE chat_id = ? AND (thread_ts = ? OR id = ?)", (chat_id, ts, ts))
    w.execute("DELETE FROM router_decisions WHERE chat_id = ? AND message_id = ?", (chat_id, ts))
    for table in ("cards", "made"):
        w.execute(f"DELETE FROM {table} WHERE chat_id = ? AND thread_ts = ?", (chat_id, ts))
    w.execute(f"DELETE FROM hops WHERE (chat_id = ? AND thread_ts = ?) OR from_session IN ({marks}) OR to_session "
              f"IN ({marks})", (chat_id, ts, *sessions, *sessions))
    w.execute("DELETE FROM thread_git WHERE chat_id = ? AND thread_ts = ?", (chat_id, ts))
    w.execute("DELETE FROM intelligence WHERE (scope = 'thread' AND scope_id = ?) OR (scope = 'session' AND "
              f"scope_id IN ({marks}))", (thread_scope(chat_id, ts), *sessions))
    w.execute(f"DELETE FROM loop_breaker WHERE scope = ? OR scope IN ({marks})",
              (f"thread:{chat_id}:{ts}", *(f"session:{s}" for s in sessions)))
    for table in ("wakes", "grants", "comms_tokens", "integrity_baselines"):      # its runs stay (what its turns did)
        w.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", sessions)
    w.execute("UPDATE threads SET title = NULL, label = NULL, status_md = NULL, outputs = '[]', worktrees_seen = NULL, "
              "summary = NULL, summary_at = NULL, purged_at = ? WHERE chat_id = ? AND ts = ?", (w.now, chat_id, ts))
    for session_id, status, epoch in w.execute(
            f"UPDATE sessions SET retired_at = ?, updated_at = ? WHERE id IN ({marks}) AND retired_at IS NULL "
            "RETURNING id, status, epoch", (w.now, w.now, *sessions)).fetchall():
        w.append("session", {"status": status, "reason": "retired", "why": DELETED}, session_id=session_id,
                 chat_id=chat_id, thread_ts=ts, epoch=epoch)


def _purge_files(chat: Mapping[str, Any] | None, ts: str, tree_path: str | None, att_ids: set[str]) -> None:
    """Remove a purged thread's unnamed attachments and Made copies on disk; failures are logged, raw logs stay."""
    if chat is not None and att_ids:
        try:
            attachments.forget(attachments.chat_folder(chat), att_ids)
        except (ServiceError, OSError) as exc:
            log.warning("storm chats: a purged thread's attachments weren't removed: %s", exc)
    where = trees.thread_of(Path(tree_path)) if tree_path else None
    if where is not None:
        from services.cowork_agent.storm.made import MADE_DIRNAME
        copies = where[0] / XO_DIRNAME / MADE_DIRNAME / f"{trees.TREE_PREFIX}{ts}"
        if copies.is_dir() and not copies.is_symlink():
            shutil.rmtree(copies, ignore_errors=True)
