"""
Aggregated live-events broadcaster for the ``/api/events`` SSE feed.

Holds a registry of named section fetchers (models, data, skills, ...) and
assembles them into one snapshot dict. Adding a new section to the feed means
writing one async fetcher and adding one registry entry — the loop, diffing,
and route never change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from services.cowork_agent.adapters.loader import load_capability as _load_capability

logger = logging.getLogger(__name__)

SectionFetcher = Callable[[], Awaitable[dict[str, Any]]]


async def gather_named(
    probes: dict[str, SectionFetcher], fallback: dict[str, Any]
) -> dict[str, Any]:
    """Run named probes concurrently; a failing probe yields `fallback`."""
    names = list(probes)
    results = await asyncio.gather(
        *(probes[name]() for name in names), return_exceptions=True
    )
    out: dict[str, Any] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            logger.warning("events probe %r failed: %s", name, result)
            out[name] = fallback
        else:
            out[name] = result
    return out


def flatten_providers(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a ``/providers/status`` payload into ``{name: {connected}}``.

    ``oauth`` and ``api_keys`` key sets are disjoint by construction
    (agent runtimes vs. key vendors), so a plain merge is safe.
    """
    return {**payload.get("oauth", {}), **payload.get("api_keys", {})}


def rclone_remote_status(available: bool, remotes: list) -> dict[str, str]:
    """Derive a connector status from rclone daemon reachability + remotes."""
    if not available:
        return {"status": "unavailable"}
    return {"status": "connected" if remotes else "needs_auth"}


# ---------------------------------------------------------------------------
# Default section fetchers — the real sources behind /api/events.
# Adding a new section = one async fetcher + one entry in default_sections().
# ---------------------------------------------------------------------------


async def _fetch_models() -> dict[str, Any]:
    """Provider connection state via the active agent's capability module.

    Goes through the capability loader exactly like /providers/status, so no
    agent name appears here (modularity invariant). A missing capability
    raises and the broadcaster reports the section as empty.
    """
    mod = _load_capability("providers_status")
    return flatten_providers(await mod.get_providers_status())


async def _fetch_data() -> dict[str, Any]:
    """Connector statuses, one probe per connector, individually isolated."""
    from services.cowork_agent.connectors import github_connector, vercel_connector
    from services.cowork_agent.connectors.gdrive_rclone import (
        list_drive_remotes,
        rclone_available,
    )
    from services.cowork_agent.connectors.onedrive_rclone import list_onedrive_remotes

    async def github() -> dict[str, Any]:
        return {"status": (await github_connector.get_status()).get("status", "error")}

    async def vercel() -> dict[str, Any]:
        return {"status": (await vercel_connector.get_status()).get("status", "error")}

    async def gdrive() -> dict[str, Any]:
        available = await rclone_available()
        remotes = await list_drive_remotes() if available else []
        return rclone_remote_status(available, remotes)

    async def onedrive() -> dict[str, Any]:
        available = await rclone_available()
        remotes = await list_onedrive_remotes() if available else []
        return rclone_remote_status(available, remotes)

    return await gather_named(
        {"github": github, "vercel": vercel, "gdrive": gdrive, "onedrive": onedrive},
        fallback={"status": "error"},
    )


async def _fetch_skills() -> dict[str, Any]:
    """Static default — there is no install-state source for skills yet."""
    return {"okx": {"installed": True}}


async def _fetch_channels() -> dict[str, Any]:
    """Channel connection state via the active agent's capability module.

    Agents without channels (claude_code, codex) return the empty envelope,
    so the section degrades to ``{}`` uniformly.
    """
    mod = _load_capability("channels_status")
    payload = await mod.get_channels_status()
    return payload.get("channels", {})


def default_sections() -> dict[str, SectionFetcher]:
    return {
        "models": _fetch_models,
        "data": _fetch_data,
        "skills": _fetch_skills,
        "channels": _fetch_channels,
    }


class EventsBroadcaster:
    """Assembles per-section snapshots for the events feed."""

    def __init__(self, sections: dict[str, SectionFetcher], interval: float = 60.0):
        self._sections = sections
        self._interval = interval
        self._loop_task: asyncio.Task | None = None
        # Last successful value per section; a failing fetcher never removes
        # data the frontend already has, it just goes stale until recovery.
        self._last: dict[str, dict[str, Any]] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._refresh_pending = False
        # Last snapshot pushed to subscribers; poll_once() only publishes
        # when the fresh snapshot differs from this.
        self._published: dict[str, Any] | None = None

    @property
    def loop_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    def subscribe(self) -> asyncio.Queue:
        """Register a subscriber; returns the queue snapshots are pushed to.

        The first subscriber starts the poll loop; while nobody is connected
        the feed costs nothing (no external probes at all).
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        if not self.loop_running:
            self._loop_task = asyncio.create_task(self._poll_loop())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber; the last one out stops the poll loop."""
        self._subscribers.discard(queue)
        if not self._subscribers and self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None
            # Force a fresh initial push for the next subscriber even if the
            # snapshot hasn't changed by then.
            self._published = None

    def request_refresh(self, delay: float = 0.5) -> None:
        """Ask for an out-of-cycle probe round soon (e.g. after an auth
        mutation), so UI-driven changes reflect in ~1s instead of waiting
        for the next interval tick.

        No-op when nobody is subscribed. Rapid successive calls collapse
        into one round: `delay` doubles as the debounce window and lets the
        mutation that triggered us finish settling before we probe.
        """
        if not self._subscribers or self._refresh_pending:
            return
        self._refresh_pending = True

        async def _refresh() -> None:
            try:
                await asyncio.sleep(delay)
                await self.poll_once()
            except Exception:
                logger.exception("events refresh probe failed")
            finally:
                self._refresh_pending = False

        asyncio.create_task(_refresh())

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                # poll_once already isolates per-section errors; this guards
                # the loop itself so it never dies silently.
                logger.exception("events poll loop iteration failed")
            await asyncio.sleep(self._interval)

    async def current_snapshot(self) -> dict[str, Any]:
        """Return the latest snapshot, building one if none exists yet."""
        if self._published is None:
            self._published = await self.build_snapshot()
        return self._published

    async def poll_once(self) -> None:
        """Build a fresh snapshot and push it to subscribers if it changed."""
        snapshot = await self.build_snapshot()
        if snapshot == self._published:
            return
        self._published = snapshot
        for queue in self._subscribers:
            queue.put_nowait(snapshot)

    async def build_snapshot(self) -> dict[str, Any]:
        """Fetch every registered section concurrently and assemble one dict.

        A section whose fetcher raises contributes its last known value
        (``{}`` if it has never succeeded) so one broken source can't take
        down the whole feed.
        """
        names = list(self._sections)
        results = await asyncio.gather(
            *(self._sections[name]() for name in names),
            return_exceptions=True,
        )
        snapshot: dict[str, Any] = {}
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                logger.warning("events section %r failed: %s", name, result)
                snapshot[name] = self._last.get(name, {})
            else:
                self._last[name] = result
                snapshot[name] = result
        return snapshot
