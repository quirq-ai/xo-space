"""What travels on storm's streams (design ch 09 §3): one envelope for every
event, the subscriber's filter, and the SSE encoding.

A frame derived from a stored event carries that event's ``seq``, which is
its SSE ``id:`` (the reconnect point). One stored event may give several
frames (a narration finishing as a tool call starts its group); they share
its seq and are sent together. A frame without a seq is live only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from services.cowork_agent.storm.store import StoredEvent

#: Sent whatever the filter: they are about the connection itself.
CONTROL = frozenset({"snapshot", "reset", "heartbeat"})
#: Stored events kept for the presenter and the state log, not sent as they
#: are: their blocks are (ch 09 §2).
PRESENTED_PREFIX = "step."


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

    @classmethod
    def of(cls, event: StoredEvent, type_: str | None = None, data: Mapping[str, Any] | None = None) -> "Frame":
        """A frame derived from a stored event: its seq, time and ids."""
        return cls(type_ or event.type, event.data if data is None else data, event.seq, event.ts, event.chat_id,
                   event.session_id, event.thread_ts, event.turn_id, event.run_id)

    def wire(self) -> dict[str, Any]:
        return {"type": self.type, "seq": self.seq, "ts": self.ts, "chat_id": self.chat_id,
                "session_id": self.session_id, "thread_ts": self.thread_ts, "turn_id": self.turn_id,
                "run_id": self.run_id, "data": self.data}

    def sse(self) -> str:
        head = f"id: {self.seq}\n" if self.seq is not None else ""
        return f"{head}event: {self.type}\ndata: {json.dumps(self.wire(), separators=(',', ':'), default=str)}\n\n"


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
