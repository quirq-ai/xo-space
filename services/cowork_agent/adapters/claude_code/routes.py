"""claude_code adapter-owned routes.

Remote Control lifecycle endpoints — start/stop/status a local
``claude remote-control`` server so the workspace's Claude Code session can be
driven from the Claude mobile app or claude.ai/code. Mounted only when
claude_code is the active agent (the router aggregation resolves the active
agent's ``routes`` module via ``try_load_capability('routes')``), so they never
leak into openclaw/hermes deployments.

One button on the frontend maps to this one workspace-level session:
  * ``POST /api/remote-control/start``  → launch (idempotent)
  * ``POST /api/remote-control/stop``   → stop
  * ``GET  /api/remote-control/status`` → drives the button's on/off state
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from services.cowork_agent.adapters.claude_code import remote_control

router = APIRouter()


class RemoteControlStartBody(BaseModel):
    name: str | None = None


@router.get("/api/remote-control/status")
def remote_control_status():
    """Whether a Remote Control session is live, plus login presence and the
    deep-link URL if known."""
    return remote_control.status()


@router.post("/api/remote-control/start")
def remote_control_start(body: RemoteControlStartBody | None = None):
    """Start the Remote Control session (idempotent). ``name`` sets the label
    shown in the Claude app's Code tab; defaults to the workspace name
    (``XO_WORKSPACE_NAME``, ``CODER_WORKSPACE_NAME``, or the pod hostname
    without its ``coder-`` prefix and random suffix)."""
    return remote_control.start(name=body.name if body else None)


@router.post("/api/remote-control/stop")
def remote_control_stop():
    """Stop the Remote Control session (idempotent)."""
    return remote_control.stop()
