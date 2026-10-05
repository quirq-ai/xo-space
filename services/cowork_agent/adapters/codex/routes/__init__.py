"""codex adapter-owned routes, mounted only while codex is the active agent."""
from __future__ import annotations

from .remote_control import router

__all__ = ["router"]
