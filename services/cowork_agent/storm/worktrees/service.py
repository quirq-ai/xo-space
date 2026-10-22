"""Storm's worktrees in the engine's life (design ch 13 §2, §4–§6).

``Worktrees`` is three things the engine runs:

* the engine's ``Preparer``: before a thread's process starts, its tree is
  ensured (made, reused, or brought back after a removal), and the thread's
  record gains its ``worktree_path``, ``branch`` and ``base_commit`` in one
  transaction with a ``thread`` event ``{ts, **changed columns}``. A tree XO
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
import json
import logging
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Iterable, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import Engine, EngineUnavailable, Wake, Writes
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.engine.session import Meta
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.worktrees import trees
from services.errors import ServiceError
from services.periodic import run_forever

log = logging.getLogger(__name__)

#: How long a resolved thread keeps its tree: a reply reopens it and finds
#: the tree as it was (§4.1).
GRACE_S = 7 * 24 * 3600
#: The sweep runs daily, its first run an hour after the engine starts, so
#: it never adds to a boot under load (§5).
SWEEP_INTERVAL_S = 24 * 3600
SWEEP_FIRST_S = 3600
_THREAD_COLUMNS = ("worktree_path", "branch", "base_commit")

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
        self._tasks: set[asyncio.Task] = set()
        self._seen_locks: dict[str, asyncio.Lock] = {}
        self._sweep: asyncio.Task | None = None

    # ── life ─────────────────────────────────────────────────────────────

    def start(self, engine: Engine) -> None:
        """Once the engine runs: hear its turn ends, and start the daily sweep."""
        global _running
        self.engine = engine
        engine.subscribe(self)
        self._sweep = asyncio.create_task(run_forever("storm worktree sweep", self.sweep,
                                                      interval_s=lambda: SWEEP_INTERVAL_S,
                                                      startup_delay_s=SWEEP_FIRST_S, logger=log),
                                          name="storm-worktree-sweep")
        _running = self

    async def stop(self) -> None:
        global _running
        if _running is self:
            _running = None
        if self.engine is not None:
            self.engine.unsubscribe(self)
        tasks = [t for t in (self._sweep, *self._tasks) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── the engine's preparer ────────────────────────────────────────────

    async def prepare(self, engine: Engine, meta: Meta, spec: c.SessionSpec) -> None:
        where = trees.thread_of(Path(spec.cwd))
        if where is None or meta.role != "thread" or not meta.chat_id or not meta.thread_ts:
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
        await engine.write(record)

    # ── worktrees a CLI made on its own (§6) ─────────────────────────────

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.type == "turn.finished" and event.chat_id and event.thread_ts:
                self._spawn(self._look(event.chat_id, event.thread_ts))

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
            "SELECT chat_id, ts, session_id, worktree_path, branch FROM threads WHERE state = 'resolved' AND "
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
                "SELECT session_id, branch FROM threads WHERE chat_id = ? AND ts = ? AND state = 'resolved' AND "
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
                try:
                    note = await trees.remove(project, found, branch)
                except trees.Kept as exc:
                    await self._kept(chat_id, ts, exc.message)
                    return
                await engine.write(lambda w: _removed(w, chat_id, ts, by="sweep", note=note))
                return
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

    async def listing(self, *, chat: str | None = None) -> dict[str, Any]:
        """XO's trees that are still there, each with what removing it would
        lose; and the worktrees CLIs made that were reported and still exist."""
        engine = self._engine()

        def read(conn) -> tuple[list, list]:
            rows = conn.execute(
                "SELECT t.chat_id, t.ts, t.session_id, t.worktree_path, t.branch, t.base_commit, t.state, "
                "t.resolved_at, s.status FROM threads t LEFT JOIN sessions s ON s.id = t.session_id "
                "WHERE t.worktree_path IS NOT NULL AND (:chat IS NULL OR t.chat_id = :chat) ORDER BY t.created_at",
                {"chat": chat}).fetchall()
            reported = conn.execute(
                "SELECT seq, chat_id, thread_ts, json_extract(data, '$.path'), ts FROM events WHERE "
                "type = 'worktree.reported' AND (:chat IS NULL OR chat_id = :chat) ORDER BY seq", {"chat": chat}
            ).fetchall()
            return rows, reported
        rows, reported = await engine.store.read(read)
        out: list[dict[str, Any]] = []
        for project, group in _by_project(rows).items():
            try:
                snap = await trees.snapshot(project)
            except (ServiceError, OSError) as exc:
                log.warning("storm worktrees: can't list %s's worktrees: %s", project, getattr(exc, "message", exc))
                continue
            for chat_id, ts, session_id, path, branch, base, state, resolved_at, session_state in group:
                found = await asyncio.to_thread(snap.found, Path(path))
                if not found.registered and not found.exists:
                    continue                # removed
                ours = found.ours(chat_id, ts)
                error = None
                try:
                    lost = await trees.loss(found, branch) if ours and found.checkout else trees.Loss()
                except ServiceError as exc:     # this tree can't be read now; the others still list
                    log.warning("storm worktrees: can't tell what removing %s would lose: %s", path, exc.message)
                    lost, error = None, exc.message
                out.append({
                    "chat_id": chat_id, "thread_ts": ts, "path": path, "branch": branch, "base_commit": base,
                    "thread_state": state, "resolved_at": resolved_at, "session_id": session_id,
                    "session_state": session_state, "exists": found.checkout, "xo": ours,
                    "would_lose": None if lost is None or lost.empty else lost.what(), "error": error,
                    "removable": lost is not None and ours and lost.empty and session_state in (None, "cold"),
                })
        present = await asyncio.to_thread(lambda: [os.path.lexists(r[3]) for r in reported])
        return {"worktrees": out,
                "reported": [{"seq": seq, "chat_id": chat_id, "thread_ts": ts, "path": path, "reported_at": when,
                              "managed": False}
                             for (seq, chat_id, ts, path, when), there in zip(reported, present) if there]}

    async def remove(self, chat_id: str, ts: str, *, confirm: str | None = None) -> dict[str, Any]:
        """The person removes a thread's tree (§4.3, call 3). Nothing would be
        lost: removed as the sweep would. Something would: ``WouldLoseWork``
        says exactly what, with a confirmation; the same request with that
        confirmation removes it with ``--force`` and ``branch -D`` and records
        what was lost. A changed tree gets a new confirmation."""
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT session_id, worktree_path, branch FROM threads WHERE chat_id = ? AND ts = ?",
            (chat_id, ts)).fetchone())
        if row is None:
            raise NotFound("thread_not_found", f"There is no thread {ts} in chat {chat_id}.")
        session_id, path, branch = row
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
                    raise WouldLoseWork(what, expected)
            try:
                note = await trees.remove(project, found, branch, force=forced)
            except trees.Kept as exc:
                raise ServiceError("removal_refused", f"git kept it: {exc.reason}. Look at the worktree with git, "
                                                      "then try again.", status=409) from exc
            lost_what = lost.what() if forced else None
            await engine.write(lambda w: _removed(w, chat_id, ts, by="person", forced=forced, lost=lost_what,
                                                  note=note))
        return {"removed": True, "forced": forced, "lost": lost_what, "note": note}

    # ── plumbing ─────────────────────────────────────────────────────────

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


def _by_project(rows: Iterable[tuple]) -> dict[Path, list[tuple]]:
    """Rows whose 4th column is a thread tree's path, by project folder."""
    groups: dict[Path, list[tuple]] = defaultdict(list)
    for row in rows:
        where = trees.thread_of(Path(row[3]))
        if where is not None:
            groups[where[0]].append(row)
    return groups
