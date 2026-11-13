"""Keeping each thread branch's git state (``state.py``) in the store, so a
thread says what became of its work after its worktree and branch are gone.

A thread's state is read again:

* when one of its turns ends, and when it is resolved or reopened (the
  ``Worktrees`` listener hears both);
* before its worktree is removed, and after (the branch may go with it);
* when the person lists worktrees and what is stored is older than
  ``FRESH_S`` (``refresh_stale``: the listing waits ``LIST_BUDGET_S`` at
  most, the rest lands a moment later as ``thread.git`` events).

Its pull request is asked of the forge less often (``pr_due``): every
``PR_OPEN_S`` while it could still change, ``PR_CLOSED_S`` once closed,
never again once merged, and only for a branch that was pushed. Each change
is one ``thread.git {ts, git}`` event, so open streams see it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from services.cowork_agent.storm.engine import Engine, Writes
from services.cowork_agent.storm.worktrees import state as git_state
from services.cowork_agent.storm.worktrees import trees
from services.errors import ServiceError
from services.timestamps import parse_ts

log = logging.getLogger(__name__)

#: A stored state younger than this is shown as it is.
FRESH_S = 60
#: How long a listing waits for the states it asked to be read again.
LIST_BUDGET_S = 3.0
#: A pull request that is open, or none found yet for a pushed branch.
PR_OPEN_S = 5 * 60
#: A closed pull request (it may be reopened).
PR_CLOSED_S = 3600
#: Threads read at once (each is a handful of local git commands).
READ_CONCURRENCY = 4
#: ``gh`` calls at once (each asks the forge).
GH_CONCURRENCY = 2
EVENT = "thread.git"


class Tracker:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._reads = asyncio.Semaphore(READ_CONCURRENCY)
        self._gh = asyncio.Semaphore(GH_CONCURRENCY)

    # ── reading the store ────────────────────────────────────────────────

    async def stored(self, keys: Iterable[tuple[str, str]] | None = None, *, chat: str | None = None
                     ) -> dict[tuple[str, str], dict[str, Any]]:
        """``{(chat, ts): {state, checked_at, pr_checked_at}}``."""
        def read(conn) -> list[tuple]:
            return conn.execute("SELECT chat_id, thread_ts, state, checked_at, pr_checked_at FROM thread_git "
                                "WHERE (:chat IS NULL OR chat_id = :chat)", {"chat": chat}).fetchall()
        wanted = set(keys) if keys is not None else None
        return {(c, t): {"state": json.loads(s), "checked_at": at, "pr_checked_at": pr}
                for c, t, s, at, pr in await self.engine.store.read(read)
                if wanted is None or (c, t) in wanted}

    # ── reading git ──────────────────────────────────────────────────────

    async def refresh(self, chat_id: str, ts: str, *, pr: bool | None = None) -> dict[str, Any] | None:
        """Read the thread's branch again and store what changed; ``pr``
        True asks the forge now, False never, None when due. Returns the
        state (None for a thread without a branch). Never raises for git or
        ``gh``: a failure is logged, and what was stored stays."""
        async with self._locks.setdefault((chat_id, ts), asyncio.Lock()):
            try:
                return await self._refresh(chat_id, ts, pr)
            except (ServiceError, OSError) as exc:
                log.warning("storm worktrees: couldn't read thread %s's git state: %s", ts,
                            getattr(exc, "message", exc))
                return None

    async def refresh_stale(self, keys: Iterable[tuple[str, str]], spawn) -> None:
        """Read again the threads whose stored state is older than
        ``FRESH_S``, waiting ``LIST_BUDGET_S`` at most; ``spawn`` keeps the
        unfinished ones running."""
        keys = list(keys)
        if not keys:
            return
        stored = await self.stored(keys)
        now = self.engine.clock.time()
        stale = [k for k in keys if k not in stored or _age(stored[k]["checked_at"], now) >= FRESH_S]
        if not stale:
            return
        tasks = [asyncio.ensure_future(self.refresh(*k)) for k in stale]
        _done, pending = await asyncio.wait(tasks, timeout=LIST_BUDGET_S)
        for task in pending:
            spawn(task)

    async def _refresh(self, chat_id: str, ts: str, pr: bool | None) -> dict[str, Any] | None:
        engine = self.engine
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT t.worktree_path, t.branch, t.base_commit, g.state, g.pr_checked_at FROM threads t LEFT JOIN "
            "thread_git g ON g.chat_id = t.chat_id AND g.thread_ts = t.ts WHERE t.chat_id = ? AND t.ts = ?",
            (chat_id, ts)).fetchone())
        if row is None or not row[0] or not row[1]:
            return None
        path, branch, base_commit, stored, pr_checked_at = row
        where = trees.thread_of(Path(path))
        if where is None:
            return None
        project = where[0]
        previous = json.loads(stored) if stored else None
        async with self._reads:
            now_state = await git_state.read(project, branch, base_commit, previous=previous)
        asked = False
        if pr is True or (pr is None and pr_due(now_state, pr_checked_at, engine.clock.time())):
            if git_state.gh_available():
                async with self._gh:
                    answered, found = await git_state.pr_lookup(project, branch)
                if answered:
                    now_state, asked = git_state.merge_pr(now_state, found), True

        def record(w: Writes) -> None:
            before = w.execute("SELECT state FROM thread_git WHERE chat_id = ? AND thread_ts = ?",
                               (chat_id, ts)).fetchone()
            if w.execute("SELECT 1 FROM threads WHERE chat_id = ? AND ts = ?", (chat_id, ts)).fetchone() is None:
                return                      # the thread went meanwhile (its chat was removed)
            w.execute("INSERT INTO thread_git (chat_id, thread_ts, state, checked_at, pr_checked_at) "
                      "VALUES (?, ?, json(?), ?, ?) ON CONFLICT (chat_id, thread_ts) DO UPDATE SET "
                      "state = excluded.state, checked_at = excluded.checked_at, "
                      "pr_checked_at = coalesce(excluded.pr_checked_at, thread_git.pr_checked_at)",
                      (chat_id, ts, json.dumps(now_state), w.now, w.now if asked else None))
            if git_state.changed(json.loads(before[0]) if before else None, now_state):
                w.append(EVENT, {"ts": ts, "git": now_state}, chat_id=chat_id, thread_ts=ts)
        await engine.write(record)
        return now_state


def pr_due(state: dict[str, Any], checked_at: str | None, now: float) -> bool:
    """Whether to ask the forge about the branch's pull request now."""
    pr = state.get("pr")
    if pr and pr.get("state") == "MERGED":
        return False
    if not pr and not (state.get("upstream") or {}).get("pushed"):
        return False                        # never pushed: there can't be one
    if checked_at is None:
        return True
    return _age(checked_at, now) >= (PR_CLOSED_S if pr and pr.get("state") == "CLOSED" else PR_OPEN_S)


def _age(when: str | None, now: float) -> float:
    parsed = parse_ts(when)
    return float("inf") if parsed is None else now - parsed.timestamp()

