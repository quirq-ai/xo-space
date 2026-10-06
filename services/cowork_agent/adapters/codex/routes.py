"""codex adapter-owned routes, mounted only while codex is the active agent.

Remote Control: the same ``/api/remote-control/*`` paths and contract as
``adapters/claude_code/routes.py``, plus ``pair``.
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
    """Daemon, CLI, login and enrollment state (read-only)."""
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
