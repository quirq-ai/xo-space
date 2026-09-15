"""
Publish one row into the per-project session index after each hermes streaming
exchange.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from services.cowork_agent.engine import sessions_io as _session_index
from services.cowork_agent.project_layout import project_dir as _xo_project_dir

logger = logging.getLogger(__name__)


def _composite_key(agent_id: str, our_session_id: str) -> str:
    """``hermes:<agent_id>:<surface>:<8hex>`` — mirrors the openclaw
    shape parsed by the visualizer (sessions_io / source).

    The 8-hex suffix is derived from ``our_session_id`` so the same
    xo-cowork session always maps to the same composite key (and so
    repeated writes overwrite the same row instead of accumulating
    duplicates).
    """
    suffix = our_session_id.replace("-", "")[:8] if our_session_id else "00000000"
    return f"hermes:{agent_id}:web:{suffix}"


def write_session_row(
    *,
    agent_id: Optional[str],
    our_session_id: Optional[str],
    native_session_id: Optional[str],
) -> None:
    """Upsert one row in the project's session index."""
    if not agent_id or not native_session_id:
        return
    try:
        composite = _composite_key(agent_id, our_session_id or native_session_id)
        existing = _session_index.read_session_index(agent_id).get(composite) or {}

        # Merge onto the row that is already there instead of rebuilding it.
        entry = dict(existing)
        # ``usage`` is carried forward, not recomputed here (see the module
        # docstring); seed the zero block only when there is nothing to carry.
        if not entry.get("usage"):
            entry["usage"] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        entry.update({
            "sessionId": our_session_id or native_session_id,
            "nativeSessionId": native_session_id,
            "directory": str(_xo_project_dir(agent_id)),
            "backend": "hermes",
            "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        })

        _session_index.write_session_row(agent_id, composite, entry)
    except Exception as exc:  # noqa: BLE001 — never fail a chat for a dashboard write
        logger.warning("hermes session index write failed: %s", exc)
