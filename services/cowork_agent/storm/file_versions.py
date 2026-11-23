"""Joins Space's file versions with storm's running turns: attributes changes to agents and streams file events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.storm.store import StoredEvent
from services.project_files import edits, events
from services.project_files.address import is_noise
from services.project_files.save import file_locks
from services.project_files.watch import watch
from services.timestamps import parse_ts

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine import Engine, Writes

log = logging.getLogger(__name__)

#: How often the running turns are read again from the store (a turn whose
#: end was missed must not keep attributing).
REFRESH_S = 60.0
#: How long a turn's end waits for the watcher's last batch (its debounce
#: and a margin), so the record attributed is the watcher's own.
SETTLE_S = 0.4
#: A write the watcher records this long after the turn's end is still the
#: turn's (its debounce and the batch's processing); a later one is not.
WINDOW_AFTER_END_S = 2.0
#: How long a finished turn is remembered as finished.
FINISHED_KEPT_S = 600.0


@dataclass(frozen=True)
class _Turn:
    turn_id: str
    session_id: str
    chat_id: str | None
    thread_ts: str | None
    folder: str
    started_at: str | None


class FileVersions:
    def __init__(self) -> None:
        self.engine: Engine | None = None
        #: turn id -> its turn; replaced whole, never changed in place (the
        #: watcher reads it from a worker thread).
        self._running: dict[str, _Turn] = {}
        self._tasks: set[asyncio.Task] = set()
        self._refresher: asyncio.Task | None = None
        #: Turns that finished lately: a read of the running turns that raced
        #: their end must not bring them back.
        self._finished: dict[str, float] = {}

    # ── life ─────────────────────────────────────────────────────────────

    def start(self, engine: Engine) -> None:
        self.engine = engine
        engine.subscribe(self)
        events.subscribe(self.deliver)
        watch.attributor = self.attributor
        watch.busy.append(self.busy)
        self._refresher = asyncio.get_running_loop().create_task(self._refresh_forever())

    async def stop(self) -> None:
        events.unsubscribe(self.deliver)
        if watch.attributor == self.attributor:
            watch.attributor = None
        if self.busy in watch.busy:
            watch.busy.remove(self.busy)
        if self.engine is not None:
            self.engine.unsubscribe(self)
        if self._refresher is not None:
            self._refresher.cancel()
        pending = [*self._tasks, *([self._refresher] if self._refresher is not None else [])]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── who is writing now ───────────────────────────────────────────────

    def committed(self, stored: Sequence[StoredEvent]) -> None:
        for event in stored:
            if event.type == "turn.started" and event.turn_id and event.session_id:
                self._spawn(self._learn(event.turn_id))
            elif event.type == "turn.finished" and event.turn_id:
                now = time.monotonic()
                self._finished = {t: at for t, at in self._finished.items() if now - at < FINISHED_KEPT_S}
                self._finished[event.turn_id] = now
                if event.turn_id in self._running:
                    running = dict(self._running)
                    running.pop(event.turn_id, None)
                    self._running = running

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        """Nothing live matters here."""

    async def _learn(self, turn_id: str) -> None:
        turns = {t: turn for t, turn in (await self._read_running(turn_id)).items() if t not in self._finished}
        if turns:
            self._running = {**self._running, **turns}
            self._touch(turns.values())

    async def _refresh_forever(self) -> None:
        while True:
            try:
                turns = await self._read_running(None)
                self._running = {t: turn for t, turn in turns.items() if t not in self._finished}
                self._touch(turns.values())
            except Exception:  # noqa: BLE001 - the next refresh tries again
                log.exception("storm file versions: couldn't read the running turns")
            await asyncio.sleep(REFRESH_S)

    async def _read_running(self, turn_id: str | None) -> dict[str, _Turn]:
        engine = self._engine()

        def read(conn) -> list[tuple]:
            sql = ("SELECT t.id, t.session_id, s.chat_id, s.thread_ts, s.spec, t.started_at FROM turns t "
                   "JOIN sessions s ON s.id = t.session_id WHERE t.status = 'running'")
            return conn.execute(sql + (" AND t.id = ?" if turn_id else ""),
                                (turn_id,) if turn_id else ()).fetchall()
        rows = await engine.store.read(read)
        out: dict[str, _Turn] = {}
        for tid, session_id, chat_id, thread_ts, spec, started_at in rows:
            try:
                cwd = json.loads(spec).get("cwd") if spec else None
            except ValueError:
                cwd = None
            if not cwd or not os.path.isabs(cwd):
                continue
            out[tid] = _Turn(tid, session_id, chat_id, thread_ts, os.path.realpath(cwd), started_at)
        return out

    def _touch(self, turns) -> None:
        for turn in turns:
            found = edits.project_of(Path(turn.folder))
            if found is not None:
                watch.touch(found[0])

    def attributor(self, path: Path) -> Mapping[str, Any] | None:
        """The running turn whose folder holds ``path`` (the deepest)."""
        text = str(path)
        best: _Turn | None = None
        for turn in self._running.values():
            if text == turn.folder or text.startswith(turn.folder + os.sep):
                if best is None or len(turn.folder) > len(best.folder):
                    best = turn
        if best is None:
            return None
        return {"session_id": best.session_id, "thread_ts": best.thread_ts, "turn_id": best.turn_id}

    def busy(self, project_id: str) -> bool:
        root = os.path.realpath(xo_projects_root() / project_id)
        return any(t.folder == root or t.folder.startswith(root + os.sep) for t in self._running.values())

    # ── a turn's end ─────────────────────────────────────────────────────

    async def after_turn(self, event: StoredEvent, wrote: list[tuple[Path, str]], message_id: str | None) -> None:
        """``Made.after_turn``: the files this turn's tools wrote are its."""
        if not wrote or not event.turn_id:
            return
        await asyncio.sleep(SETTLE_S)
        engine = self._engine()
        row = await engine.store.read(lambda conn: conn.execute(
            "SELECT started_at FROM turns WHERE id = ?", (event.turn_id,)).fetchone())
        started = parse_ts(row[0]) if row and row[0] else None
        since = started or datetime.now(timezone.utc)
        ended = parse_ts(event.ts) or datetime.now(timezone.utc)
        until = ended + timedelta(seconds=WINDOW_AFTER_END_S)
        by = edits.writer(edits.AGENT, session_id=event.session_id, thread_ts=event.thread_ts,
                          turn_id=event.turn_id, message_id=message_id)
        await asyncio.to_thread(_attribute_all, wrote, since, until, by)

    # ── on the stream ────────────────────────────────────────────────────

    async def deliver(self, batch: list[events.Event]) -> None:
        """Append Space's file events to their projects' chats, creating a missing chat so no event is dropped."""
        engine = self.engine
        if engine is None or not batch:
            return
        names = sorted({project for _type, project, _data in batch})

        def known(conn) -> dict[str, str]:
            found = {}
            for name in names:
                row = conn.execute("SELECT id FROM chats WHERE kind = 'project' AND retired_at IS NULL AND "
                                   "project_name = ?", (name,)).fetchone()
                if row:
                    found[name] = row[0]
            return found
        chats = await engine.store.read(known)
        for name in names:
            if name not in chats:
                chats[name] = await _project_chat(name)

        def write(w: Writes) -> None:
            for type_, project, data in batch:
                if chats.get(project):
                    w.append(type_, data, chat_id=chats[project])
        await engine.write(write)

    def _engine(self) -> Engine:
        if self.engine is None:
            raise RuntimeError("storm file versions isn't started")
        return self.engine


async def _project_chat(name: str) -> str | None:
    """The project's chat id, created on first use; None when it can't be made."""
    from services.cowork_agent.storm import chats as storm_chats

    try:
        return (await storm_chats.current().project(name))["id"]
    except Exception as exc:  # noqa: BLE001 - no chat: GET /versions?since= still has every record
        log.info("storm file versions: no chat for project %s (%s); its file events aren't streamed", name, exc)
        return None


def _attribute_all(wrote: list[tuple[Path, str]], since: datetime, until: datetime, by: Mapping[str, Any]) -> None:
    for path, tool_kind in wrote:
        real = Path(os.path.realpath(path))
        found = edits.project_of(real)
        if found is None:
            continue
        pid, root = found
        rel = real.relative_to(root.resolve()).as_posix()
        if is_noise(rel):
            continue
        try:
            with file_locks([real]):
                edits.attribute(pid, rel, since=since, until=until, by=by, current=edits.content_at(real),
                                created=tool_kind == "create_file")
        except OSError:
            log.exception("storm file versions: couldn't attribute a file a turn wrote")
