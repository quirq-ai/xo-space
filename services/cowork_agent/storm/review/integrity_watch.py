"""The integrity check in the engine's life (design ch 16 §2.3–§2.4; owner
§11.2, §0.72): when it runs, the baselines it keeps, and the report it opens.
The check itself, on disk, is ``integrity.py``.

* **When.** Before every turn a thread, a job run or a plain session starts
  (the engine's ``TurnCheck``: a new input, a wake, the first turn after a
  release or a restart; a start is checked before its process opens), and
  at every input end (the runtime's ``SessionIdle``, or its process ending
  without one), as an engine ``Listener``. Coordinators are never checked
  (§2.3: they can't write, and their folder is the person's own). One
  session's checks run one at a time, off the event loop (git through the
  executor, the walk and the hashes in a thread), with at most one more
  queued behind the running one. A turn waits for its check at most
  ``BEFORE_TURN_WAIT_S``, and for a check that takes the baseline at most
  ``BASELINE_WAIT_S``; while an earlier check of the session still runs, it
  doesn't wait at all. A check that takes longer reports when it ends.
* **Baselines** (``integrity_baselines``, hashes only, §0.83) are taken only
  by a check before a turn, never at an input end (by then the agent has
  written): normally the first, before the process opens, with a marker row
  (path ``""``, kind ``baseline``) so that a folder with nothing watched in it
  has one too. When that check couldn't run and a later one takes it, the
  session has run turns meanwhile, so that is reported (``late_baseline``):
  what was written before is in the baseline. In a thread's worktree only
  git's own files and what its runtimes read in the project's main checkout
  are kept: its watched files are compared with ``base_commit``. The first
  check comes right after the worktree is made (the preparers run first).
* **On a change, a report** (the turn runs as usual): an ``integrity`` item
  listing up to ``MAX_LISTED`` changed paths (the rest counted), each with
  its diff (through ``redact``), the session's last recorded tool call that
  named it, and "Seen", opened through the queue in one transaction with its
  ``review.opened`` event; the queue's conversation tells a thread's
  coordinator. **Raised once per state:** the last state reported for each
  path (its hash, or nothing) is kept beside the baseline (kind
  ``reported``, naming the item that reported it, so that item's "Seen"
  covers the changes it only counted too); a path is reported again only
  when it moves to another state, and its record is cleared once a complete
  check finds it matching its baseline again, so content reverted and
  written again is reported again. "Seen" doesn't move the baseline (Keep is noted for later, §0.72). A
  check that can't run is reported the same way, once per reason code until
  a check runs without it.
* **The doctor** (§11.2) calls ``findings``: the same check, writing nothing,
  listing what no seen item covers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import Engine, Writes
from services.cowork_agent.storm.engine.session import Meta
from services.cowork_agent.storm.review import integrity
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.paths import cli_homes
from services.cowork_agent.storm.review.queue import ReviewQueue
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.worktrees.trees import thread_of
from services.storage.paths import quirq_state_dir

log = logging.getLogger(__name__)

#: The roles whose folders are checked (§2.3); a job run is a thread.
CHECKED_ROLES = ("plain", "thread")
#: How long a turn waits for its check (git and a few hashes take
#: milliseconds; this bounds a hung git or a stuck filesystem), and for one
#: that takes the baseline (worth waiting longer for: a baseline taken after
#: the turn started could hold the turn's own writes, and is reported as
#: late). The worst real check measured took 0.21 s; the wait holds a start
#: slot or the session's lock, so it stays short.
BEFORE_TURN_WAIT_S = 2.0
BASELINE_WAIT_S = 15.0
BEFORE_TURN, INPUT_END = "before_turn", "input_end"
#: The baseline's marker row (path, kind); the kind of the rows that keep
#: the last state reported for a path (path ``reported:<path>``) or a
#: failure (``reported:!<reason>``).
MARKER, MARKER_KIND = "", "baseline"
REPORTED, REPORTED_KIND = "reported:", "reported"
#: A process ended: an input it didn't finish with ``SessionIdle`` ends here.
ENDED = ("cold", "parked")
#: Cold without an input that ended: a new session, a process that never opened.
NO_INPUT = ("created", "open_failed")
#: The tool calls that can name a path they change: the file tools by
#: their target, a command by its text.
FILE_TOOLS = ("create_file", "edit_file", "delete_file")
NAMING = (*FILE_TOOLS, "command")
#: How many paths an item's title names.
TITLE_PATHS = 3
LATE_BASELINE = "late_baseline"
WHY_CODE = "These files can make an agent CLI or git run commands on their own, without asking anyone."
WHY_XO = "Files under .xo/ are XO's own project records, which agents never write."
WHY_TAIL = "XO reports the change; nothing was undone, and the turn ran as usual."
_ABSENT = object()


@dataclass(frozen=True)
class Target:
    """A session as the check needs it."""

    session_id: str
    role: str
    folder: Path
    chat_id: str | None
    thread_ts: str | None
    retired: bool
    worktree: bool                      # a thread's worktree: compared with its base commit
    base_commit: str | None


class IntegrityWatch:
    def __init__(self) -> None:
        self.engine: Engine | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._queued: set[str] = set()              # sessions whose next check waits for the running one
        self._skip: tuple[Path, ...] | None = None
        self._closed = False

    # ── life ─────────────────────────────────────────────────────────────

    def start(self, engine: Engine) -> None:
        """Once the engine runs: hear its input ends."""
        self.engine = engine
        engine.subscribe(self)

    async def stop(self) -> None:
        self._closed = True
        if self.engine is not None:
            self.engine.unsubscribe(self)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── when it runs ─────────────────────────────────────────────────────

    async def before_turn(self, engine: Engine, meta: Meta, spec: c.SessionSpec) -> None:
        """The engine's ``TurnCheck``."""
        if meta.role not in CHECKED_ROLES or self._closed:
            return
        lock = self._locks.get(meta.id)
        busy = lock is not None and lock.locked()
        task = self._spawn(engine, meta.id, BEFORE_TURN)
        if busy or task is None:
            return          # an earlier check still runs: the turn goes ahead, and that check reports when it ends
        has_baseline = await engine.store.read(lambda conn: _has_baseline(conn, meta.id))
        wait = BEFORE_TURN_WAIT_S if has_baseline else BASELINE_WAIT_S
        done, _pending = await asyncio.wait({task}, timeout=wait)
        if not done:
            log.warning("storm integrity: the check of %s runs past %s s; its turn starts, and the check reports "
                        "when it ends", meta.id, wait)

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.type == "session" and event.session_id and event.data.get("status") in ENDED \
                    and event.data.get("reason") not in NO_INPUT:
                self._heard(event.session_id)

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        if isinstance(event, c.SessionIdle):
            self._heard(event.session)

    def _heard(self, session_id: str) -> None:
        if self.engine is not None and not self._closed:
            self._spawn(self.engine, session_id, INPUT_END)

    # ── one check ────────────────────────────────────────────────────────

    async def _check(self, engine: Engine, session_id: str, moment: str) -> None:
        queue = engine.desk
        if not isinstance(queue, ReviewQueue):
            log.error("storm integrity: this engine runs without the review queue, so %s's check can't report",
                      session_id)
            return
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        try:
            await lock.acquire()
        finally:
            self._queued.discard(session_id)     # running now: a later one may queue behind it
        try:
            await self._check_locked(engine, queue, session_id, moment)
        finally:
            lock.release()

    async def _check_locked(self, engine: Engine, queue: ReviewQueue, session_id: str, moment: str) -> None:
        target, baseline, reported = await engine.store.read(lambda conn: (
            _target(conn, session_id), _baseline(conn, session_id), _reported(conn, session_id)))
        if target is None or target.role not in CHECKED_ROLES or target.retired:
            return
        keep = baseline is None and moment == BEFORE_TURN
        if baseline is None and not keep and not target.worktree:
            return                  # nothing to compare with yet; only a check before a turn takes it
        try:
            found = await self._inspect(engine, target, baseline)
        except integrity.CheckFailed as exc:            # nothing could be compared
            found, failures = None, [exc]
        else:
            failures = [found.failed] if found.failed else []
        changes = found.changes if found is not None else ()
        fresh = [ch for ch in changes if reported.get(ch.path, _ABSENT) != ch.sha256]
        fresh_failures = [f for f in failures if "!" + f.reason not in reported]
        # What a check no longer finds is clear, and reported again if it comes back: a failure
        # once a check runs without it, a path once a complete check finds it back at its baseline.
        current = {ch.path for ch in changes} | {"!" + f.reason for f in failures}
        complete = found is not None and not failures
        cleared = [key for key in reported if key not in current and (complete or key.startswith("!"))]
        listed = fresh[:integrity.MAX_LISTED]
        details = await integrity.describe(target.folder, listed, found.tree) if listed else []
        if details:
            by = await engine.store.read(lambda conn: _who(conn, target, details))
            for change in details:
                change["by"] = by.get(change["path"])
        keeping = keep and found is not None
        if not (keeping or fresh or fresh_failures or cleared):
            return

        def record(w: Writes) -> None:
            if keeping and _keep_baseline(w, session_id, found.snapshot) and _has_run(w, session_id):
                _report_failure(w, queue, target, integrity.CheckFailed(LATE_BASELINE, _late(target)), moment)
            _clear(w, session_id, cleared)
            if details:
                _report(w, queue, target, details, fresh, moment)
            for failure in fresh_failures:
                _report_failure(w, queue, target, failure, moment)
        await engine.write(record)

    async def _inspect(self, engine: Engine, target: Target,
                       baseline: Mapping[str, tuple[str, str]] | None) -> integrity.Inspection:
        if target.worktree and not target.base_commit:
            raise integrity.CheckFailed(integrity.BASE_UNREADABLE, f"The thread's worktree {target.folder} has no "
                                                                   "base commit recorded, so it can't be compared "
                                                                   "with one.")
        if self._skip is None:
            self._skip = await asyncio.to_thread(_never_watched)
        if target.worktree:
            watch = integrity.Watch(engine.project_config_paths, skip=self._skip)
        else:           # XO's own services write the same folder
            watch = integrity.Watch(engine.project_config_paths, skip=self._skip,
                                    unwatched=integrity.SNAPSHOT_UNWATCHED)
        main = None
        if target.worktree and engine.main_checkout_config_paths and (where := thread_of(target.folder)):
            main = (where[0], integrity.Watch(engine.main_checkout_config_paths, skip=self._skip, core=()))
        return await integrity.inspect(target.folder, watch, base_commit=target.base_commit if target.worktree
                                       else None, baseline=baseline, main=main)

    # ── the doctor ───────────────────────────────────────────────────────

    async def findings(self, engine: Engine) -> list[dict[str, Any]]:
        """Every checked session's open findings now (ch 16 §11.2): its
        changes against its baseline and the checks that can't run, less what
        an item reported and the person marked seen. Writes nothing: a
        session whose first check hasn't run has no baseline and isn't
        listed."""
        marks = ", ".join("?" * len(CHECKED_ROLES))
        ids = await engine.store.read(lambda conn: [sid for (sid,) in conn.execute(
            f"SELECT s.id FROM sessions s WHERE s.retired_at IS NULL AND s.role IN ({marks}) AND EXISTS ("
            "SELECT 1 FROM integrity_baselines b WHERE b.session_id = s.id AND b.path = ? AND b.kind = ?) "
            "ORDER BY s.created_at", (*CHECKED_ROLES, MARKER, MARKER_KIND))])
        out = []
        for session_id in ids:
            target, baseline, seen = await engine.store.read(lambda conn: (
                _target(conn, session_id), _baseline(conn, session_id), _seen(conn, session_id)))
            if target is None:
                continue
            try:
                found = await self._inspect(engine, target, baseline)
            except integrity.CheckFailed as exc:
                found, failures = None, [exc]
            else:
                failures = [found.failed] if found.failed else []
            changes = [{"path": ch.path, "group": ch.group, "change": ch.change, "against": ch.against}
                       for ch in (found.changes if found is not None else ()) if (ch.path, ch.sha256) not in seen]
            failed = [{"reason": f.reason, "message": f.message} for f in failures
                      if ("!" + f.reason, None) not in seen]
            if changes or failed:
                out.append({"session_id": session_id, "chat_id": target.chat_id, "thread_ts": target.thread_ts,
                            "folder": str(target.folder), "changes": changes, "failed": failed})
        return out

    # ── plumbing ─────────────────────────────────────────────────────────

    def _spawn(self, engine: Engine, session_id: str, moment: str) -> asyncio.Task | None:
        """Start a check of the session; None when one already waits behind
        the running one (it will look after it, so one is enough)."""
        if session_id in self._queued:
            return None
        self._queued.add(session_id)
        task = asyncio.create_task(self._check(engine, session_id, moment), name=f"storm-integrity:{session_id}")
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm integrity: a check failed", exc_info=task.exception())


def _never_watched() -> tuple[Path, ...]:
    """The CLIs' homes and XO's state root, resolved (§2.6)."""
    return tuple(Path(os.path.realpath(p)) for p in (*cli_homes(), quirq_state_dir()))


def _late(target: Target) -> str:
    compared = " Its watched files are still compared with the thread's base commit." if target.worktree else ""
    return ("XO couldn't take this session's baseline before its first turn (that check couldn't run), so it took "
            f"it now: a change made before now is part of the baseline and isn't reported.{compared}")


# ── the store (each runs on the store's thread) ──────────────────────────


def _target(conn, session_id: str) -> Target | None:
    row = conn.execute(
        "SELECT s.role, json_extract(s.spec, '$.cwd'), s.chat_id, s.thread_ts, s.retired_at IS NOT NULL, "
        "t.worktree_path, t.base_commit FROM sessions s LEFT JOIN threads t ON t.chat_id = s.chat_id "
        "AND t.ts = s.thread_ts WHERE s.id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    role, cwd, chat_id, thread_ts, retired, worktree_path, base_commit = row
    worktree = role == "thread" and bool(worktree_path)
    return Target(session_id, role, Path(worktree_path if worktree else cwd), chat_id, thread_ts, bool(retired),
                  worktree, base_commit)


def _has_baseline(conn, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM integrity_baselines WHERE session_id = ? AND path = ? AND kind = ?",
                        (session_id, MARKER, MARKER_KIND)).fetchone() is not None


def _baseline(conn, session_id: str) -> dict[str, tuple[str, str]] | None:
    """path → (sha256, group), or None when no baseline was taken yet."""
    if not _has_baseline(conn, session_id):
        return None
    return {path: (sha, kind) for path, sha, kind in conn.execute(
        "SELECT path, sha256, kind FROM integrity_baselines WHERE session_id = ? AND kind NOT IN (?, ?)",
        (session_id, MARKER_KIND, REPORTED_KIND))}


def _keep_baseline(w: Writes, session_id: str, snapshot: Mapping[str, tuple[str, str]]) -> bool:
    """Keep the first baseline; False when one was taken meanwhile."""
    if _has_baseline(w, session_id):
        return False
    w.tx.conn.executemany(
        "INSERT INTO integrity_baselines (session_id, path, sha256, kind) VALUES (?, ?, ?, ?)",
        [(session_id, MARKER, None, MARKER_KIND),
         *((session_id, path, sha, group) for path, (sha, group) in sorted(snapshot.items()))])
    return True


def _has_run(w: Writes, session_id: str) -> bool:
    return w.execute("SELECT 1 FROM turns WHERE session_id = ? LIMIT 1", (session_id,)).fetchone() is not None


def _reported(conn, session_id: str) -> dict[str, str | None]:
    """The last state reported for each path (its sha256; None for
    nothing there), and each failure reason reported (``!<reason>``)."""
    return {path.removeprefix(REPORTED): sha for path, sha in conn.execute(
        "SELECT path, sha256 FROM integrity_baselines WHERE session_id = ? AND kind = ?",
        (session_id, REPORTED_KIND))}


def _mark(w: Writes, session_id: str, key: str, sha: str | None, item_id: str | None) -> None:
    """Record a state as reported by ``item_id`` (a ``reported`` row's
    ``accepted_from_item``: the item whose "Seen" covers it)."""
    w.execute("INSERT INTO integrity_baselines (session_id, path, sha256, kind, accepted_from_item) "
              "VALUES (?, ?, ?, ?, ?) ON CONFLICT (session_id, path) DO UPDATE SET sha256 = excluded.sha256, "
              "accepted_from_item = excluded.accepted_from_item",
              (session_id, REPORTED + key, sha, REPORTED_KIND, item_id))


def _clear(w: Writes, session_id: str, keys: Sequence[str]) -> None:
    w.tx.conn.executemany("DELETE FROM integrity_baselines WHERE session_id = ? AND path = ? AND kind = ?",
                          [(session_id, REPORTED + key, REPORTED_KIND) for key in keys])


def _report(w: Writes, queue: ReviewQueue, target: Target, details: list[dict],
            fresh: Sequence[integrity.Change], moment: str) -> str | None:
    """Open the report for the changes in states not reported yet (checked
    again here), and record each one's state as reported, the counted ones
    too."""
    reported = _reported(w, target.session_id)
    fresh = [ch for ch in fresh if reported.get(ch.path, _ABSENT) != ch.sha256]
    keys = {(ch.path, ch.sha256) for ch in fresh}
    listed = [d for d in details if (d["path"], d["sha256"]) in keys]
    if not listed:
        for change in fresh:            # the listed ones were reported meanwhile; these were only counted
            _mark(w, target.session_id, change.path, change.sha256, None)
        return None
    paths = [d["path"] for d in listed]
    named = ", ".join(paths[:TITLE_PATHS]) + (f" and {len(fresh) - TITLE_PATHS} more" if len(fresh) > TITLE_PATHS
                                              else "")
    xo = [d["group"] == integrity.XO for d in listed]
    title = f"XO's project files changed: {named}" if all(xo) else \
        f"Files that can make agents or git run code changed: {named}"
    why = " ".join([*([] if all(xo) else [WHY_CODE]), *([WHY_XO] if any(xo) else []), WHY_TAIL])
    detail = {"changes": listed, "more": len(fresh) - len(listed), "folder": str(target.folder),
              "base_commit": target.base_commit if target.worktree else None, "checked": moment}
    item_id = queue.open_item(w, ItemSpec(kind="integrity", title=title, origin="xo", why=why, detail=detail,
                                          session_id=target.session_id, chat_id=target.chat_id,
                                          thread_ts=target.thread_ts))
    for change in fresh:                # the counted ones too: this item's "Seen" covers them
        _mark(w, target.session_id, change.path, change.sha256, item_id)
    return item_id


def _report_failure(w: Writes, queue: ReviewQueue, target: Target, failure: integrity.CheckFailed,
                    moment: str) -> str | None:
    if "!" + failure.reason in _reported(w, target.session_id):
        return None                     # reported meanwhile
    item_id = queue.open_item(w, ItemSpec(
        kind="integrity", title="XO couldn't check this session's config files", origin="xo", why=failure.message,
        detail={"failed": failure.message, "reason": failure.reason, "changes": [], "more": 0,
                "folder": str(target.folder), "checked": moment},
        session_id=target.session_id, chat_id=target.chat_id, thread_ts=target.thread_ts))
    _mark(w, target.session_id, "!" + failure.reason, None, item_id)
    return item_id


def _seen(conn, session_id: str) -> set[tuple[str, str | None]]:
    """(path, sha256) and (``!reason``, None) last reported by an item the
    person marked seen, listed in it or only counted."""
    return {(path.removeprefix(REPORTED), sha) for path, sha in conn.execute(
        "SELECT b.path, b.sha256 FROM integrity_baselines b JOIN review_items r ON r.id = b.accepted_from_item "
        "WHERE b.session_id = ? AND b.kind = ? AND r.state = 'decided'", (session_id, REPORTED_KIND))}


def _who(conn, target: Target, details: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each change, the session's last recorded tool call that named its
    path after the last item that listed it: which turn changed it, when the
    calls say so. One read of the session's naming tool calls, matched here,
    outside any write. A sub-agent's own calls aren't stored (ch 14 §2.1), so
    those stay unknown."""
    since = {path: seq for path, seq in conn.execute(
        "SELECT json_extract(c.value, '$.path'), max(r.seq) FROM review_items r, json_each(r.detail, '$.changes') c "
        "WHERE r.session_id = ? AND r.kind = 'integrity' GROUP BY 1", (target.session_id,))}
    start = min(since.get(d["path"], 0) for d in details)
    marks = ", ".join("?" * len(NAMING))
    calls = conn.execute(
        "SELECT seq, turn_id, run_id, json_extract(data, '$.tool'), json_extract(data, '$.title'), "
        "json_extract(data, '$.kind'), json_extract(data, '$.target') FROM events WHERE session_id = ? AND "
        f"type = 'step.tool' AND seq > ? AND json_extract(data, '$.kind') IN ({marks}) ORDER BY seq DESC",
        (target.session_id, start, *NAMING)).fetchall()
    written: dict[str, tuple] = {}              # a written path → its latest call
    commands: list[tuple] = []                  # latest first
    for call in calls:
        if not call[6]:
            continue
        if call[5] in FILE_TOOLS:
            named = call[6] if os.path.isabs(call[6]) else target.folder / call[6]
            written.setdefault(os.path.normpath(named), call)
        else:
            commands.append(call)
    out: dict[str, dict[str, Any]] = {}
    for change in details:
        path, floor = change["path"], since.get(change["path"], 0)
        names = {os.path.normpath(target.folder / path), *([os.path.normpath(change["file"])] if change.get("file")
                                                           else [])}
        call = max((written[n] for n in names if n in written and written[n][0] > floor), key=lambda h: h[0],
                   default=None)
        if call is None:
            call = next((cmd for cmd in commands if cmd[0] > floor and (path in cmd[6]
                                                                        or any(n in cmd[6] for n in names))), None)
        if call is not None:
            out[path] = {"turn_id": call[1], "run_id": call[2], "tool": call[3], "title": call[4], "seq": call[0]}
    return out
