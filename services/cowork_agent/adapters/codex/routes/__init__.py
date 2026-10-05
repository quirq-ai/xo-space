"""codex adapter-owned routes.

Mounted only when codex is the active agent: the router aggregation resolves
the active agent's ``routes`` module via ``try_load_capability('routes')`` and
mounts its ``router``. Endpoint handlers live in modules in this package; the
package exposes their ``router`` as its own.
"""
from __future__ import annotations

from .remote_control import router

__all__ = ["router"]
