"""
OpenClaw usage — discovery + parser + dashboard aggregator.

Loaded by ``services.cowork_agent.engine.usage_loader.load_usage_module()`` when
``AGENT_NAME=openclaw``. Public contract (also satisfied by claude_code/hermes):

    get_session_files(*, agent_id=None) -> list[str]
    parse_file(path, *, start_ms, end_ms) -> tuple[meta, entries]
    build_summary(meta, entries) -> dict
    dashboard / analytics / summary / summary_card / list_sessions / get_session
    aggregate_for_sync(*, since_date=None) / sync_payload

This module owns only what is OpenClaw-specific: where session transcripts live
(each agent's SQLite store, see ``agent_db``, or for an agent without one
``OPENCLAW_AGENTS_DIR/<agent>/sessions/*.jsonl`` with gateway filename rules),
how one gateway record maps to a normalized usage entry (``parse_file``), and
the ``/api/usage`` dashboard rollup. Everything that operates purely on the
normalized entries — summaries, analytics, sessions, and the daily sync rollup —
is shared with claude_code via :mod:`services.cowork_agent.adapters.usage_common`.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.openclaw import agent_db
from services.cowork_agent.engine.messages import content_blocks


# ─────────────────────────────────────────────────────────────────────────────
# Filename filter + tool extraction + discovery
# Mirrors openclaw `dist/artifacts-B81-HgBC.js` and `dist/chat-envelope-D39qAHGK.js`.
# ─────────────────────────────────────────────────────────────────────────────

_TOOL_CALL_BLOCK_TYPES = frozenset({"tool_use", "toolcall", "tool_call"})
_TOOL_RESULT_BLOCK_TYPES = frozenset({"tool_result", "tool_result_error"})

_ARCHIVE_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:\.\d{3})?Z$")
_SESSIONS_JSON_BAK_RE = re.compile(r"^sessions\.json\.bak\.\d+$")


def _is_usage_counted_session_filename(name: str) -> bool:
    if name == "sessions.json" or _SESSIONS_JSON_BAK_RE.match(name):
        return False
    for tail in (".jsonl.reset.", ".jsonl.deleted.", ".jsonl.bak."):
        idx = name.find(tail)
        if idx > 0:
            ts_part = name[idx + len(tail):]
            if _ARCHIVE_TS_RE.match(ts_part):
                return tail != ".jsonl.bak."
            return False
    return name.endswith(".jsonl")


def _extract_tool_counts(message: dict) -> tuple[list[str], dict]:
    """Mirror gateway extractToolCallNames + countToolResults."""
    names: list[str] = []
    seen: set[str] = set()
    results = {"total": 0, "errors": 0}

    direct = message.get("toolName") or message.get("tool_name")
    if isinstance(direct, str) and direct and direct not in seen:
        seen.add(direct)
        names.append(direct)

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if not isinstance(btype, str):
                continue
            btype_lc = btype.lower()
            if btype_lc in _TOOL_CALL_BLOCK_TYPES:
                name = block.get("name")
                if isinstance(name, str) and name and name not in seen:
                    seen.add(name)
                    names.append(name)
            elif btype_lc in _TOOL_RESULT_BLOCK_TYPES:
                results["total"] += 1
                if block.get("is_error") is True:
                    results["errors"] += 1

    return names, results


def _agents_dir() -> str:
    return os.getenv("OPENCLAW_AGENTS_DIR", os.path.expanduser("~/.openclaw/agents"))


def _list_agent_ids() -> list[str]:
    base = _agents_dir()
    try:
        return sorted(
            name for name in os.listdir(base)
            if os.path.isdir(os.path.join(base, name))
        )
    except OSError:
        return []


def _discover_one_agent(agent_id: str) -> list[str]:
    if agent_db.has_database(agent_id):
        return sorted(
            agent_db.generation_locator(agent_id, session_id)
            for session_id in agent_db.list_generation_ids(agent_id)
        )
    sessions_dir = os.path.join(_agents_dir(), agent_id, "sessions")
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return []
    return sorted(
        os.path.join(sessions_dir, n)
        for n in names
        if _is_usage_counted_session_filename(n)
    )


def _resolve_tz(tz: str) -> tzinfo:
    """tz='local' → host local TZ (matches gateway mode=gateway).
    tz='utc' → UTC. Anything else falls back to local."""
    if tz == "utc":
        return timezone.utc
    return datetime.now().astimezone().tzinfo or timezone.utc


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_time(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _read_records(path: str) -> list[dict]:
    """A session's transcript records: a database generation (see
    ``agent_db.generation_locator``) or a JSONL file. Undecodable lines are
    skipped; a missing file raises."""
    located = agent_db.parse_generation_locator(path)
    if located:
        return agent_db.read_generation(*located)
    records: list[dict] = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Contract — discovery + parser (OpenClaw-specific)
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """All session transcripts the gateway counts toward usage.

    ``agent_id=None`` iterates every agent subdir under OPENCLAW_AGENTS_DIR
    (matches gateway sessions.usage "all agents" view). Pass an explicit id
    to scope to a single openclaw agent (main, researcher, …).
    """
    if agent_id:
        return _discover_one_agent(agent_id)
    files: list[str] = []
    for aid in _list_agent_ids():
        files.extend(_discover_one_agent(aid))
    return sorted(files)


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse a single openclaw session transcript (JSONL file or database generation).

    Returns (session_meta, entries) where each entry carries
    ``role="user"|"assistant"``. User entries are minimal; assistant entries
    carry usage/cost/model/stopReason/toolNames/toolResultCounts/durationMs.
    Emitting both roles keeps parity with gateway messageCounts.
    """
    session_meta: dict = {}
    entries: list = []

    last_user_ts: Optional[float] = None

    for record in _read_records(path):

        rtype = record.get("type")
        if rtype == "session":
            session_meta = {
                "sessionId": record.get("id"),
                "sessionFile": os.path.basename(path),
                "startTimestamp": record.get("timestamp"),
            }
            continue
        if rtype != "message":
            continue

        msg = record.get("message", {})
        role = msg.get("role")
        ts_str = record.get("timestamp") or msg.get("timestamp")

        ts_epoch_ms: Optional[int] = None
        if isinstance(ts_str, str):
            try:
                ts_epoch_ms = int(
                    datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000
                )
            except Exception:
                pass
        elif isinstance(ts_str, (int, float)):
            ts_epoch_ms = int(ts_str) if ts_str > 1e12 else int(ts_str * 1000)

        if ts_epoch_ms:
            if start_ms and ts_epoch_ms < start_ms:
                continue
            if end_ms and ts_epoch_ms > end_ms:
                continue

        if role == "user":
            last_user_ts = ts_epoch_ms
            entries.append({"role": "user", "timestamp": ts_epoch_ms})
            continue

        if role != "assistant":
            continue

        usage = msg.get("usage")
        if not usage:
            continue

        tool_names, tool_result_counts = _extract_tool_counts(msg)
        duration_ms = msg.get("durationMs")
        if not (isinstance(duration_ms, (int, float)) and duration_ms > 0):
            duration_ms = (ts_epoch_ms - last_user_ts) if (last_user_ts and ts_epoch_ms) else None

        entries.append({
            "role": "assistant",
            "usage": usage,
            "provider": msg.get("provider"),
            "model": msg.get("model"),
            "timestamp": ts_epoch_ms,
            "stopReason": msg.get("stopReason"),
            "toolNames": tool_names,
            "toolResultCounts": tool_result_counts,
            "durationMs": duration_ms,
        })

    return session_meta, entries


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Returns the UsageStats shape consumed by the frontend Settings → Usage tab.
# Kept OpenClaw-specific (separate from the shared entries views) because of the
# gateway-parity rules: empty-trajectory sessions still count via mtime, and the
# response-time clamp / title heuristic are openclaw-shaped.
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    """The body of /api/usage minus HTTP plumbing.

    Aggregates across every agent under OPENCLAW_AGENTS_DIR (the all-agents
    view that matches the gateway's sessions.usage). Day buckets honor ``tz``
    (default ``"local"`` for gateway parity); ``"utc"`` is also accepted.
    """
    days = max(1, min(days, 365))
    z = _resolve_tz(tz)
    today = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=days - 1)

    total_tokens = _empty_tokens()
    total_cost = 0.0
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()

    by_day: dict[str, dict] = {}
    by_model_key: dict[tuple[str, str], dict] = {}
    session_stats: dict[str, dict] = {}
    response_times: list[float] = []

    cutoff_ms = int(cutoff.timestamp() * 1000)

    for sf in get_session_files():
        located = agent_db.parse_generation_locator(sf)
        session_id: Optional[str] = located[1] if located else None
        if located is None:
            # session_id = base part of filename (handles .reset/.deleted archives)
            name = os.path.basename(sf)
            for marker in (".jsonl.reset.", ".jsonl.deleted."):
                idx = name.find(marker)
                if idx > 0:
                    session_id = name[:idx]
                    break
            if session_id is None:
                if name.endswith(".jsonl") and ".checkpoint." not in name:
                    session_id = name[:-len(".jsonl")]
        if session_id is None:
            continue

        # Gateway parity: empty trajectory shells (e.g. .trajectory.jsonl.deleted.<ISO>
        # files with only session.started/session.ended events) still count as a
        # discovered session if mtime is within the window. They contribute 0 to
        # tokens/cost/messages but +1 to total_sessions. Without this we report
        # 27 sessions where the gateway reports 29 on the same data.
        try:
            updated_ms = agent_db.generation_updated_ms(*located) if located else os.path.getmtime(sf) * 1000
            if updated_ms is not None and updated_ms >= cutoff_ms:
                session_ids.add(session_id)
        except OSError:
            pass

        try:
            records = _read_records(sf)
        except Exception:
            continue

        session_title: Optional[str] = None
        first_user_ts: Optional[str] = None
        session_entry = {
            "session_id": session_id,
            "title": "Untitled Session",
            "total_cost": 0.0,
            "total_tokens": 0,
            "message_count": 0,
            "time_created": None,
        }
        last_user_time: Optional[datetime] = None

        for record in records:
            if record.get("type") != "message":
                continue
            msg = record.get("message", {})
            role = msg.get("role")
            rt = _record_time(record.get("timestamp"))
            if rt is None or rt < cutoff:
                continue

            if role == "user":
                user_messages += 1
                session_ids.add(session_id)
                last_user_time = rt
                if session_title is None:
                    for block in content_blocks(msg):
                        if block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text and not text.startswith("Read HEARTBEAT.md"):
                                session_title = text[:80] + ("..." if len(text) > 80 else "")
                                break
                if first_user_ts is None:
                    first_user_ts = record.get("timestamp")
                continue

            if role != "assistant":
                continue

            usage_data = msg.get("usage") or {}
            if not usage_data:
                continue

            inp = int(usage_data.get("input", 0) or 0)
            out = int(usage_data.get("output", 0) or 0)
            cache_r = int(usage_data.get("cacheRead", 0) or 0)
            cache_w = int(usage_data.get("cacheWrite", 0) or 0)
            cost_raw = usage_data.get("cost", 0)
            cost_val = float(cost_raw.get("total") or 0) if isinstance(cost_raw, dict) else float(cost_raw or 0)

            total_tokens["input"] += inp
            total_tokens["output"] += out
            total_tokens["cache_read"] += cache_r
            total_tokens["cache_write"] += cache_w
            total_cost += cost_val
            assistant_messages += 1
            session_ids.add(session_id)

            if last_user_time is not None:
                delta = (rt - last_user_time).total_seconds()
                if 0 <= delta <= 600:
                    response_times.append(delta)
                last_user_time = None

            day_key = rt.date().isoformat()
            day = by_day.setdefault(day_key, {"date": day_key, "cost": 0.0, "tokens": 0, "messages": 0})
            day["cost"] += cost_val
            day["tokens"] += inp + out
            day["messages"] += 1

            model_id = msg.get("model") or "unknown"
            provider_id = msg.get("provider") or ""
            mk = (model_id, provider_id)
            m = by_model_key.setdefault(mk, {
                "model_id": model_id, "provider_id": provider_id,
                "total_cost": 0.0, "total_tokens": _empty_tokens(), "message_count": 0,
            })
            m["total_cost"] += cost_val
            m["total_tokens"]["input"] += inp
            m["total_tokens"]["output"] += out
            m["total_tokens"]["cache_read"] += cache_r
            m["total_tokens"]["cache_write"] += cache_w
            m["message_count"] += 1

            session_entry["total_cost"] += cost_val
            session_entry["total_tokens"] += inp + out
            session_entry["message_count"] += 1

        if session_entry["message_count"] > 0:
            if session_title:
                session_entry["title"] = session_title
            session_entry["time_created"] = first_user_ts or _iso_now()
            session_stats[session_id] = session_entry

    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).date().isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["total_cost"], reverse=True)
    by_session = sorted(session_stats.values(), key=lambda s: s["total_tokens"], reverse=True)[:10]

    if response_times:
        sorted_rt = sorted(response_times)
        n = len(sorted_rt)
        rt_stats = {
            "avg": sum(sorted_rt) / n,
            "median": sorted_rt[n // 2],
            "p95": sorted_rt[min(n - 1, int(n * 0.95))],
            "min": sorted_rt[0],
            "max": sorted_rt[-1],
            "count": n,
        }
    else:
        rt_stats = {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}

    total_sessions = len(session_ids)
    flat_tokens = total_tokens["input"] + total_tokens["output"] + total_tokens["reasoning"]
    avg_tokens_per_session = flat_tokens / total_sessions if total_sessions else 0

    return {
        "total_cost": round(total_cost, 6),
        "total_tokens": total_tokens,
        "total_sessions": total_sessions,
        "total_messages": assistant_messages + user_messages,
        "avg_tokens_per_session": round(avg_tokens_per_session, 2),
        "avg_response_time": round(rt_stats["avg"], 3),
        "by_model": by_model,
        "by_session": by_session,
        "daily": daily,
        "response_time": {
            "avg": round(rt_stats["avg"], 3),
            "median": round(rt_stats["median"], 3),
            "p95": round(rt_stats["p95"], 3),
            "min": round(rt_stats["min"], 3),
            "max": round(rt_stats["max"], 3),
            "count": rt_stats["count"],
        },
    }


def dashboard(*, window: dict) -> dict:
    """UsageStats shape — what /api/usage returns. Routes here for openclaw."""
    days = window.get("days", 30) if "days" in window else None
    tz = window.get("tz", "local")
    if days is None:
        # Explicit start/end branch: derive a synthetic days value for the
        # daily[] zero-fill in aggregate_for_dashboard.
        _start_ms, _end_ms, derived_days = _uc.window_to_ms(window)
        days = derived_days or 30
    return aggregate_for_dashboard(days=days, tz=tz)


# ─────────────────────────────────────────────────────────────────────────────
# Shared entries-based views — bound to this module's discovery + parser.
# Implementations live in usage_common; the public names/signatures below are
# unchanged so the loader and usage_sync keep working.
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE = _uc.Source(discover=lambda: get_session_files(), parse_file=parse_file)


def build_summary(session_meta: dict, entries: list) -> dict:
    return _uc.build_summary(session_meta, entries)


def analytics(*, window: dict) -> dict:
    return _uc.analytics(_SOURCE, window=window)


def summary(*, window: dict) -> dict:
    return _uc.summary(_SOURCE, window=window)


def summary_card(*, window: dict) -> dict:
    return _uc.summary_card(_SOURCE, window=window)


def list_sessions() -> dict:
    return _uc.list_sessions(_SOURCE)


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    return _uc.get_session(_SOURCE, session_id, window=window)


def aggregate_for_sync(*, since_date: Optional[str] = None) -> dict:
    return _uc.aggregate_for_sync(_SOURCE, since_date=since_date)


# Canonical sync surface name (used by services/usage_sync.py).
sync_payload = aggregate_for_sync
