"""Read-only Space session telemetry for local Kimi Code agent sessions.

Kimi Code stores session metadata and transcripts under:
  macOS/Linux: ~/.kimi-code/sessions/ and ~/.kimi-code/logs/
  Windows: %USERPROFILE%\.kimi-code\sessions\ and %USERPROFILE%\.kimi-code\logs\

This read-only capability scans JSON/JSONL session logs and calculates token
totals, session counts, models, and daily rollups.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_ID = "kimi_code"
SOURCE_LABEL = "Kimi Code"
META_PRIORITY = 30
COST_STATUS = "unavailable"

MAX_SESSIONS = 500
MAX_TOOLS_PER_SESSION = 10


def _kimi_home() -> Path:
    configured = (os.getenv("KIMI_HOME") or os.getenv("KIMI_CODE_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".kimi-code"


def runtime_mounts() -> list[Path]:
    """Native directories the managed Docker runtime may read if present."""
    return [_kimi_home()]


def _parse_timestamp(val: Any) -> str | None:
    if not val:
        return None
    if isinstance(val, (int, float)):
        ms = float(val)
        if ms < 1_000_000_000_000:
            ms *= 1000
        try:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None
    if isinstance(val, str):
        try:
            norm = val.replace("Z", "+00:00")
            return datetime.fromisoformat(norm).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None
    return None


def collect_session_telemetry() -> dict:
    root = _kimi_home()
    sessions_dir = root / "sessions"
    logs_dir = root / "logs"

    if not root.is_dir() and not sessions_dir.is_dir() and not logs_dir.is_dir():
        raise FileNotFoundError(f"Kimi Code directory not found at {root}")

    session_files: list[Path] = []
    for d in (sessions_dir, logs_dir, root):
        if d.is_dir():
            session_files.extend(list(d.glob("*.json")) + list(d.glob("*.jsonl")))

    if not session_files:
        raise ValueError(f"Kimi Code directory {root} contains no log or session files")

    sessions: list[dict] = []
    seen_ids: set[str] = set()
    project_keys: set[str] = set()

    daily_models: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    daily_sessions: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    daily_tools: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

    for file_path in sorted(session_files, key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_SESSIONS]:
        try:
            stat = file_path.stat()
            file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            file_day = file_mtime[:10]

            sid = file_path.stem
            if sid in seen_ids:
                continue
            seen_ids.add(sid)

            project_path = ""
            model = "kimi-k1.5"
            started_at = file_mtime
            ended_at = file_mtime
            total_tokens = 0
            unclassified = 0
            fresh_tokens = 0
            output_tokens = 0
            tool_calls = 0

            # Inspect file content
            if file_path.suffix == ".json":
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        sid = str(data.get("id") or data.get("session_id") or sid)
                        project_path = str(data.get("project_path") or data.get("cwd") or "")
                        model = str(data.get("model") or model)
                        started_at = _parse_timestamp(data.get("created_at") or data.get("started_at")) or file_mtime
                        ended_at = _parse_timestamp(data.get("updated_at") or data.get("ended_at")) or file_mtime
                        usage = data.get("usage") or {}
                        fresh_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                        total_tokens = int(usage.get("total_tokens") or (fresh_tokens + output_tokens))
                except Exception:
                    pass
            elif file_path.suffix == ".jsonl":
                try:
                    lines = file_path.read_text(encoding="utf-8").splitlines()
                    for line in lines:
                        if not line.strip():
                            continue
                        rec = json.loads(line)
                        if isinstance(rec, dict):
                            if rec.get("model"):
                                model = str(rec["model"])
                            if rec.get("cwd") or rec.get("project_path"):
                                project_path = str(rec.get("cwd") or rec.get("project_path"))
                            ts = _parse_timestamp(rec.get("timestamp") or rec.get("time"))
                            if ts:
                                ended_at = ts
                            usage = rec.get("usage") or {}
                            if usage:
                                fresh_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                                output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                                total_tokens += int(usage.get("total_tokens") or 0)
                except Exception:
                    pass

            if total_tokens == 0:
                unclassified = max(1, stat.st_size // 4)
                total_tokens = unclassified

            proj_name = Path(project_path).name if project_path else "(unknown)"
            if project_path:
                project_keys.add(project_path)

            day = started_at[:10] if started_at else file_day

            daily_models[(day, model)][0] += total_tokens
            daily_models[(day, model)][1] += unclassified

            daily_sessions[(day, sid)][0] += total_tokens
            daily_sessions[(day, sid)][1] += unclassified

            sessions.append({
                "id": sid,
                "agent": SOURCE_ID,
                "project": proj_name,
                "project_path": project_path,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_sec": None,
                "model": model,
                "agent_version": "",
                "turns": 1,
                "fresh": fresh_tokens,
                "output": output_tokens,
                "cache_read": 0,
                "cache_write": 0,
                "tokens": total_tokens,
                "own_tokens": total_tokens,
                "total_tokens": total_tokens,
                "unclassified": unclassified,
                "breakdown_known": unclassified == 0,
                "cost": 0.0,
                "cost_known": False,
                "tools": [],
                "subagents": [],
            })
        except Exception:
            continue

    if not sessions:
        raise ValueError(f"No valid Kimi Code sessions found under {root}")

    sessions.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    all_tokens = sum(r["tokens"] for r in sessions)
    kept_ids = {r["id"] for r in sessions}

    return {
        "source": {
            "id": SOURCE_ID,
            "label": SOURCE_LABEL,
            "cost_status": COST_STATUS,
        },
        "meta_priority": META_PRIORITY,
        "meta": {
            "db_path": str(root),
            "schema_version": None,
            "pricing_version": None,
        },
        "totals": {
            "sessions": len(sessions),
            "tokens": all_tokens,
            "cost_usd": 0.0,
            "projects": len(project_keys),
            "sessions_by_agent": {SOURCE_ID: len(sessions)},
        },
        "project_keys": sorted(project_keys),
        "sessions": sessions,
        "daily_models": [
            {
                "day": day, "agent": SOURCE_ID, "model": model,
                "tokens": values[0], "unclassified": values[1],
                "breakdown_known": values[1] == 0,
                "cost": 0.0, "cost_known": False,
            }
            for (day, model), values in sorted(daily_models.items())
        ],
        "daily_sessions": [
            {
                "day": day, "agent": SOURCE_ID, "session_id": session_id,
                "session_key": f"{SOURCE_ID}:{session_id}",
                "tokens": values[0], "unclassified": values[1],
                "breakdown_known": values[1] == 0,
                "cost": 0.0, "cost_known": False,
            }
            for (day, session_id), values in sorted(daily_sessions.items())
            if session_id in kept_ids
        ],
        "daily_tools": [
            {
                "day": day, "agent": SOURCE_ID, "name": name,
                "calls": values[0], "errors": values[1],
            }
            for (day, name), values in sorted(daily_tools.items())
        ],
    }
