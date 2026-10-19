"""Everything the stream asks of the engine, in one place, through the
engine's public view only.

The bus is the engine's ``ports.Listener`` from boot (``boot(listeners=…)``),
before the engine object exists; it is bound to the engine once ``boot``
returns. What needs the engine before then waits for that.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Mapping

from services.cowork_agent.adapters.contract import FeatureSupport
from services.cowork_agent.storm.store import Store

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine import Engine
    from services.cowork_agent.storm.engine.ports import Listener


class Source:
    def __init__(self) -> None:
        self._engine: "Engine | None" = None
        self._bound = asyncio.Event()

    def bind(self, engine: "Engine") -> None:
        self._engine = engine
        self._bound.set()

    @property
    def engine(self) -> "Engine | None":
        return self._engine

    @property
    def store(self) -> Store:
        assert self._engine is not None, "the stream reads the store only once bound"
        return self._engine.store

    def unlisten(self, listener: "Listener") -> None:
        if self._engine is not None:
            self._engine.unsubscribe(listener)

    async def features(self, session_id: str) -> Mapping[str, FeatureSupport]:
        """What the session's runtime offers (its feature list)."""
        await self._bound.wait()
        return (await self._engine.features(session_id)).features

    async def held_items(self, session_id: str) -> list[str]:
        """The review items the session's calls are held on."""
        await self._bound.wait()
        return await self._engine.held_items(session_id)

    async def require_session(self, session_id: str) -> None:
        """Raises ``SessionNotFound`` (404) for a session the store doesn't have."""
        await self._bound.wait()
        await self._engine.meta(session_id)
