"""
Antigravity (agy) models-status view — the "not logged in" surface.

Mirrors claude_code's ``models_status`` (``{default, models:[{id, status}]}``),
but the source of truth is the **OAuth token file**, not ``claude auth status
--json`` — agy exposes no auth CLI. ``loggedIn`` truthiness → ``status``:
usable token → ``"ok"``, otherwise → ``"error"``. The frontend reads this
(mirrored into xo.json under ``models.status``) and shows the connect / logged-out
affordance exactly as it does for claude_code.
"""
from __future__ import annotations

from typing import Any

from services.cowork_agent.adapters.antigravity.lifecycle.login import logged_in
from services.cowork_agent.adapters.cli_status import CliStatusError  # re-exported for symmetry

_MODEL_ID = "antigravity/gemini-3-5-flash-medium"


def build_status_view(logged_in: bool) -> dict[str, Any]:
    """Translate login state into the common ``{default, models}`` envelope.

    Only ``logged_in`` is consumed, keeping the shape identical to the other
    agents' status adapters."""
    status = "ok" if logged_in else "error"
    return {
        "default": _MODEL_ID,
        "models": [{"id": _MODEL_ID, "status": status}],
    }


async def get_models_status(timeout: float | None = None) -> dict[str, Any]:
    """No CLI call — agy has no ``auth status`` subcommand. Login is read from
    the token file, so this is a pure file check."""
    return build_status_view(logged_in())


__all__ = ["CliStatusError", "build_status_view", "get_models_status"]
