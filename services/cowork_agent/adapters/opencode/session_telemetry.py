"""Read-only Space session telemetry for local OpenCode agent sessions.

OpenCode stores session metadata, tokens, and model choices inside SQLite database:
  macOS/Linux: ~/.local/share/opencode/opencode.db
  Windows: %LOCALAPPDATA%\opencode\opencode.db

This read-only capability queries opencode.db (falling back to JSON/JSONL logs if present)
and extracts exact model IDs (e.g. deepseek-v4-flash-free), tokens, session counts, and daily rollups.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_ID = "opencode"
SOURCE_LABEL = "OpenCode"
META_PRIORITY = 45
COST_STATUS = "unavailable"

MAX_SESSIONS = 500
_BUSY_TIMEOUT_MS = 2000


def _opencode_roots() -> list[Path]:
    roots: list[Path] = []
    configured = (os.getenv("OPENCODE_HOME") or os.getenv("OPENCODE_DATA_DIR") or "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    home = Path.home()
    roots.extend([
        home / ".local" / "share" / "opencode",
        home / ".config" / "opencode",
        home / "AppData" / "Local" / "opencode",
        home / ".opencode",
    ])
    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def runtime_mounts() -> list[Path]:
    """Native directories the managed Docker runtime may read if present."""
    return _opencode_roots()


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


def _extract_model_name(raw: Any) -> str:
    if not raw:
        return "unknown"
    if isinstance(raw, str):
        if raw.startswith("{") and raw.endswith("}"):
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("id"):
                    return str(data["id"])
            except Exception:
                pass
        return raw
    if isinstance(raw, dict) and raw.get("id"):
        return str(raw["id"])
    return str(raw)


def collect_session_telemetry() -> dict:
    roots = _opencode_roots()
    db_path: Path | None = None
    for root in roots:
        cand = root / "opencode.db"
        if cand.is_file():
            db_path = cand
            break

    if db_path:
        return _collect_from_sqlite(db_path)

    return _collect_from_files(roots)


def _collect_from_sqlite(db_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.execute(f"pragma busy_timeout={_BUSY_TIMEOUT_MS}")

    try:
        rows = connection.execute("""
            SELECT 
                id, directory, title, time_created, time_updated, 
                model, cost, tokens_input, tokens_output, tokens_reasoning, 
                tokens_cache_read, tokens_cache_write
            FROM session 
            ORDER BY time_updated DESC
            LIMIT ?
        """, (MAX_SESSIONS,)).fetchall()
    except Exception as exc:
        connection.close()
        raise ValueError(f"Failed to query opencode.db under {db_path}: {exc}") from exc

    sessions: list[dict] = []
    project_keys: set[str] = set()

    daily_models: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    daily_sessions: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    daily_tools: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

    for row in rows:
        (
            sid, directory, title, tc, tu, 
            model_raw, cost_val, input_tok, output_tok, reasoning_tok, 
            cache_read, cache_write
        ) = row

        model = _extract_model_name(model_raw)
        started_at = _parse_timestamp(tc) or _parse_timestamp(tu) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ended_at = _parse_timestamp(tu) or started_at

        fresh = int(input_tok or 0)
        output = int(output_tok or 0)
        cread = int(cache_read or 0)
        cwrite = int(cache_write or 0)
        total_tokens = fresh + output + cread + cwrite

        unclassified = 0
        if total_tokens == 0:
            unclassified = 100
            total_tokens = unclassified

        project_path = str(directory or "")
        proj_name = Path(project_path).name if project_path else "(unknown)"
        if project_path:
            project_keys.add(project_path)

        day = started_at[:10]

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
            "fresh": fresh,
            "output": output,
            "cache_read": cread,
            "cache_write": cwrite,
            "tokens": total_tokens,
            "own_tokens": total_tokens,
            "total_tokens": total_tokens,
            "unclassified": unclassified,
            "breakdown_known": unclassified == 0,
            "cost": float(cost_val or 0.0),
            "cost_known": cost_val is not None and float(cost_val) > 0,
            "tools": [],
            "subagents": [],
        })

    connection.close()

    if not sessions:
        raise ValueError(f"OpenCode database {db_path} contains no sessions")

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
            "db_path": str(db_path),
            "schema_version": None,
            "pricing_version": None,
        },
        "totals": {
            "sessions": len(sessions),
            "tokens": all_tokens,
            "cost_usd": sum(r["cost"] for r in sessions),
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


def _collect_from_files(roots: list[Path]) -> dict:
    session_files: list[Path] = []
    active_root = None

    for root in roots:
        if root.is_dir():
            if not active_root:
                active_root = root
            session_files.extend(list(root.glob("*.json")) + list(root.glob("*.jsonl")))
            for sub in ("sessions", "logs", "transcripts"):
                sub_dir = root / sub
                if sub_dir.is_dir():
                    session_files.extend(list(sub_dir.glob("*.json")) + list(sub_dir.glob("*.jsonl")))

    if not active_root or not session_files:
        raise FileNotFoundError(f"OpenCode directories containing session logs not found under {roots}")

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
            model = "unknown"
            started_at = file_mtime
            ended_at = file_mtime
            total_tokens = 0
            unclassified = 0
            fresh_tokens = 0
            output_tokens = 0

            if file_path.suffix == ".json":
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        sid = str(data.get("id") or data.get("session_id") or sid)
                        project_path = str(data.get("project_path") or data.get("cwd") or "")
                        model = _extract_model_name(data.get("model") or data.get("modelID") or model)
                        started_at = _parse_timestamp(data.get("created_at") or data.get("started_at")) or file_mtime
                        ended_at = _parse_timestamp(data.get("updated_at") or data.get("ended_at")) or file_mtime
                        usage = data.get("usage") or {}
                        fresh_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                        total_tokens = int(usage.get("total_tokens") or (fresh_tokens + output_tokens))
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
        raise ValueError(f"No valid OpenCode sessions found under {roots}")

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
            "db_path": str(active_root),
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
