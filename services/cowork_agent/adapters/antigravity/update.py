"""``agy update``: agy's own updater, run when a person asks for it (the
Connect flow's or settings' "Update" action), its output passed on line by
line.

XO never updates agy on its own: sessions run with agy's auto-update off
(``AGY_CLI_DISABLE_AUTO_UPDATE=true``), so the CLI never changes under a
running session, and an older agy keeps working (on print mode) until the
person updates it. A running session keeps the binary it started with; the
next session starts the new one. One update runs at a time.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

from services.cowork_agent.adapters.antigravity.runtime.core import _VERSION, find_binary, update_note
from services.errors import ServiceError
from utils.commands import run, streaming

UPDATE_TIMEOUT_S = 300.0
VERSION_TIMEOUT_S = 30.0
_lock = asyncio.Lock()


class UpdateFailed(ServiceError):
    pass


async def version_of(binary: str) -> str | None:
    result = await run([binary, "--version"], timeout=VERSION_TIMEOUT_S, log_output=False)
    found = _VERSION.search(result.output) if result.ok else None
    return found.group(1) if found else None


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["BROWSER"] = "true"                 # never open a browser on the server
    env["DISPLAY"] = ""
    return env


async def update(*, heartbeat_s: float = 15.0) -> AsyncIterator[dict]:
    """``agy update``, as events: ``installing`` (the version before), one
    ``install_log`` per line it prints, ``heartbeat`` while it is quiet, then
    ``done`` (the version after, and any update still wanted). A failure
    raises ``UpdateFailed``; so does a second update while one runs."""
    if _lock.locked():
        raise UpdateFailed("update_running", "agy is already being updated", status=409)
    async with _lock:
        binary = find_binary()
        before = await version_of(binary)
        if before is None:
            raise UpdateFailed("cli_missing", f"agy can't be run ({binary}); install it first", status=404)
        yield {"type": "installing", "package": "agy", "version": before}
        lines: asyncio.Queue[str | None] = asyncio.Queue()

        async def pump(stream: asyncio.StreamReader | None) -> None:
            try:
                while stream is not None:
                    raw = await stream.readline()
                    if not raw:
                        break
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    if text:
                        await lines.put(text)
            finally:
                await lines.put(None)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + UPDATE_TIMEOUT_S
        async with streaming([binary, "update"], env=_env()) as proc:
            pumps = [asyncio.create_task(pump(proc.stdout)), asyncio.create_task(pump(proc.stderr))]
            try:
                open_streams = len(pumps)
                while open_streams:
                    left = deadline - loop.time()
                    if left <= 0:
                        raise UpdateFailed("timeout", f"agy update didn't finish within {UPDATE_TIMEOUT_S:g} s",
                                           status=504)
                    try:
                        line = await asyncio.wait_for(lines.get(), min(left, heartbeat_s))
                    except asyncio.TimeoutError:
                        yield {"type": "heartbeat"}
                        continue
                    if line is None:
                        open_streams -= 1
                        continue
                    yield {"type": "install_log", "line": line}
                try:
                    code = await asyncio.wait_for(proc.wait(), max(deadline - loop.time(), 1.0))
                except asyncio.TimeoutError:
                    raise UpdateFailed("timeout", f"agy update didn't finish within {UPDATE_TIMEOUT_S:g} s",
                                       status=504) from None
            finally:
                for task in pumps:
                    task.cancel()
        if code != 0:
            raise UpdateFailed("update_failed", f"agy update failed (exit code {code})", status=502)
        after = await version_of(binary)
        yield {"type": "done", "returncode": 0, "version_before": before, "version": after,
               "update": update_note(after)}


__all__ = ["UpdateFailed", "update", "version_of"]
