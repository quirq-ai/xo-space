"""Read-only Space session telemetry for local OpenClaw sessions.

OpenClaw writes one JSONL transcript per session under
``<agents dir>/<agent>/sessions/`` (``OPENCLAW_AGENTS_DIR``, default
``~/.openclaw/agents``), and every assistant message carries its own usage:
``input``, ``output``, ``cacheRead``, ``cacheWrite`` and a ``cost``. Files are
discovered and parsed by the same functions ``/api/usage`` uses
(``adapters/openclaw/usage.get_session_files`` / ``parse_file``), so both views
count the same messages. Only usage, model, timestamps and tool names are kept;
message text is never retained.

A session's project comes from the per-project session index XO Space writes
for every openclaw chat (``~/.quirq/projects/<pid>/sessions/``).

OpenClaw is not installed when its agents directory does not exist: the source
is then reported unavailable and contributes nothing.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.cowork_agent.adapters.openclaw import usage as openclaw_usage
from services.cowork_agent.engine import sessions_io

SOURCE_ID = "openclaw"
SOURCE_LABEL = "OpenClaw"
META_PRIORITY = 5
COST_STATUS = "estimated"
SOURCE_CONFIG = {
    "vendor": "openclaw",
    "path_env": "OPENCLAW_AGENTS_DIR",
    "path_default": "~/.openclaw/agents",
    "path_kind": "dir",
    "path_label": "OpenClaw agents directory",
    "collects": ["Sessions and models", "Token usage", "Tool calls"],
    "never": "No prompt text stored",
}

MAX_SESSIONS = 500
MAX_TOOLS_PER_SESSION = 10


def _agents_dir() -> Path:
    return Path(openclaw_usage._agents_dir()).expanduser()


def runtime_mounts() -> list[Path]:
    """Native directories the managed Docker runtime may read if present."""
    return [_agents_dir()]


def _iso(epoch_ms: Any) -> str | None:
    if not isinstance(epoch_ms, (int, float)) or epoch_ms <= 0:
        return None
    moment = datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _int(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _cost(usage: dict) -> tuple[float, bool]:
    raw = usage.get("cost")
    if isinstance(raw, dict):
        raw = raw.get("total")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw), True
    return 0.0, False


def _session_id(path: str, meta: dict) -> str:
    sid = meta.get("sessionId")
    if isinstance(sid, str) and sid:
        return sid
    name = os.path.basename(path)
    return name.split(".jsonl", 1)[0]


def _index_directories() -> dict[str, str]:
    """Native session id → project directory, from this backend's index rows."""
    directories: dict[str, str] = {}
    for _project_id, project_dir, index in sessions_io.iter_project_session_indexes():
        for row in index.values():
            if row.get("backend") != SOURCE_ID:
                continue
            native = row.get("nativeSessionId") or row.get("sessionId")
            if native:
                directories[str(native)] = str(row.get("directory") or project_dir)
    return directories


def collect_session_telemetry() -> dict:
    agents_dir = _agents_dir()
    if not agents_dir.is_dir():
        raise FileNotFoundError(
            f"OpenClaw agents directory not found at {agents_dir} (set OPENCLAW_AGENTS_DIR to change)"
        )

    directories = _index_directories()
    daily_models: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0, True])
    daily_sessions: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0, True])
    daily_tools: dict[tuple[str, str], int] = defaultdict(int)
    sessions: dict[str, dict] = {}
    unreadable = 0

    for path in openclaw_usage.get_session_files():
        try:
            meta, entries = openclaw_usage.parse_file(path)
        except (OSError, UnicodeDecodeError, ValueError):
            unreadable += 1
            continue
        sid = _session_id(path, meta)
        session = sessions.setdefault(sid, {
            "first": None, "last": None, "model": None, "turns": 0,
            "fresh": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "cost": 0.0, "cost_known": True, "tools": defaultdict(int),
        })
        for entry in entries:
            ts = entry.get("timestamp")
            if isinstance(ts, (int, float)) and ts > 0:
                session["first"] = ts if session["first"] is None else min(session["first"], ts)
                session["last"] = ts if session["last"] is None else max(session["last"], ts)
            if entry.get("role") == "user":
                session["turns"] += 1
                continue
            usage = entry.get("usage") or {}
            fresh, output = _int(usage.get("input")), _int(usage.get("output"))
            cache_read, cache_write = _int(usage.get("cacheRead")), _int(usage.get("cacheWrite"))
            tokens = fresh + output + cache_read + cache_write
            amount, known = _cost(usage)
            model = entry.get("model") or "unknown"
            session["fresh"] += fresh
            session["output"] += output
            session["cache_read"] += cache_read
            session["cache_write"] += cache_write
            session["cost"] += amount
            session["cost_known"] = session["cost_known"] and known
            if entry.get("model"):
                session["model"] = entry["model"]
            day = (_iso(ts) or "")[:10]
            if day:
                for values in (daily_models[(day, model)], daily_sessions[(day, sid)]):
                    values[0] += tokens
                    values[1] += amount
                    values[2] = values[2] and known
            for name in entry.get("toolNames") or []:
                session["tools"][name] += 1
                if day:
                    daily_tools[(day, name)] += 1

    meaningful = []
    for sid, session in sessions.items():
        session["tokens"] = (
            session["fresh"] + session["output"] + session["cache_read"] + session["cache_write"]
        )
        if session["tokens"] > 0:
            meaningful.append((sid, session))
    meaningful.sort(key=lambda item: item[1]["first"] or 0, reverse=True)

    rows = []
    projects: set[str] = set()
    for sid, session in meaningful:
        project_path = directories.get(sid, "")
        projects.add(project_path or "(unknown)")
        if len(rows) >= MAX_SESSIONS:
            continue
        first, last = session["first"], session["last"]
        rows.append({
            "id": sid,
            "key": f"{SOURCE_ID}:{sid}",
            "agent": SOURCE_ID,
            "project": project_path.rstrip("/").rsplit("/", 1)[-1] if project_path else "(unknown)",
            "project_path": project_path or None,
            "started_at": _iso(first),
            "ended_at": _iso(last),
            "duration_sec": max(0, int((last - first) / 1000)) if first and last else None,
            "model": session["model"],
            "agent_version": None,
            "turns": session["turns"],
            "fresh": session["fresh"],
            "output": session["output"],
            "cache_read": session["cache_read"],
            "cache_write": session["cache_write"],
            "tokens": session["tokens"],
            "own_tokens": session["tokens"],
            "total_tokens": session["tokens"],
            "unclassified": 0,
            "breakdown_known": True,
            "cost": session["cost"],
            "cost_known": session["cost_known"],
            "tools": [
                {"name": name, "calls": calls, "errors": 0}
                for name, calls in sorted(session["tools"].items(), key=lambda item: (-item[1], item[0]))
            ][:MAX_TOOLS_PER_SESSION],
            "subagents": [],
        })

    kept_ids = {row["id"] for row in rows}
    return {
        "source": {
            "id": SOURCE_ID,
            "label": SOURCE_LABEL,
            "cost_status": COST_STATUS,
            "unreadable_files": unreadable,
        },
        "meta_priority": META_PRIORITY,
        "meta": {"db_path": str(agents_dir), "schema_version": None, "pricing_version": None},
        "totals": {
            "sessions": len(meaningful),
            "tokens": sum(session["tokens"] for _sid, session in meaningful),
            "cost_usd": sum(session["cost"] for _sid, session in meaningful),
            "projects": len(projects),
            "sessions_by_agent": {SOURCE_ID: len(meaningful)},
        },
        "project_keys": sorted(projects),
        "sessions": rows,
        "daily_models": [
            {
                "day": day, "agent": SOURCE_ID, "model": model,
                "tokens": values[0], "unclassified": 0, "breakdown_known": True,
                "cost": values[1], "cost_known": values[2],
            }
            for (day, model), values in sorted(daily_models.items())
        ],
        "daily_sessions": [
            {
                "day": day, "agent": SOURCE_ID, "session_id": session_id,
                "session_key": f"{SOURCE_ID}:{session_id}",
                "tokens": values[0], "unclassified": 0, "breakdown_known": True,
                "cost": values[1], "cost_known": values[2],
            }
            for (day, session_id), values in sorted(daily_sessions.items())
            if session_id in kept_ids
        ],
        "daily_tools": [
            {"day": day, "agent": SOURCE_ID, "name": name, "calls": calls, "errors": 0}
            for (day, name), calls in sorted(daily_tools.items())
        ],
    }
