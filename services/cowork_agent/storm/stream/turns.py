"""Stored events → frames: the one fold the live stream, a reconnect's
replay and ``GET /api/chat/events`` all run, so each gives the same frames
for the same stored events, in the same order."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.stream.frames import PRESENTED_PREFIX, Frame
from services.cowork_agent.storm.stream.presenter import TURN_EVENTS, Out, Streams, TurnPresenter


@dataclass
class Running:
    """A turn in progress: its presenter and where it belongs."""

    presenter: TurnPresenter
    session_id: str | None
    chat_id: str | None
    thread_ts: str | None

    def frames(self, outs: Iterable[Out]) -> list[Frame]:
        """Live-only frames (no seq) for this turn."""
        p = self.presenter
        return [Frame(o.type, o.data, chat_id=self.chat_id, session_id=self.session_id, thread_ts=self.thread_ts,
                      turn_id=p.turn_id) for o in outs]


class Turns:
    def __init__(self, streams: Callable[[str | None], Streams] = lambda _session: Streams()) -> None:
        self.running: dict[str, Running] = {}
        self._streams = streams

    def apply(self, event: StoredEvent) -> list[Frame]:
        """The frames one stored event gives: the blocks it starts or
        finishes (before a ``turn.finished``, so a turn's blocks are final
        when it is), then the event itself unless it is a step."""
        frames: list[Frame] = []
        turn = event.turn_id
        if turn is not None and event.type == "turn.started" and turn not in self.running:
            self.running[turn] = Running(TurnPresenter(turn, streams=self._streams(event.session_id)),
                                         event.session_id, event.chat_id, event.thread_ts)
        running = self.running.get(turn) if turn is not None else None
        if running is not None and event.type in TURN_EVENTS:
            frames += [Frame.of(event, out.type, out.data) for out in running.presenter.durable(event)]
            if running.presenter.ended:
                del self.running[turn]
        if not event.type.startswith(PRESENTED_PREFIX):
            frames.append(Frame.of(event))
        return frames
