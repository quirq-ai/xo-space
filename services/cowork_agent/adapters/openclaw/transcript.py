"""
Tee OpenClaw exchanges into the per-project session index.

OpenClaw's gateway is the source of truth for session state (used for
resume via the session-key header). This module publishes a row into the
project's session index so the harness sees an openclaw session exactly the
way it sees any other backend's.

The target project is determined by the explicit ``xo_agent_id`` argument
(the subdirectory name under ``~/xo-projects/``).

Two behaviours changed with the tier move (syncplan T19). The index is
machine-local now, so nothing is written inside the project folder at all;
and the write goes through ``engine.sessions_io``, which resolves the folder
name properly and **skips** a project that does not exist. This module used
to build the path by hand and ``mkdir(parents=True)`` it, which conjured an
empty ghost project — one that then registered as a project of its own.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from services.cowork_agent.engine import sessions_io as _session_index
from services.cowork_agent.project_layout import project_dir as _xo_project_dir
from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR


def _sum_usage(session_id: str, agent_name: str) -> dict | None:
    """Sum token usage across all assistant messages in the native OpenClaw JSONL."""
    path = AGENTS_DIR / agent_name / "sessions" / f"{session_id}.jsonl"
    if not path.exists():
        return None
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost": 0.0}
    found = False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "message":
                continue
            msg = record.get("message", {})
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            found = True
            totals["input_tokens"] += int(usage.get("input", 0) or 0)
            totals["output_tokens"] += int(usage.get("output", 0) or 0)
            totals["cache_read_input_tokens"] += int(usage.get("cacheRead", 0) or 0)
            totals["cache_creation_input_tokens"] += int(usage.get("cacheWrite", 0) or 0)
            cost_raw = usage.get("cost", 0)
            cost_val = float(cost_raw.get("total") or 0) if isinstance(cost_raw, dict) else float(cost_raw or 0)
            totals["cost"] += cost_val
    except Exception:
        pass
    if not found:
        return None
    totals["cost"] = round(totals["cost"], 6)
    return totals


def tee_exchange(
    session_key: str,
    session_id: str,
    question: str,
    response_text: str,
    model_id: str = "",
    xo_agent_id: str | None = None,
) -> None:
    """Record session metadata in the project's session index.

    Messages are NOT stored here — they live in the OpenClaw native session
    files under ``~/.openclaw/agents/<id>/sessions/``. This keeps the project
    folder free of chat content so it can be shared safely.

    ``xo_agent_id`` is the subdirectory name under ``~/xo-projects/``. When
    not supplied the function returns without writing — agent-only chats
    (no project selected) are not mirrored; the openclaw native files are the
    source of truth in that case. A project folder that does not exist is
    likewise a skip, decided inside ``engine.sessions_io``.
    """
    if not xo_agent_id or not session_id:
        return
    agent_id = xo_agent_id

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    entry = dict(_session_index.read_session_index(agent_id).get(session_key) or {})
    entry.update({
        "sessionId": session_id,
        "nativeSessionId": session_id,
        "directory": str(_xo_project_dir(agent_id)),
        "backend": "openclaw",
        "updatedAt": now_ms,
    })

    # Read cumulative token usage directly from the native OpenClaw JSONL.
    # Summing the whole file on each turn avoids double-counting — we replace
    # rather than accumulate, so it stays accurate even if tee_exchange is
    # called multiple times for the same session.
    agent_name = session_key.split(":")[1] if ":" in session_key else "main"
    usage_totals = _sum_usage(session_id, agent_name)
    if usage_totals:
        entry["usage"] = usage_totals

    _session_index.write_session_row(agent_id, session_key, entry)
