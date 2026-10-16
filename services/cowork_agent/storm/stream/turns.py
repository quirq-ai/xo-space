"""Stored events → frames: the one fold the live stream, a reconnect's
replay and ``GET /api/chat/events`` all run, so each gives the same frames
for the same stored events, in the same order, at the same places."""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Callable, Iterable

from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.stream.frames import PRESENTED_PREFIX, Frame
from services.cowork_agent.storm.stream.presenter import TURN_EVENTS, Out, TurnPresenter

log = logging.getLogger(__name__)

#: A session whose process is gone: a turn still running in it can't finish.
ENDED = frozenset({"cold", "parked"})


@dataclass
class Running:
    """A turn in progress: its presenter and where it belongs."""

    presenter: TurnPresenter
    session_id: str | None
    chat_id: str | None
    thread_ts: str | None
    epoch: int | None

    def frames(self, outs: Iterable[Out]) -> list[Frame]:
        """Live-only frames (no seq) for this turn."""
        p = self.presenter
        return [Frame(o.type, o.data, chat_id=self.chat_id, session_id=self.session_id, thread_ts=self.thread_ts,
                      turn_id=p.turn_id) for o in outs]


class Turns:
    def __init__(self) -> None:
        self.running: dict[str, Running] = {}

    def apply(self, event: StoredEvent) -> list[Frame]:
        """The frames one stored event gives, numbered in order: the blocks it
        starts or finishes (before a ``turn.finished``, so a turn's blocks are
        final when it is), then the event itself. A step is sent as the
        blocks of its turn; one no running turn presents (between turns) is
        sent as it is, so nothing stored is invisible. An event the presenter
        can't read is logged and gives no blocks: it must never stop the
        stream, a replay, or the state log."""
        frames: list[Frame] = []
        presented = False
        try:
            running = self._track(event)
            if running is not None and event.type in TURN_EVENTS:
                presented = event.type.startswith(PRESENTED_PREFIX)
                frames += [Frame.of(event, out.type, out.data) for out in running.presenter.durable(event)]
                if running.presenter.ended:
                    del self.running[event.turn_id]
        except Exception:  # noqa: BLE001 - one row it can't present must not stall every client
            log.exception("storm stream: presenting %s (seq %s) failed", event.type, event.seq)
            frames = []
        if not presented:
            frames.append(Frame.of(event))
        return [dataclasses.replace(f, part=k, last=k == len(frames) - 1) for k, f in enumerate(frames)]

    def _track(self, event: StoredEvent) -> Running | None:
        """Start, find, or drop the turn this event belongs to. A session runs
        one turn at a time, so a new one, the session's process ending, or a
        new epoch (a later process) ends the presenter of one that can no
        longer finish: its state is never kept past that."""
        turn, session = event.turn_id, event.session_id
        if event.type == "session" and session is not None:
            ended = event.data.get("status") in ENDED
            self._drop(session, lambda r: ended or (event.epoch is not None and r.epoch is not None
                                                    and event.epoch > r.epoch))
        if turn is not None and event.type == "turn.started" and turn not in self.running:
            self._drop(session, lambda _r: True)
            self.running[turn] = Running(TurnPresenter(turn), session, event.chat_id, event.thread_ts, event.epoch)
        return self.running.get(turn) if turn is not None else None

    def _drop(self, session: str | None, stale: Callable[[Running], bool]) -> None:
        for turn, running in list(self.running.items()):
            if running.session_id == session and stale(running):
                del self.running[turn]
