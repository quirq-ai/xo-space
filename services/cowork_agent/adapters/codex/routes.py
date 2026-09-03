"""codex adapter-owned routes.

Remote Control lifecycle endpoints — start/pair/stop/status the codex
app-server daemon in remote-control mode so this machine can be driven from
the ChatGPT app. Mounted only when codex is the active agent (the router
aggregation resolves the active agent's ``routes`` module via
``try_load_capability('routes')``), so they never collide with claude_code's
own ``/api/remote-control/*`` routes.

Same paths and response contract as ``adapters/claude_code/routes.py`` so one
frontend button drives either runtime, plus the codex-only pairing step:
  * ``GET  /api/remote-control/status`` → daemon/enrollment state (read-only)
  * ``POST /api/remote-control/start``  → start the daemon (idempotent)
  * ``POST /api/remote-control/pair``   → mint a short-lived pairing code
  * ``POST /api/remote-control/stop``   → stop the daemon (idempotent)

Expected failures come back as ``{ok: False, error, detail}`` with HTTP 200,
as claude_code's do. Pairing codes are credentials: returned once, never
logged.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from services.cowork_agent.adapters.codex import remote_control

router = APIRouter()


class RemoteControlStartBody(BaseModel):
    name: str | None = None  # accepted for parity with claude_code; codex names the server itself


@router.get("/api/remote-control/status")
async def remote_control_status() -> dict[str, Any]:
    """Whether the remote-control daemon is running, plus CLI, login and
    enrollment facts. Never starts anything."""
    return await remote_control.get_status()


@router.post("/api/remote-control/start")
async def remote_control_start(body: RemoteControlStartBody | None = None) -> dict[str, Any]:
    """Start the daemon with remote control enabled (idempotent)."""
    return await remote_control.start(name=body.name if body else None)


@router.post("/api/remote-control/pair")
async def remote_control_pair() -> dict[str, Any]:
    """Mint a short-lived, single-use pairing code for the ChatGPT app."""
    return await remote_control.pair()


@router.post("/api/remote-control/stop")
async def remote_control_stop() -> dict[str, Any]:
    """Stop the daemon (idempotent)."""
    return await remote_control.stop()
