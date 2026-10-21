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
  executor, the walk and the hashes in a thread). A turn waits for its check
  at most ``BEFORE_TURN_WAIT_S``; a check that takes longer reports when it
  ends.
* **Baselines** (``integrity_baselines``, hashes only, §0.83): taken at a
  session's first check, in one transaction, with a marker row (path ``""``,
  kind ``baseline``) so that a folder with nothing watched in it has one
  too. In a thread's worktree only git's own files and what its runtimes
  read in the project's main checkout are kept there: its watched files are
  compared with ``base_commit``. That first check comes
  right after the worktree is made: a start runs the preparers, then the
  check, then the process.
* **On a change, a report** (the turn runs as usual): an ``integrity`` item
  with each changed path, its diff (through ``redact``), the session's last
  recorded tool call that named it, and "Seen", opened through the queue in
  one transaction with its ``review.opened`` event; the queue's conversation
  tells a thread's coordinator. **Raised once:** a change is keyed by its
  path and what is there now (its hash, or nothing), and one that any earlier
  integrity item of the session listed, open or seen, isn't listed again.
  "Seen" doesn't move the baseline (Keep is noted for later, §0.72), so a
  seen change stays quiet until the path changes again. A check that can't
  run is reported the same way, once per reason.
* **The doctor** (§11.2) calls ``findings``: the same check, writing nothing.
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
#: milliseconds; this bounds a hung git or a stuck filesystem).
BEFORE_TURN_WAIT_S = 2.0
#: The baseline's marker row: its path, and its kind.
MARKER, MARKER_KIND = "", "baseline"
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
WHY = ("These files can make an agent CLI or git run commands on their own, without asking anyone. XO reports the "
       "change; nothing was undone, and the turn ran as usual.")


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
        task = self._spawn(engine, meta.id, "before_turn")
        done, _pending = await asyncio.wait({task}, timeout=BEFORE_TURN_WAIT_S)
        if not done:
            log.warning("storm integrity: the check of %s runs past %s s; its turn starts, and the check reports "
                        "when it ends", meta.id, BEFORE_TURN_WAIT_S)

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
            self._spawn(self.engine, session_id, "input_end")

    # ── one check ────────────────────────────────────────────────────────

    async def _check(self, engine: Engine, session_id: str, moment: str) -> None:
        queue = engine.desk
        if not isinstance(queue, ReviewQueue):
            log.error("storm integrity: this engine runs without the review queue, so %s's check can't report",
                      session_id)
            return
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            target, baseline, reported, failures = await engine.store.read(lambda conn: (
                _target(conn, session_id), _baseline(conn, session_id), _reported(conn, session_id),
                _failures(conn, session_id)))
            if target is None or target.role not in CHECKED_ROLES or target.retired:
                return
            try:
                found = await self._inspect(engine, target, baseline)
                fresh = [ch for ch in found.changes if (ch.path, ch.sha256) not in reported]
                details = await integrity.describe(target.folder, fresh, found.tree) if fresh else []
            except integrity.CheckFailed as exc:            # nothing could be compared
                failure = exc.message
                if failure not in failures:
                    await engine.write(lambda w: _report_failure(w, queue, target, failure, moment))
                return
            failed = found.failed if found.failed not in failures else None

            def record(w: Writes) -> None:
                if baseline is None:
                    _keep_baseline(w, session_id, found.snapshot)
                if details:
                    _report(w, queue, target, details, moment)
                if failed:
                    _report_failure(w, queue, target, failed, moment)
            if baseline is None or details or failed:
                await engine.write(record)

    async def _inspect(self, engine: Engine, target: Target,
                       baseline: Mapping[str, tuple[str, str]] | None) -> integrity.Inspection:
        if target.worktree and not target.base_commit:
            raise integrity.CheckFailed(f"The thread's worktree {target.folder} has no base commit recorded, so "
                                        "it can't be compared with one.")
        if self._skip is None:
            self._skip = await asyncio.to_thread(_never_watched)
        watch = integrity.Watch(engine.project_config_paths, skip=self._skip)
        main = None
        if target.worktree and engine.main_checkout_config_paths and (where := thread_of(target.folder)):
            main = (where[0], integrity.Watch(engine.main_checkout_config_paths, skip=self._skip, core=()))
        return await integrity.inspect(target.folder, watch, base_commit=target.base_commit if target.worktree
                                       else None, baseline=baseline, main=main)

    # ── the doctor ───────────────────────────────────────────────────────

    async def findings(self, engine: Engine) -> list[dict[str, Any]]:
        """Every checked session's changes against its baseline now, and the
        checks that can't run (ch 16 §11.2). Writes nothing: a session whose
        first check hasn't run yet has no baseline and isn't listed."""
        marks = ", ".join("?" * len(CHECKED_ROLES))
        ids = await engine.store.read(lambda conn: [sid for (sid,) in conn.execute(
            f"SELECT s.id FROM sessions s WHERE s.retired_at IS NULL AND s.role IN ({marks}) AND EXISTS ("
            "SELECT 1 FROM integrity_baselines b WHERE b.session_id = s.id AND b.path = ? AND b.kind = ?) "
            "ORDER BY s.created_at", (*CHECKED_ROLES, MARKER, MARKER_KIND))])
        out = []
        for session_id in ids:
            target, baseline = await engine.store.read(lambda conn: (_target(conn, session_id),
                                                                     _baseline(conn, session_id)))
            if target is None:
                continue
            finding: dict[str, Any] = {"session_id": session_id, "chat_id": target.chat_id,
                                       "thread_ts": target.thread_ts, "folder": str(target.folder)}
            try:
                found = await self._inspect(engine, target, baseline)
            except integrity.CheckFailed as exc:
                finding["failed"] = exc.message
            else:
                if not found.changes and not found.failed:
                    continue
                finding["changes"] = [{"path": ch.path, "group": ch.group, "change": ch.change,
                                       "against": ch.against} for ch in found.changes]
                if found.failed:
                    finding["failed"] = found.failed
            out.append(finding)
        return out

    # ── plumbing ─────────────────────────────────────────────────────────

    def _spawn(self, engine: Engine, session_id: str, moment: str) -> asyncio.Task:
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


def _baseline(conn, session_id: str) -> dict[str, tuple[str, str]] | None:
    """path → (sha256, group), or None when no baseline was taken yet."""
    rows = conn.execute("SELECT path, sha256, kind FROM integrity_baselines WHERE session_id = ?",
                        (session_id,)).fetchall()
    if not any(path == MARKER and kind == MARKER_KIND for path, _sha, kind in rows):
        return None
    return {path: (sha, kind) for path, sha, kind in rows if kind != MARKER_KIND}


def _keep_baseline(w: Writes, session_id: str, snapshot: Mapping[str, tuple[str, str]]) -> None:
    if _baseline(w, session_id) is not None:
        return                          # taken meanwhile: the first one stands
    w.tx.conn.executemany(
        "INSERT INTO integrity_baselines (session_id, path, sha256, kind) VALUES (?, ?, ?, ?)",
        [(session_id, MARKER, None, MARKER_KIND),
         *((session_id, path, sha, group) for path, (sha, group) in sorted(snapshot.items()))])


def _reported(conn, session_id: str) -> set[tuple[str, str | None]]:
    """(path, sha256) of every change an integrity item of the session listed."""
    return {(path, sha) for path, sha in conn.execute(
        "SELECT json_extract(c.value, '$.path'), json_extract(c.value, '$.sha256') FROM review_items r, "
        "json_each(r.detail, '$.changes') c WHERE r.session_id = ? AND r.kind = 'integrity'", (session_id,))}


def _failures(conn, session_id: str) -> set[str]:
    """Why the session's checks couldn't run, as items reported it."""
    return {why for (why,) in conn.execute(
        "SELECT json_extract(detail, '$.failed') FROM review_items WHERE session_id = ? AND kind = 'integrity' "
        "AND json_extract(detail, '$.failed') IS NOT NULL", (session_id,))}


def _report(w: Writes, queue: ReviewQueue, target: Target, details: list[dict], moment: str) -> str | None:
    """Open the report for what no item listed yet (checked again here: a
    check that ran meanwhile may have reported it)."""
    reported = _reported(w, target.session_id)
    fresh = [d for d in details if (d["path"], d["sha256"]) not in reported]
    if not fresh:
        return None
    for change in fresh:
        change["by"] = _who(w, target, change)
    paths = [d["path"] for d in fresh]
    named = ", ".join(paths[:TITLE_PATHS]) + (f" and {len(paths) - TITLE_PATHS} more" if len(paths) > TITLE_PATHS
                                              else "")
    detail = {"changes": fresh, "folder": str(target.folder), "base_commit": target.base_commit if target.worktree
              else None, "checked": moment}
    return queue.open_item(w, ItemSpec(
        kind="integrity", title=f"Files that can make agents or git run code changed: {named}", origin="xo",
        why=WHY, detail=detail, session_id=target.session_id, chat_id=target.chat_id, thread_ts=target.thread_ts))


def _report_failure(w: Writes, queue: ReviewQueue, target: Target, failure: str, moment: str) -> str | None:
    if failure in _failures(w, target.session_id):
        return None                     # reported meanwhile
    return queue.open_item(w, ItemSpec(
        kind="integrity", title="XO couldn't check this session's config files", origin="xo", why=failure,
        detail={"failed": failure, "changes": [], "folder": str(target.folder), "checked": moment},
        session_id=target.session_id, chat_id=target.chat_id, thread_ts=target.thread_ts))


def _who(w: Writes, target: Target, change: Mapping[str, Any]) -> dict[str, Any] | None:
    """The session's last recorded tool call that named the path, after the
    last item that reported it: which turn changed it, when the calls say
    so. A sub-agent's own calls aren't stored (ch 14 §2.1), so those stay
    unknown."""
    since = w.execute(
        "SELECT coalesce(max(r.seq), 0) FROM review_items r, json_each(r.detail, '$.changes') c WHERE "
        "r.session_id = ? AND r.kind = 'integrity' AND json_extract(c.value, '$.path') = ?",
        (target.session_id, change["path"])).fetchone()[0]
    file = change.get("file")
    for seq, turn_id, run_id, tool, title, kind, named in w.execute(
            "SELECT seq, turn_id, run_id, json_extract(data, '$.tool'), json_extract(data, '$.title'), "
            "json_extract(data, '$.kind'), json_extract(data, '$.target') FROM events WHERE session_id = ? AND "
            f"type = 'step.tool' AND seq > ? AND json_extract(data, '$.kind') IN ({', '.join('?' * len(NAMING))}) "
            "ORDER BY seq DESC", (target.session_id, since, *NAMING)):
        if _names(kind, named, change["path"], file, target.folder):
            return {"turn_id": turn_id, "run_id": run_id, "tool": tool, "title": title, "seq": seq}
    return None


def _names(kind: str | None, named: str | None, path: str, file: str | None, folder: Path) -> bool:
    if not named:
        return False
    if kind in FILE_TOOLS:
        written = os.path.normpath(named if os.path.isabs(named) else folder / named)
        return written == file or written == os.path.normpath(folder / path)
    if kind == "command":
        return path in named or bool(file and file in named)
    return False
