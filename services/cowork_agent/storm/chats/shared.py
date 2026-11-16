"""Files an agent shares, as a message carries them (inline media).

``describe`` gives one shared file what a client needs to show it in place:
its address on ``/api/xo-projects/{project_id}/image|file|download``, its
Made item, when it last changed, and, for an image, ``w``, ``h`` and
``tint`` (``services/media_info.py``). The ``share`` event, the thread's
outputs and the agent's message all carry that same entry.

An agent's message carries the files its session shared in the current turn
since its previous message (``extra_for``): share, then reply, and the reply
shows them. When the turn ends, files shared after its last message join
that message (``LateShares``), with a ``message.updated {id, chat_id,
thread_ts, extra}`` event, as a reaction changes a message. A turn with no
message keeps its files in its shared-files block only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import made
from services.cowork_agent.storm.engine import Engine, Writes
from services.cowork_agent.storm.store import StoredEvent
from services.media_info import image_info
from services.timestamps import iso

log = logging.getLogger(__name__)

#: The fields ``describe`` adds to a shared file's ``{path, name, type, bytes}``.
ADDED_FIELDS = ("project_id", "relative_path", "made_id", "mtime")
IMAGE_FIELDS = ("w", "h", "tint")


def describe(chat_id: str, folder: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    """One shared file, described (blocking: it reads the file's header)."""
    where = made.shared_path(folder, entry["path"])
    out = {**entry, **made.address(where), "made_id": made.file_id(chat_id, where), "mtime": None}
    try:
        out["mtime"] = iso(datetime.fromtimestamp(os.stat(where).st_mtime, timezone.utc))
    except OSError:
        pass
    if str(entry.get("type") or "").startswith("image/"):
        out.update(image_info(os.path.realpath(where), entry.get("type")))
    return out


def _turn(w: Writes, session_id: str) -> str | None:
    row = w.execute("SELECT id FROM turns WHERE session_id = ? AND status = 'running' ORDER BY started_at DESC "
                    "LIMIT 1", (session_id,)).fetchone()
    return row[0] if row else None


def _last_message(w: Writes, session_id: str, turn_id: str) -> tuple[str, int, str] | None:
    """The session's latest message in this turn: ``(id, seq, extra)``."""
    return w.execute(
        "SELECT m.id, m.seq, m.extra FROM messages m JOIN turns t ON t.id = ? WHERE m.author_session_id = ? "
        "AND m.author_kind = 'agent' AND m.created_at >= t.started_at ORDER BY m.seq DESC LIMIT 1",
        (turn_id, session_id)).fetchone()


def _shared_since(w: Writes, session_id: str, turn_id: str, after_seq: int) -> list[dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for (data,) in w.execute("SELECT data FROM events WHERE type = 'share' AND session_id = ? AND turn_id = ? "
                             "AND seq > ? ORDER BY seq", (session_id, turn_id, after_seq)).fetchall():
        for f in json.loads(data).get("files") or []:
            files[f.get("made_id") or f["path"]] = f        # shared twice: the latest description
    return list(files.values())


def extra_for(w: Writes, session_id: str) -> dict[str, Any]:
    """The ``extra`` an agent's new message starts with: what its session
    shared in its current turn since its previous message there."""
    turn_id = _turn(w, session_id)
    if turn_id is None:
        return {}
    last = _last_message(w, session_id, turn_id)
    files = _shared_since(w, session_id, turn_id, last[1] if last else 0)
    return {"attachments": files} if files else {}


def attach_late(w: Writes, session_id: str, turn_id: str) -> str | None:
    """At the turn's end: what was shared after its last message joins that
    message. Returns the message's id when it changed."""
    last = _last_message(w, session_id, turn_id)
    if last is None:
        return None
    message_id, seq, raw = last
    files = _shared_since(w, session_id, turn_id, seq)
    if not files:
        return None
    extra = json.loads(raw)
    attached = {a.get("made_id") or a.get("path") or a.get("id"): a for a in extra.get("attachments") or []}
    for f in files:
        attached[f.get("made_id") or f["path"]] = dict(f)
    extra["attachments"] = list(attached.values())
    w.execute("UPDATE messages SET extra = json(?) WHERE id = ?", (json.dumps(extra), message_id))
    chat_id, thread_ts = w.execute("SELECT chat_id, thread_ts FROM messages WHERE id = ?", (message_id,)).fetchone()
    w.append("message.updated", {"id": message_id, "chat_id": chat_id, "thread_ts": thread_ts, "extra": extra},
             chat_id=chat_id, thread_ts=thread_ts, session_id=session_id)
    return message_id


class LateShares:
    """The engine listener that runs ``attach_late`` at each turn's end."""

    def __init__(self) -> None:
        self.engine: Engine | None = None
        self._tasks: set[asyncio.Task] = set()

    def start(self, engine: Engine) -> None:
        self.engine = engine
        engine.subscribe(self)

    async def stop(self) -> None:
        if self.engine is not None:
            self.engine.unsubscribe(self)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.type == "turn.finished" and event.session_id and event.turn_id and event.chat_id:
                task = asyncio.ensure_future(self._attach(event.session_id, event.turn_id))
                self._tasks.add(task)
                task.add_done_callback(self._done)

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        """Nothing live matters here."""

    async def _attach(self, session_id: str, turn_id: str) -> None:
        def write(w: Writes) -> None:
            if w.execute("SELECT 1 FROM events WHERE type = 'share' AND turn_id = ? LIMIT 1",
                         (turn_id,)).fetchone():
                attach_late(w, session_id, turn_id)
        await self.engine.write(write)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm shared files: attaching a turn's late shares failed", exc_info=task.exception())
