"""What travels on storm's streams (design ch 09 §3): one envelope for every
event, its place in the stream, the subscriber's filter, and the SSE
encoding.

A frame derived from a stored event has a place: the event's ``seq`` and
its index among the frames that event gives (``part``). One stored event
may give several (a narration finishing as a tool call starts its group),
each its own SSE message, and a connection can drop between them, so the
SSE ``id:`` names the frame: ``N:k`` for frame k of seq N, plain ``N`` for
its last frame. A resume from ``N:k`` replays seq N from frame k+1; from
``N`` (or ``after_seq=N``), everything after seq N. The parts are counted
before any filter, so every subscriber agrees on them. A frame without a seq
is live only and has no id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from services.cowork_agent.storm.store import StoredEvent
from services.errors import ServiceError

#: Sent whatever the filter: they are about the connection itself.
CONTROL = frozenset({"snapshot", "reset", "heartbeat"})
#: Stored events kept for the presenter and the state log, not sent as they
#: are: their blocks are (ch 09 §2).
PRESENTED_PREFIX = "step."
#: SQLite's largest integer: no seq is beyond it.
MAX_SEQ = 2**63 - 1
_POINT = re.compile(r"([0-9]{1,19})(?::([0-9]{1,9}))?")


class BadResumePoint(ServiceError):
    def __init__(self, text: str) -> None:
        super().__init__("bad_last_event_id", f"Last-Event-ID must be an id this stream sent (N or N:k), not "
                                              f"{text[:40]!r}; reconnect without it to start from now.", status=400)


def resume_point(after_seq: int | None, last_event_id: str | None) -> "Point | None":
    """Where a stream resumes: ``Last-Event-ID`` (EventSource sends it on
    its own reconnects, so it is newer than the URL's ``after_seq``), else
    after all of seq ``after_seq``; None is "from now". Raises
    ``BadResumePoint`` (400) for an id this stream can't have sent."""
    if last_event_id is None or not last_event_id.strip():
        return None if after_seq is None else Point(after_seq)
    point = Point.parse(last_event_id)
    if point is None:
        raise BadResumePoint(last_event_id)
    return point


@dataclass(frozen=True)
class Frame:
    type: str
    data: Mapping[str, Any]
    seq: int | None = None
    ts: str | None = None
    chat_id: str | None = None
    session_id: str | None = None
    thread_ts: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    part: int = 0                     # its index among its stored event's frames
    last: bool = True                 # the last of them

    @classmethod
    def of(cls, event: StoredEvent, type_: str | None = None, data: Mapping[str, Any] | None = None) -> "Frame":
        """A frame derived from a stored event: its seq, time and ids."""
        return cls(type_ or event.type, event.data if data is None else data, event.seq, event.ts, event.chat_id,
                   event.session_id, event.thread_ts, event.turn_id, event.run_id)

    @property
    def id(self) -> str | None:
        return None if self.seq is None else Point.after(self).id

    def wire(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "seq": self.seq, "ts": self.ts, "chat_id": self.chat_id,
                "session_id": self.session_id, "thread_ts": self.thread_ts, "turn_id": self.turn_id,
                "run_id": self.run_id, "data": self.data}

    def sse(self) -> str:
        head = f"id: {self.id}\n" if self.seq is not None else ""
        return f"{head}event: {self.type}\ndata: {json.dumps(self.wire(), separators=(',', ':'), default=str)}\n\n"


@dataclass(frozen=True)
class Point:
    """A place in the stream: after every frame of stored event ``seq``
    (``part`` None), or after its frames 0..``part``."""

    seq: int
    part: int | None = None

    @classmethod
    def parse(cls, text: str) -> "Point | None":
        """``N`` or ``N:k`` in ASCII digits, N within SQLite's range; None
        for anything else."""
        match = _POINT.fullmatch(text.strip())
        if match is None or int(match[1]) > MAX_SEQ:
            return None
        return cls(int(match[1]), None if match[2] is None else int(match[2]))

    @classmethod
    def after(cls, frame: Frame) -> "Point":
        assert frame.seq is not None
        return cls(frame.seq, None if frame.last else frame.part)

    @property
    def id(self) -> str:
        return str(self.seq) if self.part is None else f"{self.seq}:{self.part}"

    def covers(self, frame: Frame) -> bool:
        """Whether a client at this point already has ``frame``."""
        if frame.seq is None:
            return False
        return frame.seq < self.seq or (frame.seq == self.seq and (self.part is None or frame.part <= self.part))


@dataclass(frozen=True)
class Filter:
    """One subscriber's view (ch 09 §4): a chat, a session, event kinds, or
    everything. A kind names a type (``turn.finished``) or its family
    (``turn``, ``block``)."""

    chat: str | None = None
    session: str | None = None
    kinds: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def parse(cls, *, chat: str | None = None, session: str | None = None, kinds: str | None = None) -> "Filter":
        return cls(chat or None, session or None, frozenset(k.strip() for k in (kinds or "").split(",") if k.strip()))

    def wants_kind(self, type_: str) -> bool:
        if not self.kinds or type_ in CONTROL:
            return True
        return type_ in self.kinds or type_.split(".", 1)[0] in self.kinds

    def wants_ids(self, chat_id: str | None, session_id: str | None) -> bool:
        return (self.chat is None or chat_id == self.chat) and (self.session is None or session_id == self.session)

    def matches(self, frame: Frame) -> bool:
        if frame.type in CONTROL:
            return True
        return self.wants_kind(frame.type) and self.wants_ids(frame.chat_id, frame.session_id)

    @property
    def wants_blocks(self) -> bool:
        return self.wants_kind("block.updated")


def control(type_: str, data: Mapping[str, Any]) -> Frame:
    return Frame(type_, data)
