"""Keeping each thread's git state in the store, so a read never runs git.

``thread_git.state`` holds two halves, read together under the thread's lock:
its branch (``state.py``) and its worktree (``tree``: there, XO's, and what
removing it would lose, counted file by file as removal counts; the first
paths of each kind go in ``tree_detail``, for the listing only).

It is read when it can change: ``mark`` (a turn ended, resolve or reopen, a
tree made, restored or removed, a file in it changed); ``check`` (the person
asks); and each ``TICK_S``, ``tick``: every project's branches diffed
against those last seen (``_diff_refs``; the first look after a start reads
every thread once, so nothing that changed while the server was down is
missed), then, only while someone looks, a backstop for the stalest threads
past ``ttl``. A thread with nothing left on disk (no tree, no branch) is
read again only when something happens to it.
Each change is one ``thread.git {ts, git}`` event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.cowork_agent.storm.engine import Engine, Writes
from services.cowork_agent.storm.worktrees import state as git_state
from services.cowork_agent.storm.worktrees import trees
from services.errors import ServiceError
from services.git_safe import GitFailed, git
from services.periodic import run_forever
from services.timestamps import parse_ts

log = logging.getLogger(__name__)

#: A pull request that is open, or none found yet for a pushed branch.
PR_OPEN_S = 5 * 60
#: A closed pull request (it may be reopened).
PR_CLOSED_S = 3600
#: Threads read at once in the background (each is a handful of local git commands).
WORKERS = 2
#: ``gh`` calls at once (each asks the forge).
GH_CONCURRENCY = 2
#: How long one thread's read may take before it is given up (it is marked again by the next trigger or the backstop).
READ_DEADLINE_S = 30.0
#: How often refs are compared and the backstop runs; the first one waits ``FIRST_TICK_S`` after the start.
TICK_S = 60.0
FIRST_TICK_S = 60.0
#: How old a stored state may get before the backstop reads it again: at least this for an open thread and a
#: resolved one; a sixth of how long the thread has been idle when that is longer, up to ``MAX_TTL_S``.
OPEN_TTL_S = 10 * 60
RESOLVED_TTL_S = 60 * 60
MAX_TTL_S = 6 * 3600
#: The backstop runs only while someone looks: a listing or a check this recently, or an open stream (the refs
#: are diffed every tick regardless).
LOOK_S = 5 * 60
#: Threads the backstop marks per tick at most, stalest first.
BACKSTOP_PER_TICK = 30
#: A file change in a tree is read after this much quiet (an agent or an editor writes in bursts).
FILE_SETTLE_S = 5.0
#: How long a background read reuses a project's worktree list (a mark from a tree's life reads it fresh).
SNAPSHOT_TTL_S = 5.0
EVENT = "thread.git"
#: Priorities: something happened to the thread; or the backstop and refs looking again.
NOW, LATER = 0, 1
#: The refs a thread's state is read against (``--branches``, ``--remotes``): tags, notes and the stash move nothing.
REF_SPACES = ("refs/heads/", "refs/remotes/")


@dataclass
class _Mark:
    priority: int
    due: float
    fresh: bool


class Tracker:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._gh = asyncio.Semaphore(GH_CONCURRENCY)
        self._marked: dict[tuple[str, str], _Mark] = {}
        self._reading: set[tuple[str, str]] = set()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._snaps: dict[Path, tuple[float, trees.Snapshot]] = {}
        self._refs: dict[Path, dict[str, tuple[str, str]]] = {}
        self._checks: dict[tuple[str, str], asyncio.Task] = {}
        self._looked_at = float("-inf")
        #: How many event streams are open (the service sets it; none in tests).
        self.streams: Callable[[], int] = lambda: 0

    # ── life ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._work(), name=f"storm-git-state-{n}") for n in range(WORKERS)]
        self._tasks.append(loop.create_task(
            run_forever("storm worktree git state", self.tick, interval_s=lambda: TICK_S,
                        startup_delay_s=FIRST_TICK_S, logger=log), name="storm-git-state-tick"))

    async def stop(self) -> None:
        tasks = [*self._tasks, *self._checks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []

    def looked(self) -> None:
        """Someone looked at the worktrees (the listing, a check): the
        backstop runs while someone looks (``LOOK_S``) or a stream is open."""
        self._looked_at = time.monotonic()

    def looking(self) -> bool:
        return time.monotonic() - self._looked_at < LOOK_S or self.streams() > 0

    # ── marking ──────────────────────────────────────────────────────────

    def mark(self, chat_id: str, ts: str, *, fresh: bool = False, settle: float = 0.0,
             priority: int = NOW) -> None:
        """Read the thread again soon: after ``settle`` seconds of quiet;
        ``fresh``: its worktree may have come or gone (its project's worktree
        list is read again)."""
        key = (chat_id, ts)
        due = time.monotonic() + settle
        mark = self._marked.get(key)
        if mark is None:
            self._marked[key] = _Mark(priority, due, fresh)
        else:
            mark.priority = min(mark.priority, priority)
            mark.due = max(mark.due, due) if settle else min(mark.due, due)
            mark.fresh = mark.fresh or fresh
        self._wake.set()

    def _next(self) -> tuple[tuple[str, str] | None, float | None]:
        """The marked thread to read now (by priority, then due), or how long until one is due."""
        now = time.monotonic()
        ready = [(m.priority, m.due, k) for k, m in self._marked.items() if m.due <= now and k not in self._reading]
        if ready:
            return min(ready)[2], None
        waiting = [m.due - now for k, m in self._marked.items() if k not in self._reading]
        return None, (min(waiting) if waiting else None)

    async def _work(self) -> None:
        while True:
            key, wait = self._next()
            if key is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                continue
            mark = self._marked.pop(key)
            self._reading.add(key)
            try:
                await asyncio.wait_for(self.refresh(*key, fresh=mark.fresh), timeout=READ_DEADLINE_S)
            except asyncio.TimeoutError:
                log.warning("storm worktrees: reading thread %s's git state took over %ss; it is read again "
                            "later", key[1], READ_DEADLINE_S)
            except Exception:  # noqa: BLE001 - one thread's failure never stops the workers
                log.exception("storm worktrees: reading thread %s's git state failed", key[1])
            finally:
                self._reading.discard(key)
                self._wake.set()        # the other worker may be waiting for this key

    # ── the tick: refs that changed, and the backstop ────────────────────

    async def tick(self) -> None:
        def read(conn) -> list[tuple]:
            return conn.execute(
                "SELECT t.chat_id, t.ts, t.worktree_path, t.resolved_at, g.state, g.checked_at, t.branch, "
                "t.updated_at "
                "FROM threads t LEFT JOIN thread_git g ON g.chat_id = t.chat_id AND g.thread_ts = t.ts "
                "WHERE t.branch IS NOT NULL AND t.worktree_path IS NOT NULL AND t.deleted_at IS NULL").fetchall()
        rows = await self.engine.store.read(read)
        now = self.engine.clock.time()
        live: dict[Path, list[tuple[str, str, str]]] = {}
        gone: dict[Path, list[tuple[str, str, str]]] = {}
        due: list[tuple[float, tuple[str, str]]] = []
        for chat_id, ts, path, resolved_at, stored, checked_at, branch, updated_at in rows:
            state = json.loads(stored) if stored else None
            nothing = _nothing_on_disk(state)
            where = trees.thread_of(Path(path))
            if where is not None:
                (gone if nothing else live).setdefault(where[0], []).append((chat_id, ts, branch))
            if state is None or "tree" not in state:
                due.append((float("inf"), (chat_id, ts)))      # never read, or read before the tree half was kept
            elif not nothing:               # nothing left on disk to change: only its pull request, asked on turns
                age = _age(checked_at, now)
                if age >= ttl(bool(resolved_at), _age(updated_at, now)):
                    due.append((age, (chat_id, ts)))
        await self._diff_refs(live, gone)
        if not self.looking():
            return          # the backstop is for what no trigger sees: only worth reading while someone looks
        # Never read (a new build's first start): all of them, two at a time; the rest, the stalest few.
        due.sort(reverse=True)
        unread = sum(1 for age, _key in due if age == float("inf"))
        for _age_s, key in due[:max(unread, BACKSTOP_PER_TICK)]:
            if key not in self._marked:
                self.mark(*key, priority=LATER)

    async def _diff_refs(self, live: dict[Path, list[tuple[str, str, str]]],
                         gone: dict[Path, list[tuple[str, str, str]]]) -> None:
        """Each project's refs against those last seen; marks the threads a
        change touches. Runs every tick: one ``for-each-ref`` per project,
        whether or not anyone looks. ``live``: each project's threads with
        a tree or a branch, as ``(chat, ts, branch)``; ``gone``: those with
        neither. A project first seen (this server's first tick) has all its
        live threads read once: whatever changed while no server watched,
        refs, trees or pull requests, is read then."""
        for project in sorted(live.keys() | gone.keys()):
            refs = await self._ref_map(project)
            if refs is None:
                continue
            before = self._refs.get(project)
            if before == refs:
                continue
            self._refs[project] = refs
            threads = live.get(project, [])
            if before is None:
                touched = [(chat_id, ts) for chat_id, ts, _branch in threads]
            else:
                touched = await _touched(project, before, refs, threads, gone.get(project, []))
            for key in touched:
                self.mark(*key, priority=LATER)

    async def _ref_map(self, project: Path) -> dict[str, tuple[str, str]] | None:
        """``{ref: (sha, symref target)}`` of the project's branches and
        remote-tracking refs (``REF_SPACES``; None when git can't tell)."""
        result = await git(project, "for-each-ref", "--format=%(objectname) %(refname) %(symref)",
                           *(space.rstrip("/") for space in REF_SPACES), timeout=READ_DEADLINE_S)
        if not result.ok:
            return None
        refs: dict[str, tuple[str, str]] = {}
        for line in result.output.splitlines():
            sha, _, rest = line.partition(" ")
            name, _, target = rest.partition(" ")
            if sha and name:
                refs[name] = (sha, target)
        return refs

    # ── reading git ──────────────────────────────────────────────────────

    async def refresh(self, chat_id: str, ts: str, *, pr: bool | None = None, fresh: bool = True
                      ) -> dict[str, Any] | None:
        """Read the thread's branch and worktree again and store what
        changed; ``pr`` True asks the forge now, False never, None when due.
        Returns the state (None for a thread without a branch). Never raises
        for git or ``gh``: a failure is logged, and what was stored stays.
        One read per thread at a time (its lock)."""
        async with self._locks.setdefault((chat_id, ts), asyncio.Lock()):
            try:
                state = await self._refresh(chat_id, ts, pr, fresh=fresh)
            except (ServiceError, OSError) as exc:
                log.warning("storm worktrees: couldn't read thread %s's git state: %s", ts,
                            getattr(exc, "message", exc))
                return None
            return state

    async def check(self, chat_id: str, ts: str) -> dict[str, Any] | None:
        """The person's check of one thread: both halves read now and stored.
        One at a time per thread: a second caller gets the running one's answer."""
        self.looked()
        key = (chat_id, ts)
        task = self._checks.get(key)
        if task is None:
            task = asyncio.ensure_future(self.refresh(chat_id, ts))
            self._checks[key] = task
            task.add_done_callback(lambda _t, key=key: self._checks.pop(key, None))
        return await asyncio.shield(task)

    async def _refresh(self, chat_id: str, ts: str, pr: bool | None, *, fresh: bool) -> dict[str, Any] | None:
        engine = self.engine
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT t.worktree_path, t.branch, t.base_commit, g.state, g.pr_checked_at, g.tree_detail FROM threads t "
            "LEFT JOIN thread_git g ON g.chat_id = t.chat_id AND g.thread_ts = t.ts WHERE t.chat_id = ? AND t.ts = ?",
            (chat_id, ts)).fetchone())
        if row is None or not row[0] or not row[1]:
            return None
        path, branch, base_commit, stored, pr_checked_at, stored_detail = row
        where = trees.thread_of(Path(path))
        if where is None:
            return None
        project = where[0]
        previous = json.loads(stored) if stored else None
        try:
            now_state = await git_state.read(project, branch, base_commit, previous=previous)
            branch_read = True
        except GitFailed as exc:    # the project's repository can't be read (gone, moved): its tree says why
            log.warning("storm worktrees: couldn't read thread %s's branch: %s", ts, exc.message)
            now_state, branch_read = {**git_state.empty(branch), **(previous or {})}, False
        tree, detail = await self._tree(chat_id, ts, project, Path(path), branch, now_state,
                                        (previous or {}).get("tree"), stored_detail, fresh=fresh)
        now_state["tree"] = tree
        asked = False
        if branch_read and (pr is True or (pr is None and pr_due(now_state, pr_checked_at, engine.clock.time()))):
            if git_state.gh_available():
                async with self._gh:
                    answered, found = await git_state.pr_lookup(project, branch)
                if answered:
                    now_state, asked = git_state.merge_pr(now_state, found), True

        def record(w: Writes) -> None:
            before = w.execute("SELECT state FROM thread_git WHERE chat_id = ? AND thread_ts = ?",
                               (chat_id, ts)).fetchone()
            if w.execute("SELECT 1 FROM threads WHERE chat_id = ? AND ts = ? AND purged_at IS NULL",
                         (chat_id, ts)).fetchone() is None:
                return                      # the thread went meanwhile (its chat was removed, or it was purged)
            now_state["tree"]["checked_at"] = w.now
            w.execute("INSERT INTO thread_git (chat_id, thread_ts, state, checked_at, pr_checked_at, tree_detail) "
                      "VALUES (?, ?, json(?), ?, ?, json(?)) ON CONFLICT (chat_id, thread_ts) DO UPDATE SET "
                      "state = excluded.state, checked_at = excluded.checked_at, "
                      "pr_checked_at = coalesce(excluded.pr_checked_at, thread_git.pr_checked_at), "
                      "tree_detail = excluded.tree_detail",
                      (chat_id, ts, json.dumps(now_state), w.now, w.now if asked else None, json.dumps(detail)))
            if git_state.changed(json.loads(before[0]) if before else None, now_state):
                w.append(EVENT, {"ts": ts, "git": now_state}, chat_id=chat_id, thread_ts=ts)
        # Once git has answered, the answer is stored even if the caller stops waiting.
        await asyncio.shield(engine.write(record))
        return now_state

    async def _tree(self, chat_id: str, ts: str, project: Path, path: Path, branch: str, branch_state: dict,
                    previous: dict | None, stored_detail: str | None, *, fresh: bool
                    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The worktree half: ``(tree, detail)``. The detail
        keeps what the listing shows beyond the event: whether git still has the tree
        registered, whether its folder is there, and each kind's first paths."""
        nothing = {"changed": 0, "untracked": 0, "kept_ignored": 0, "local_commits": 0, "would_lose": False}
        try:
            snap = await self._snapshot(project, fresh=fresh)
            found = await asyncio.to_thread(snap.found, path)
            where = {"registered": found.registered, "folder": found.exists}
            tree = {"exists": found.checkout, "xo": found.ours(chat_id, ts), "error": None, "checked_at": None}
            if not (tree["xo"] and found.checkout):
                return {**tree, **nothing}, where
            lost = await trees.loss(found, branch, branch_exists=bool(branch_state.get("branch_exists")))
        except (ServiceError, OSError) as exc:      # this tree can't be read now: its last state stays, saying why
            why = getattr(exc, "message", None) or str(exc)
            log.warning("storm worktrees: can't tell what removing %s would lose: %s", path, why)
            kept = {k: (previous or {}).get(k) for k in ("exists", "xo", *nothing)}
            return ({**kept, "error": why, "checked_at": None},
                    json.loads(stored_detail) if stored_detail else {})
        return {**tree, **lost.counts()}, {**where, **lost.paths()}

    async def _snapshot(self, project: Path, *, fresh: bool) -> trees.Snapshot:
        now = time.monotonic()
        kept = self._snaps.get(project)
        if not fresh and kept is not None and now - kept[0] < SNAPSHOT_TTL_S:
            return kept[1]
        snap = await trees.snapshot(project)
        self._snaps[project] = (time.monotonic(), snap)
        return snap


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


async def _touched(project: Path, before: dict[str, tuple[str, str]], after: dict[str, tuple[str, str]],
                   live: list[tuple[str, str, str]], gone: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The threads a change of the project's refs touches. A thread's
    ahead, behind and merged are against the default branch, so a move of
    it touches every live thread (a gone one's fields are frozen); its
    own branch, or a remote-tracking copy of it, moving, coming or going
    touches that thread. Any other ref only changes which commits a
    removal would lose: that count is shown as last read until the thread
    is read again (a turn, a file, the backstop), and removal never trusts
    it, recounting under the tree's lock."""
    moved = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    bases = await _base_refs(project, before) | await _base_refs(project, after)
    if moved & bases or not bases & after.keys():
        return [(chat_id, ts) for chat_id, ts, _branch in live]
    return [(chat_id, ts) for chat_id, ts, branch in (*live, *gone)
            if any(name == f"refs/heads/{branch}" or name.startswith("refs/remotes/") and name.endswith(f"/{branch}")
                   for name in moved)]


async def _base_refs(project: Path, refs: dict[str, tuple[str, str]]) -> set[str]:
    """The refs that hold the project's default branch in ``refs``, named as
    ``state`` reads against it (``state.default_branch_name``), and
    ``origin/HEAD``, which picks it."""
    name = await git_state.default_branch_name(project, refs.get("refs/remotes/origin/HEAD", ("", ""))[1] or None,
                                               refs.__contains__)
    return {"refs/remotes/origin/HEAD", *((f"refs/heads/{name}", f"refs/remotes/origin/{name}") if name else ())}


def _nothing_on_disk(state: dict[str, Any] | None) -> bool:
    """Whether a stored state says the thread has neither a tree nor a
    branch left (read cleanly): nothing of it that git can change."""
    if state is None or "tree" not in state:
        return False
    tree = state["tree"] or {}
    return not state.get("branch_exists") and not tree.get("exists") and tree.get("error") is None


def ttl(resolved: bool, idle_s: float) -> float:
    """How old a thread's stored state may get before the backstop reads it
    again: the longer the thread has been idle, the less likely an unseen
    change, so the longer (a sixth of its idle time, between its floor and
    ``MAX_TTL_S``)."""
    return min(max(idle_s / 6, RESOLVED_TTL_S if resolved else OPEN_TTL_S), MAX_TTL_S)


def _age(when: str | None, now: float) -> float:
    parsed = parse_ts(when)
    return float("inf") if parsed is None else now - parsed.timestamp()

