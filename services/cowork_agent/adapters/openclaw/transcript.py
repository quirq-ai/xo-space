"""Tee OpenClaw exchanges into the per-project session index."""

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
    """Record session metadata in the project's session index."""
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
