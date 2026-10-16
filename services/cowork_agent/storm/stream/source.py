"""Everything the stream asks of the engine, in one place.

The stream is the engine's ``ports.Listener``; this adapter is the only
module of ``storm/stream/`` that touches the engine, so a change to the
engine's side of the seam is a change here alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.cowork_agent.storm.engine.ports import Clock, Listener
from services.cowork_agent.storm.store import Store
from services.cowork_agent.storm.stream.presenter import Streams

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine import Engine


class Source:
    def __init__(self, engine: "Engine") -> None:
        self.engine = engine

    @property
    def store(self) -> Store:
        return self.engine.store

    @property
    def clock(self) -> Clock:
        return self.engine.clock

    def listen(self, listener: Listener) -> None:
        # The engine has no unsubscribe yet; a closed bus ignores what it hears.
        self.engine.subscribe(listener)

    def streams(self, session_id: str | None) -> Streams:
        """What the session's runtime streams live (its feature list)."""
        live = self.engine._live.get(session_id) if session_id else None
        return Streams.from_features(live.features()) if live is not None else Streams()

    def held(self, session_id: str | None) -> list[str]:
        """The review items the session's calls are held on."""
        live = self.engine._live.get(session_id) if session_id else None
        return sorted(h.item_id for h in live.facts.held.values() if h.item_id) if live is not None else []

    async def require_session(self, session_id: str) -> None:
        """Raises ``SessionNotFound`` (404) for a session the store doesn't have."""
        await self.engine.meta(session_id)
