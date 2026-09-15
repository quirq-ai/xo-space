"""
OpenClaw rows in the per-project session index, written the way the other
chat backends write theirs.

The row key is the OpenClaw session key the adapter sends in the session
header, ``agent:<openclaw agent>:web:<8hex>``; OpenClaw creates the session
under that key on the first turn. The row's ``sessionId`` is XO's id and its
``nativeSessionId`` is OpenClaw's current transcript id, filled in once the
gateway has created the session and refreshed after every turn together with
the conversation's token usage.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from services.cowork_agent.adapters.openclaw import agent_db
from services.cowork_agent.engine import sessions_io as _session_index

logger = logging.getLogger(__name__)

BACKEND = "openclaw"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def make_session_key(openclaw_agent_id: str, our_session_id: Optional[str]) -> str:
    """``agent:<openclaw agent>:web:<8hex>``. The suffix comes from XO's
    session id, so repeated writes for one session land on one row."""
    suffix = our_session_id.replace("-", "")[:8] if our_session_id else uuid.uuid4().hex[:8]
    return f"agent:{openclaw_agent_id}:web:{suffix}"


def find_session_row(session_id: str) -> Optional[tuple[str, str, dict]]:
    """``(project_id, session_key, row)`` of the OpenClaw row for
    ``session_id`` (any project, or the no-project scope), or None.

    Matches XO's id, or OpenClaw's for rows written before XO minted its own
    ids for OpenClaw chats.
    """
    if not session_id:
        return None
    for project_id, _project_dir, index in _session_index.iter_session_indexes():
        for key, meta in index.items():
            if meta.get("backend") != BACKEND:
                continue
            if session_id in (meta.get("sessionId"), meta.get("nativeSessionId")):
                return project_id, key, meta
    return None


def conversation_usage(openclaw_agent_id: str, native_session_id: str) -> Optional[dict]:
    """Token usage summed over the conversation's assistant messages."""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost": 0.0,
    }
    found = False
    for record in agent_db.read_conversation(openclaw_agent_id, native_session_id):
        if record.get("type") != "message":
            continue
        msg = record.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            continue
        found = True
        try:
            totals["input_tokens"] += int(usage.get("input", 0) or 0)
            totals["output_tokens"] += int(usage.get("output", 0) or 0)
            totals["cache_read_input_tokens"] += int(usage.get("cacheRead", 0) or 0)
            totals["cache_creation_input_tokens"] += int(usage.get("cacheWrite", 0) or 0)
            cost_raw = usage.get("cost", 0)
            totals["cost"] += (
                float(cost_raw.get("total") or 0) if isinstance(cost_raw, dict) else float(cost_raw or 0)
            )
        except (TypeError, ValueError):
            continue
    if not found:
        return None
    totals["cost"] = round(totals["cost"], 6)
    return totals


def write_preliminary_entry(
    project_id: str, session_key: str, session_id: str, directory: str
) -> bool:
    """Write the row BEFORE the request, like the CLI backends do."""
    row = {
        "sessionId": session_id,
        "nativeSessionId": "",
        "directory": directory,
        "backend": BACKEND,
        "updatedAt": _now_ms(),
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    try:
        return _session_index.write_session_row(project_id, session_key, row)
    except Exception as exc:  # noqa: BLE001 — never fail a chat for an index write
        logger.warning("openclaw session index write failed: %s", exc)
        return False


def update_session_row(project_id: str, session_key: str) -> Optional[str]:
    """Record OpenClaw's current transcript id, the conversation's usage and
    ``updatedAt`` on the row. Returns the transcript id (None if unknown)."""
    try:
        meta = dict(_session_index.read_session_index(project_id).get(session_key) or {})
        if not meta:
            return None
        native = agent_db.session_id_for_key(session_key) or meta.get("nativeSessionId") or None
        if native:
            meta["nativeSessionId"] = native
            agent = agent_db.agent_from_session_key(session_key)
            usage = conversation_usage(agent, native) if agent else None
            if usage:
                meta["usage"] = usage
        meta["updatedAt"] = _now_ms()
        _session_index.write_session_row(project_id, session_key, meta)
        return native
    except Exception as exc:  # noqa: BLE001 — never fail a chat for an index write
        logger.warning("openclaw session index update failed: %s", exc)
        return None
