"""Storm's worktrees in the engine's life (design ch 13 §2, §4–§6).

``Worktrees`` is three things the engine runs:

* the engine's ``Preparer``: before a thread's process starts, its tree is
  ensured (made, reused, or brought back after a removal), and the thread's
  record gains its ``worktree_path``, ``branch`` and ``base_commit`` in one
  transaction with a ``thread`` event ``{ts, **changed columns}``. The
  session's first input says where it works: the tree and its branch, or,
  in a project that isn't a git repository, the project folder. A tree XO
  must not make (a symlinked ``.xo``, a folder in the way) leaves the session
  cold until the person's next message, saying why; a git failure is retried;
* a ``Listener``: at each thread turn's end, the project's worktrees are
  compared with what the thread has seen, and a new one a CLI made on its
  own gets a ``worktree.reported {path}`` event, once (§6). XO never touches
  those;
* the daily sweep (§5): a thread resolved more than 7 days ago whose session
  is cold loses its tree, when nothing would be lost; otherwise the tree is
  kept and the thread says why (``worktree.kept {reason}``).

The person's own removal (``remove``) and the listing for the API are here
too. Every removal holds the tree's lock, so a thread reopened meanwhile
waits for it and then gets its tree back.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import Engine, EngineUnavailable, Wake, Writes
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.engine.session import Meta
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.worktrees import trees
from services.cowork_agent.storm.worktrees.tracking import FILE_SETTLE_S, Tracker
from services.errors import ServiceError
from services.periodic import run_forever
from services.project_files import events as file_events
from services.xo_structure import WORKTREES_DIRNAME, XO_DIRNAME

log = logging.getLogger(__name__)

#: How long a resolved thread keeps its tree: a reply reopens it and finds
#: the tree as it was (§4.1).
GRACE_S = 7 * 24 * 3600
#: The sweep runs daily, its first run an hour after the engine starts, so
#: it never adds to a boot under load (§5).
SWEEP_INTERVAL_S = 24 * 3600
SWEEP_FIRST_S = 3600
_THREAD_COLUMNS = ("worktree_path", "branch", "base_commit")
#: How much of a commit's sha the agent is shown.
SHORT_SHA = 12
#: The listing's largest page. Paging is opt-in: without ``limit`` every thread is listed (a client that
#: never reads ``next_cursor`` must not lose rows); with it, pages run newest first.
PAGE_MAX = 500
#: How long the person's check of one tree may take; past it, the last stored row is answered.
CHECK_DEADLINE_S = 10.0
NOT_CHECKED = "Not read yet: XO reads it in the background shortly."

_running: "Worktrees | None" = None


class WouldLoseWork(ServiceError):
    """The person asked to remove a tree that holds work: what would be
    lost, and the confirmation that forces it."""

    def __init__(self, what: dict[str, Any], confirm: str) -> None:
        super().__init__("would_lose_work", f"Removing it would lose {what['summary']}. To remove it anyway, send "
                                            f"the same request with ?confirm={confirm}.", status=409)
        self.what, self.confirm = what, confirm


class NotFound(ServiceError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status=404)


class CheckTimeout(ServiceError):
    """The person's check of a tree took longer than ``CHECK_DEADLINE_S``:
    the last stored ``row`` instead (the check goes on and is stored)."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        super().__init__("check_timeout", f"Reading this worktree took over {CHECK_DEADLINE_S:g} s; this is what was "
                                          "stored last. The check goes on, and its result arrives as a thread.git "
                                          "event.", status=504)
        self.row = row


def current() -> "Worktrees":
    """The running worktrees (503 when this server runs no engine)."""
    storm_engine.current()
    if _running is None:
        raise EngineUnavailable("worktrees_unavailable", "Storm's worktrees aren't running in this server yet; "
                                                         "retry shortly.")
    return _running


class Worktrees:
    def __init__(self) -> None:
        self.engine: Engine | None = None
        self.tracker: Tracker | None = None
        #: Called with (chat, thread ts, tree) before a tree is removed, the
        #: tree's lock held (Made copies what the thread shared).
        self.before_removal: list[Callable[[str, str, Path], Awaitable[None]]] = []
        self._tasks: set[asyncio.Task] = set()
        self._seen_locks: dict[str, asyncio.Lock] = {}
        self._sweep: asyncio.Task | None = None

    # ── life ─────────────────────────────────────────────────────────────

    def start(self, engine: Engine) -> None:
        """Once the engine runs: hear its turn ends, and start the daily sweep."""
        global _running
        self.engine = engine
        from services.cowork_agent.storm import stream
        self.tracker = Tracker(engine)
        self.tracker.streams = stream.open_streams
        self.tracker.start()
        engine.subscribe(self)
        file_events.subscribe(self._files_changed)
        self._sweep = asyncio.create_task(run_forever("storm worktree sweep", self.sweep,
                                                      interval_s=lambda: SWEEP_INTERVAL_S,
                                                      startup_delay_s=SWEEP_FIRST_S, logger=log),
                                          name="storm-worktree-sweep")
        _running = self

    async def stop(self) -> None:
        global _running
        if _running is self:
            _running = None
        file_events.unsubscribe(self._files_changed)
        if self.engine is not None:
            self.engine.unsubscribe(self)
        if self.tracker is not None:
            await self.tracker.stop()
        tasks = [t for t in (self._sweep, *self._tasks) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── the engine's preparer ────────────────────────────────────────────

    async def prepare(self, engine: Engine, meta: Meta, spec: c.SessionSpec) -> None:
        if meta.role != "thread" or not meta.chat_id or not meta.thread_ts:
            return
        where = trees.thread_of(Path(spec.cwd))
        if where is None:                   # a thread runs in its tree or, outside git, in the project folder
            await _tell_where_once(engine, meta, trees.WHERE_NON_GIT_NOTE.format(path=spec.cwd))
            return
        project, ts = where
        if ts != meta.thread_ts:
            raise _refused(f"This thread's folder {spec.cwd} belongs to thread {ts}, not {meta.thread_ts}; XO "
                           "won't run it there.")
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT worktree_path, branch, base_commit, worktrees_seen IS NULL FROM threads WHERE chat_id = ? "
            "AND ts = ?", (meta.chat_id, ts)).fetchone())
        if row is None:
            raise _refused(f"Thread {ts} has no record in chat {meta.chat_id}, so its worktree can't be checked "
                           "as XO's.")
        recorded = dict(zip(_THREAD_COLUMNS, row[:3]))
        if recorded["worktree_path"] and os.path.normpath(recorded["worktree_path"]) != os.path.normpath(spec.cwd):
            raise _refused(f"This thread's record names {recorded['worktree_path']} as its worktree, not "
                           f"{spec.cwd}; XO won't guess which is right.")
        try:
            tree = await trees.ensure(project, ts, chat_id=meta.chat_id, branch=recorded["branch"],
                                      base_commit=recorded["base_commit"])
        except trees.WorktreeRefused as exc:
            raise _refused(exc.message) from exc
        seen = await trees.cli_worktrees(project) if row[3] else None      # the first baseline
        now = {"worktree_path": str(tree.path), "branch": tree.branch, "base_commit": tree.base_commit}
        changed = {k: v for k, v in now.items() if recorded[k] != v}
        if not changed and seen is None and tree.note is None:
            return
        first = recorded["worktree_path"] is None

        def record(w: Writes) -> None:
            if changed:
                w.execute(f"UPDATE threads SET {', '.join(f'{k} = ?' for k in changed)}, updated_at = ? "
                          "WHERE chat_id = ? AND ts = ?", (*changed.values(), w.now, meta.chat_id, ts))
                w.append("thread", {"ts": ts, **changed}, chat_id=meta.chat_id, thread_ts=ts)
            if seen is not None:
                w.execute("UPDATE threads SET worktrees_seen = json(?) WHERE chat_id = ? AND ts = ?",
                          (json.dumps(seen), meta.chat_id, ts))
            if tree.note is not None:       # the tree came back after a removal: the agent is told how
                w.wake(meta.id, Wake(id=f"worktree:{uuid.uuid4().hex}", text=tree.note, priority="now",
                                     reason="worktree", origin="system"))
            if first:
                w.wake(meta.id, _where(meta, trees.WHERE_NOTE.format(path=tree.path, branch=tree.branch,
                                                                     base=tree.base_commit[:SHORT_SHA])))
        await engine.write(record)

    # ── worktrees a CLI made on its own (§6) ─────────────────────────────

    def committed(self, events: Sequence[StoredEvent]) -> None:
        """A thread's git state is read again when it can have changed: its
        turn ended, it was resolved or reopened, its tree came or went."""
        for event in events:
            if not (event.chat_id and event.thread_ts) or self.tracker is None:
                continue
            if event.type == "turn.finished":
                self._spawn(self._look(event.chat_id, event.thread_ts))
                self.tracker.mark(event.chat_id, event.thread_ts)
            elif event.type == "thread" and ("state" in event.data or "worktree_path" in event.data):
                self.tracker.mark(event.chat_id, event.thread_ts, fresh="worktree_path" in event.data)
            elif event.type in ("worktree.removed", "worktree.restored"):
                self.tracker.mark(event.chat_id, event.thread_ts, fresh=True)

    async def _files_changed(self, batch: list[file_events.Event]) -> None:
        """A file changed in a thread's tree (Files, a shell, an editor): its
        tree is read again once the writes settle."""
        if self.engine is None or self.tracker is None:
            return
        touched = sorted({(project, data["worktree"]) for type_, project, data in batch
                          if type_ == file_events.VERSION and isinstance(data.get("worktree"), str)
                          and data["worktree"].startswith(trees.TREE_PREFIX)})
        if not touched:
            return

        def read(conn) -> list[tuple[str, str]]:
            found = []
            for project, folder in touched:
                ts = folder[len(trees.TREE_PREFIX):]
                tail = _like(f"{project}/{XO_DIRNAME}/{WORKTREES_DIRNAME}/{folder}")
                found += conn.execute(
                    "SELECT chat_id, ts FROM threads WHERE ts = ? AND deleted_at IS NULL AND worktree_path LIKE ? "
                    "ESCAPE '\\'", (ts, "%/" + tail)).fetchall()
            return found
        for chat_id, ts in await self.engine.store.read(read):
            self.tracker.mark(chat_id, ts, settle=FILE_SETTLE_S)

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        """Nothing live matters here."""

    async def _look(self, chat_id: str, ts: str) -> None:
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT worktree_path FROM threads WHERE chat_id = ? AND ts = ?", (chat_id, ts)).fetchone())
        where = trees.thread_of(Path(row[0])) if row and row[0] else None
        if where is None:
            return                          # a non-git project: no worktrees to compare
        async with self._seen_locks.setdefault(chat_id, asyncio.Lock()):
            listed = await trees.cli_worktrees(where[0])

            def record(w: Writes) -> None:
                (seen,) = w.execute("SELECT worktrees_seen FROM threads WHERE chat_id = ? AND ts = ?",
                                    (chat_id, ts)).fetchone()
                new = [p for p in listed if seen is not None and p not in json.loads(seen)]
                for path in new:
                    w.append("worktree.reported", {"path": path}, chat_id=chat_id, thread_ts=ts)
                w.execute("UPDATE threads SET worktrees_seen = json(?) WHERE chat_id = ? AND ts = ?",
                          (json.dumps(listed), chat_id, ts))
                if new:                     # reported once: the chat's other threads take them as seen
                    for other, theirs in w.execute(
                            "SELECT ts, worktrees_seen FROM threads WHERE chat_id = ? AND ts <> ? AND "
                            "worktrees_seen IS NOT NULL", (chat_id, ts)).fetchall():
                        w.execute("UPDATE threads SET worktrees_seen = json(?) WHERE chat_id = ? AND ts = ?",
                                  (json.dumps(sorted({*json.loads(theirs), *new})), chat_id, other))
            await engine.write(record)

    # ── the daily sweep (§5) ─────────────────────────────────────────────

    async def sweep(self) -> None:
        """XO's trees whose thread was resolved more than ``GRACE_S`` ago, and
        whose session is cold, are removed when nothing would be lost; the
        others are kept, saying why. Candidates come from the store only, and
        a tree is touched only when its marker and record agree."""
        engine = self._engine()
        cutoff = iso(engine.clock.time() - GRACE_S)
        rows = await engine.store.read(lambda conn: conn.execute(
            "SELECT chat_id, ts, session_id, worktree_path, branch FROM threads WHERE resolved_at IS NOT NULL AND "
            "resolved_at < ? AND worktree_path IS NOT NULL ORDER BY resolved_at", (cutoff,)).fetchall())
        for project, candidates in _by_project(rows).items():
            try:
                snap = await trees.snapshot(project)
            except (ServiceError, OSError) as exc:
                log.warning("storm worktrees: the sweep skips %s: %s", project, getattr(exc, "message", exc))
                continue
            present = await asyncio.to_thread(lambda: [os.path.lexists(row[3]) or snap.entry(Path(row[3])) is not None
                                                       for row in candidates])
            for (chat_id, ts, _session, path, _branch), there in zip(candidates, present):
                if not there:
                    continue                # removed already
                try:
                    await self._sweep_one(chat_id, ts, Path(path), cutoff)
                except (ServiceError, OSError) as exc:
                    log.warning("storm worktrees: the sweep skips %s: %s", path, getattr(exc, "message", exc))

    async def _sweep_one(self, chat_id: str, ts: str, path: Path, cutoff: str) -> None:
        engine = self._engine()
        project, named = trees.thread_of(path)              # a tree path: _by_project kept only those
        if named != ts:
            return
        async with trees.lock_for(path):
            # Read again under the lock: a reply may have reopened the thread meanwhile, its session still cold.
            row = await engine.store.read(lambda conn: conn.execute(
                "SELECT session_id, branch FROM threads WHERE chat_id = ? AND ts = ? AND resolved_at IS NOT NULL AND "
                "resolved_at < ? AND worktree_path = ?", (chat_id, ts, cutoff, str(path))).fetchone())
            if row is None:
                return
            session_id, branch = row
            if not await self._cold(session_id):
                return                      # live trees are never touched
            found = await trees.find(project, path)
            if not found.ours(chat_id, ts):
                log.info("storm worktrees: the sweep leaves %s alone: its marker and its thread's record "
                         "don't agree", path)
                return
            lost = await trees.loss(found, branch) if found.checkout else trees.Loss()
            if lost.empty:
                await self._tracked().refresh(chat_id, ts, pr=False)    # its branch may go with it
                await self._before_removal(chat_id, ts, path)
                try:
                    note = await trees.remove(project, found, branch)
                except trees.Kept as exc:
                    await self._kept(chat_id, ts, exc.message)
                    return
                await engine.write(lambda w: _removed(w, chat_id, ts, by="sweep", note=note))
                await self._tracked().refresh(chat_id, ts, pr=False)
                return
            self._tracked().mark(chat_id, ts)            # the stored state may not know yet
            await self._kept(chat_id, ts, f"Kept: {lost.summary()}")

    async def _kept(self, chat_id: str, ts: str, reason: str) -> None:
        """The thread says why its tree stays, once per reason."""
        def record(w: Writes) -> None:
            last = w.execute("SELECT json_extract(data, '$.reason') FROM events WHERE chat_id = ? AND thread_ts = ? "
                             "AND type = 'worktree.kept' ORDER BY seq DESC LIMIT 1", (chat_id, ts)).fetchone()
            if last is None or last[0] != reason:
                w.append("worktree.kept", {"reason": reason}, chat_id=chat_id, thread_ts=ts)
        await self._engine().write(record)

    # ── the person's view and removal (the API) ──────────────────────────

    async def listing(self, *, chat: str | None = None, limit: int | None = None,
                      cursor: str | None = None) -> dict[str, Any]:
        """XO's trees that are still there, each with what removing it would
        lose and its branch's git state; the threads whose tree was removed,
        with the git state last read (``removed``); and the worktrees CLIs
        made that were reported and still exist. Read from the store only
        (``tracking`` keeps it): no git runs here. Newest thread first.
        Every thread without ``limit``; with it, at most ``limit`` threads
        (trees and removed together) and the ``cursor`` of the next, older
        page (``next_cursor``, None on the last page)."""
        engine = self._engine()
        self._tracked().looked()
        after = _cursor(cursor)

        def read(conn) -> tuple[list, list, bool, tuple | None]:
            rows = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM threads t LEFT JOIN sessions s ON s.id = t.session_id "
                "LEFT JOIN thread_git g ON g.chat_id = t.chat_id AND g.thread_ts = t.ts "
                "WHERE t.worktree_path IS NOT NULL AND t.deleted_at IS NULL AND (:chat IS NULL OR t.chat_id = :chat) "
                "AND (:at IS NULL OR (t.created_at, t.chat_id, t.ts) < (:at, :c, :t)) "
                "ORDER BY t.created_at DESC, t.chat_id DESC, t.ts DESC LIMIT :n",
                {"chat": chat, "at": after[0], "c": after[1], "t": after[2],
                 "n": -1 if limit is None else limit + 1}).fetchall()
            more = limit is not None and len(rows) > limit
            rows = rows[:limit]
            reported = conn.execute(
                "SELECT seq, chat_id, thread_ts, json_extract(data, '$.path'), ts FROM events WHERE "
                "type = 'worktree.reported' AND (:chat IS NULL OR chat_id = :chat) ORDER BY seq DESC",
                {"chat": chat}
            ).fetchall() if after[0] is None else []
            removed = _removed_at(conn, chat)
            return [_stored_row(conn, row, removed) for row in rows], reported, more, rows[-1] if rows else None
        rows, reported, more, last = await engine.store.read(read)
        out = [r for kind, r in rows if kind == "tree"]
        gone = [r for kind, r in rows if kind == "removed"]
        present = await asyncio.to_thread(lambda: [os.path.lexists(r[3]) for r in reported])
        return {"worktrees": out, "removed": gone,
                "reported": [{"seq": seq, "chat_id": chat_id, "thread_ts": ts, "path": path, "reported_at": when,
                              "managed": False}
                             for (seq, chat_id, ts, path, when), there in zip(reported, present) if there],
                "next_cursor": _cursor_of(last) if more and last is not None else None}

    async def check(self, chat_id: str, ts: str) -> dict[str, Any]:
        """The person's check of one thread's tree: read now (both halves),
        stored, and answered as its listing row.
        Past ``CHECK_DEADLINE_S``: ``CheckTimeout`` with the last stored row."""
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT worktree_path, deleted_at FROM threads WHERE chat_id = ? AND ts = ?", (chat_id, ts)).fetchone())
        if row is None or row[1] is not None:
            raise NotFound("thread_not_found", f"There is no thread {ts} in chat {chat_id}.")
        if not row[0] or trees.thread_of(Path(row[0])) is None:
            raise NotFound("no_worktree", f"Thread {ts} has no worktree (its project isn't a git repository).")
        try:
            await asyncio.wait_for(self._tracked().check(chat_id, ts), timeout=CHECK_DEADLINE_S)
        except asyncio.TimeoutError:
            raise CheckTimeout(await self._stored(chat_id, ts)) from None
        return await self._stored(chat_id, ts)

    async def _stored(self, chat_id: str, ts: str) -> dict[str, Any] | None:
        """One thread's listing row, as stored."""
        def read(conn) -> dict[str, Any] | None:
            row = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM threads t LEFT JOIN sessions s ON s.id = t.session_id "
                "LEFT JOIN thread_git g ON g.chat_id = t.chat_id AND g.thread_ts = t.ts "
                "WHERE t.chat_id = ? AND t.ts = ? AND t.worktree_path IS NOT NULL", (chat_id, ts)).fetchone()
            return _stored_row(conn, row, _removed_at(conn, chat_id, ts))[1] if row else None
        return await self._engine().store.read(read)

    async def remove(self, chat_id: str, ts: str, *, confirm: str | None = None) -> dict[str, Any]:
        """The person removes a thread's tree (§4.3, call 3). Nothing would be
        lost: removed as the sweep would. Something would: ``WouldLoseWork``
        says exactly what, with a confirmation; the same request with that
        confirmation removes it with ``--force`` and ``branch -D`` and records
        what was lost. A changed tree gets a new confirmation."""
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT session_id, worktree_path, branch, deleted_at FROM threads WHERE chat_id = ? AND ts = ?",
            (chat_id, ts)).fetchone())
        if row is None or row[3] is not None:
            raise NotFound("thread_not_found", f"There is no thread {ts} in chat {chat_id}.")
        session_id, path, branch, _deleted = row
        if not path:
            raise NotFound("no_worktree", f"Thread {ts} has no worktree (its project isn't a git repository).")
        path = Path(path)
        where = trees.thread_of(path)
        if where is None or where[1] != ts:
            raise ServiceError("not_xo_worktree", f"Thread {ts}'s record names {path}, which isn't an XO thread "
                                                  "worktree, so XO leaves it alone.", status=409)
        project = where[0]
        async with trees.lock_for(path):
            if not await self._cold(session_id):
                raise ServiceError("session_busy", "This thread's session is still running or holds its process; "
                                                   "stop it, or wait until it is released (10 minutes after its "
                                                   "last turn), then try again.", status=409)
            found = await trees.find(project, path)
            if not found.registered and not found.exists:
                raise NotFound("no_worktree", f"Thread {ts}'s worktree was removed already.")
            if not found.ours(chat_id, ts):
                raise ServiceError("not_xo_worktree", f"{path} isn't XO's worktree for this thread (its marker and "
                                                      "the thread's record disagree), so XO leaves it alone; remove "
                                                      "it with git if you mean to.", status=409)
            lost = await trees.loss(found, branch) if found.checkout else trees.Loss()
            forced = not lost.empty
            if forced:
                what = lost.what()
                expected = trees.confirmation(what)
                if confirm != expected:
                    self._tracked().mark(chat_id, ts)    # the listing shows what was found: the same counts
                    raise WouldLoseWork(what, expected)
            await self._tracked().refresh(chat_id, ts, pr=False)        # its branch may go with it
            await self._before_removal(chat_id, ts, path)
            try:
                note = await trees.remove(project, found, branch, force=forced)
            except trees.Kept as exc:
                raise ServiceError("removal_refused", f"git kept it: {exc.reason}. Look at the worktree with git, "
                                                      "then try again.", status=409) from exc
            lost_what = lost.what() if forced else None
            await engine.write(lambda w: _removed(w, chat_id, ts, by="person", forced=forced, lost=lost_what,
                                                  note=note))
        await self._tracked().refresh(chat_id, ts, pr=False)
        return {"removed": True, "forced": forced, "lost": lost_what, "note": note}

    # ── a deleted thread's tree (chats/deletion.py) ──────────────────────

    async def deletion_loss(self, chat_id: str, ts: str) -> trees.Loss | None:
        """What removing the folder would lose, branch kept; None with no tree; 409 ``not_xo_worktree`` if not XO's."""
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT worktree_path FROM threads WHERE chat_id = ? AND ts = ?", (chat_id, ts)).fetchone())
        located = _located(chat_id, ts, row[0] if row else None)
        if located is None:
            return None
        project, path = located
        found = await trees.find(project, path)
        if not found.registered and not found.exists:
            return None
        _check_ours(found, chat_id, ts)
        return await trees.loss(found, None, keep_branch=True) if found.checkout else trees.Loss()

    async def remove_keeping_branch(self, chat_id: str, ts: str, session_id: str | None, *,
                                    accept: Callable[[trees.Loss], bool]) -> dict[str, Any] | None:
        """Remove a deleted thread's folder under its lock, never its branch; ``WouldLoseWork`` unless ``accept``."""
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT worktree_path, branch FROM threads WHERE chat_id = ? AND ts = ?", (chat_id, ts)).fetchone())
        located = _located(chat_id, ts, row[0] if row else None)
        if located is None:
            return None
        project, path = located
        async with trees.lock_for(path):
            if not await self.let_go(session_id):
                raise ServiceError("session_busy", "This thread's session still holds a process after it was "
                                                   "stopped; nothing was deleted. Try again in a moment.",
                                   status=409)
            found = await trees.find(project, path)
            if not found.registered and not found.exists:
                return None
            _check_ours(found, chat_id, ts)
            lost = await trees.loss(found, None, keep_branch=True) if found.checkout else trees.Loss()
            if not lost.empty and not accept(lost):
                what = lost.what()
                raise WouldLoseWork(what, trees.confirmation(what))
            forced = not lost.empty
            await self._before_removal(chat_id, ts, path)
            try:
                note = await trees.remove(project, found, None, force=forced)       # None: every branch stays
            except trees.Kept as exc:
                raise ServiceError("removal_refused", f"git kept the worktree: {exc.reason}. Nothing was deleted; "
                                                      "look at it with git, then try again.", status=409) from exc
            lost_what = lost.what() if forced else None
            await engine.write(lambda w: _removed(w, chat_id, ts, by="delete", forced=forced, lost=lost_what,
                                                  note=note))
        return {"path": str(path), "branch": row[1], "forced": forced, "lost": lost_what}

    async def bring_back(self, chat_id: str, ts: str) -> trees.Tree | None:
        """Recreate a restored thread's tree from its kept branch (or HEAD, new branch); None if it can't be made."""
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT worktree_path, branch, base_commit FROM threads WHERE chat_id = ? AND ts = ?",
            (chat_id, ts)).fetchone())
        located = _located(chat_id, ts, row[0] if row else None)
        if located is None:
            return None
        project, _path = located
        recorded = dict(zip(_THREAD_COLUMNS, row))
        try:
            tree = await trees.ensure(project, ts, chat_id=chat_id, branch=recorded["branch"],
                                      base_commit=recorded["base_commit"])
        except (ServiceError, OSError) as exc:
            log.warning("storm worktrees: thread %s's worktree couldn't be made again on its restore: %s", ts,
                        getattr(exc, "message", exc))
            return None
        now = {"worktree_path": str(tree.path), "branch": tree.branch, "base_commit": tree.base_commit}
        changed = {k: v for k, v in now.items() if recorded[k] != v}

        def record(w: Writes) -> None:
            if changed:
                w.execute(f"UPDATE threads SET {', '.join(f'{k} = ?' for k in changed)}, updated_at = ? "
                          "WHERE chat_id = ? AND ts = ?", (*changed.values(), w.now, chat_id, ts))
                w.append("thread", {"ts": ts, **changed}, chat_id=chat_id, thread_ts=ts)
            w.append("worktree.restored", {"path": str(tree.path), "branch": tree.branch}, chat_id=chat_id,
                     thread_ts=ts)
        await engine.write(record)
        return tree

    # ── plumbing ─────────────────────────────────────────────────────────

    async def let_go(self, session_id: str | None) -> bool:
        """Whether a paused, halted session holds nothing in its tree: no process, none starting, none registered."""
        if session_id is None:
            return True
        engine = self._engine()
        if (await engine.get_session(session_id))["state"] not in ("cold", "queued") or engine.active([session_id]):
            return False
        return await engine.store.read(lambda conn: conn.execute(
            "SELECT 1 FROM procs WHERE session_id = ? LIMIT 1", (session_id,)).fetchone()) is None

    async def _cold(self, session_id: str | None) -> bool:
        """No process, no running turn, no held call (ch 07 §2), and no
        process in the registry (one boot recovery couldn't stop may still
        run in the tree)."""
        if session_id is None:
            return True
        engine = self._engine()
        if (await engine.get_session(session_id))["state"] != "cold":
            return False
        return await engine.store.read(lambda conn: conn.execute(
            "SELECT 1 FROM procs WHERE session_id = ? LIMIT 1", (session_id,)).fetchone()) is None

    async def _before_removal(self, chat_id: str, ts: str, path: Path) -> None:
        for hook in self.before_removal:
            try:
                await hook(chat_id, ts, path)
            except Exception:  # noqa: BLE001 - a hook never stops a removal; what it couldn't do is logged
                log.exception("storm worktrees: a before-removal step for %s failed", path)

    def _tracked(self) -> Tracker:
        if self.tracker is None:
            raise EngineUnavailable("worktrees_unavailable", "Storm's worktrees aren't running in this server yet; "
                                                             "retry shortly.")
        return self.tracker

    def _engine(self) -> Engine:
        if self.engine is None:
            raise EngineUnavailable("worktrees_unavailable", "Storm's worktrees aren't running in this server yet; "
                                                             "retry shortly.")
        return self.engine

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm worktrees: a background step failed", exc_info=task.exception())


def _where(meta: Meta, text: str) -> Wake:
    """Where the thread works, for its first input (its id is the key, so a
    retried start queues it once). A git thread is told once its tree is
    first recorded; ``_tell_where_once`` keys a non-git one on its turns."""
    return Wake(id=f"where:{meta.id}", text=text, priority="next", reason="worktree", origin="system")


async def _tell_where_once(engine: Engine, meta: Meta, text: str) -> None:
    """Only before the session's first turn: its turns are kept, while a
    consumed wake is pruned after a while (so its row can't say "once")."""
    def ran(conn) -> bool:
        return conn.execute("SELECT 1 FROM turns WHERE session_id = ? LIMIT 1", (meta.id,)).fetchone() is not None
    if await engine.store.read(ran):
        return

    def tell(w: Writes) -> None:
        if not ran(w):
            w.wake(meta.id, _where(meta, text))
    await engine.write(tell)


def _refused(message: str) -> c.RuntimeFailure:
    """Nothing to retry: the session waits for the person, saying why."""
    return c.RuntimeFailure("spec_refused", message, retryable=False)


def _removed(w: Writes, chat_id: str, ts: str, *, by: str, forced: bool = False, lost: dict | None = None,
             note: str | None = None) -> None:
    data: dict[str, Any] = {"by": by, "forced": forced}
    if forced:
        data["lost"] = lost
    if note:
        data["note"] = note
    w.append("worktree.removed", data, chat_id=chat_id, thread_ts=ts)


def _located(chat_id: str, ts: str, path: str | None) -> tuple[Path, Path] | None:
    """``(project, tree path)`` for a thread's recorded tree, or None; 409 ``not_xo_worktree`` if not its XO tree."""
    if not path:
        return None
    where = trees.thread_of(Path(path))
    if where is None or where[1] != ts:
        raise ServiceError("not_xo_worktree", f"Thread {ts}'s record names {path}, which isn't an XO thread "
                                              "worktree, so XO leaves it alone.", status=409)
    return where[0], Path(path)


def _check_ours(found: trees.Found, chat_id: str, ts: str) -> None:
    if not found.ours(chat_id, ts):
        raise ServiceError("not_xo_worktree", f"{found.path} isn't XO's worktree for this thread (its marker and "
                                              "the thread's record disagree), so XO leaves it alone; remove it with "
                                              "git if you mean to, then try again.", status=409)


_ROW_COLUMNS = ("t.chat_id, t.ts, t.session_id, t.worktree_path, t.branch, t.base_commit, t.resolved_at, s.status, "
                "g.state, g.checked_at, g.pr_checked_at, g.tree_detail, t.created_at")


def _removed_at(conn, chat: str | None, ts: str | None = None) -> dict[tuple[str, str], tuple[str, str | None]]:
    """``{(chat, ts): (when, by)}`` of the last removal of each thread's tree:
    one pass over that event type's index (``+`` keeps SQLite off the chat
    index, which would walk every event of the chat)."""
    return {(c, t): (when, by) for c, t, when, by in conn.execute(
        "SELECT chat_id, thread_ts, max(ts), json_extract(data, '$.by') FROM events WHERE type = 'worktree.removed' "
        "AND (:chat IS NULL OR +chat_id = :chat) AND (:ts IS NULL OR +thread_ts = :ts) GROUP BY chat_id, thread_ts",
        {"chat": chat, "ts": ts})}


def _stored_row(conn, row: tuple, removed: dict[tuple[str, str], tuple[str, str | None]]
                ) -> tuple[str, dict[str, Any]]:
    """``("tree" | "removed", the listing's row)`` from a thread's stored
    state: a tree not read yet lists as there, with ``error`` saying so."""
    from services.cowork_agent.storm.chats.rows import get_thread
    (chat_id, ts, session_id, path, branch, base, resolved_at, session_state, state, checked_at, pr_checked_at,
     detail, _created) = row
    stored = json.loads(state) if state is not None else None
    git_view = None if stored is None else {**stored, "checked_at": checked_at, "pr_checked_at": pr_checked_at}
    thread_state = (get_thread(conn, chat_id, ts) or {}).get("state")
    tree = (stored or {}).get("tree")
    where = json.loads(detail) if detail else {}
    common = {"chat_id": chat_id, "thread_ts": ts, "path": path, "branch": branch, "base_commit": base,
              "thread_state": thread_state, "resolved_at": resolved_at, "git": git_view}
    if tree is not None and not tree.get("exists") and where.get("registered") is False \
            and where.get("folder") is False:
        when, by = removed.get((chat_id, ts), (None, None))
        return "removed", {**common, "removed_at": when, "removed_by": by}
    if tree is None:
        would_lose, error = None, NOT_CHECKED
    else:
        error = tree.get("error")
        would_lose = (None if error is not None or tree.get("would_lose") is None
                      else trees.stored_what(tree, where, branch) if tree["would_lose"] else None)
    xo = bool(tree and tree.get("xo"))
    return "tree", {**common, "session_id": session_id, "session_state": session_state,
                    "exists": bool(tree and tree.get("exists")) if tree is not None else os.path.lexists(path),
                    "xo": xo, "would_lose": would_lose, "error": error,
                    "removable": bool(tree is not None and xo and error is None and tree.get("would_lose") is False
                                      and session_state in (None, "cold")),
                    "checked_at": tree.get("checked_at") if tree else None}


def _cursor_of(row: tuple) -> str:
    """An opaque page cursor: the last row's place in the listing's order."""
    return base64.urlsafe_b64encode(json.dumps([row[-1], row[0], row[1]]).encode("utf-8")).decode("ascii")


def _cursor(cursor: str | None) -> tuple[str | None, str | None, str | None]:
    if not cursor:
        return None, None, None
    try:
        at, chat_id, ts = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        if all(isinstance(v, str) for v in (at, chat_id, ts)):
            return at, chat_id, ts
    except (ValueError, TypeError):
        pass
    raise ServiceError("bad_cursor", "This page cursor isn't one the listing gave; start again without it.")


def _like(text: str) -> str:
    """``text`` matched literally inside a LIKE pattern (escape ``\\``)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _by_project(rows: Iterable[tuple]) -> dict[Path, list[tuple]]:
    """Rows whose 4th column is a thread tree's path, by project folder."""
    groups: dict[Path, list[tuple]] = defaultdict(list)
    for row in rows:
        where = trees.thread_of(Path(row[3]))
        if where is not None:
            groups[where[0]].append(row)
    return groups
