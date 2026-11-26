"""
Claude Code usage — discovery + parser + dashboard aggregator.

Loaded by ``services.cowork_agent.engine.usage_loader.load_usage_module()`` when
``AGENT_NAME=claude_code``. Same public contract as openclaw.

Source: ``~/.claude/projects/<encoded>/*.jsonl`` — every Anthropic transcript
Claude Code writes. Discovery walks the projects tree directly (NOT filtered
through xo-project ``sessionslist.json`` indices), so the UI shows the full
Claude Code usage the way the ``claude /usage`` CLI does.

Token shape uses Anthropic raw fields: ``input_tokens``, ``output_tokens``,
``cache_read_input_tokens``, ``cache_creation_input_tokens``. Cost is always
0.0 — Anthropic JSONL has no billing field. Records are deduped by
``message.id`` (every streaming chunk shares one id); a token-tuple dedup
historically over-counted by ~75% when tool_result records were interleaved.

A ``type:"user"`` record only counts as a user message if it carries actual
user text — pure tool_result records are protocol noise, not turns.

Only discovery, ``parse_file`` and the ``/api/usage`` dashboard are
Claude-Code-specific. The entries-based views (summary/analytics/sessions/sync)
are shared with openclaw via
:mod:`services.cowork_agent.adapters.usage_common` — claude_code's parser emits
the same normalized entry shape on purpose.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Optional

from services.cowork_agent.adapters import usage_common as _uc


# ─────────────────────────────────────────────────────────────────────────────
# Configuration + helpers (used by discovery / parse_file / dashboard)
# ─────────────────────────────────────────────────────────────────────────────


def _projects_root() -> Path:
    return Path(os.getenv("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")))


def _resolve_tz(tz: str) -> tzinfo:
    if tz == "utc":
        return timezone.utc
    return datetime.now().astimezone().tzinfo or timezone.utc


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _record_time(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: str) -> list[dict]:
    """Tolerant JSONL reader — skips malformed lines instead of raising,
    so one bad record doesn't poison a whole session.
    """
    return list(_iter_jsonl(path))


def _iter_jsonl(path: str, *, strict_io: bool = False) -> Iterator[dict]:
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        if strict_io:
            raise


def _derive_title(records: list[dict]) -> str:
    """First non-trivial user text in the transcript becomes the session title."""
    for r in records:
        if r.get("type") != "user":
            continue
        msg = r.get("message", {}) or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text[:80] + ("..." if len(text) > 80 else "")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        return text[:80] + ("..." if len(text) > 80 else "")
    return "Untitled Session"


# ─────────────────────────────────────────────────────────────────────────────
# File discovery — every JSONL under ~/.claude/projects/<encoded>/
# ─────────────────────────────────────────────────────────────────────────────


def _discover() -> list[str]:
    """Return every ``.jsonl`` file under the projects root, one level deep.

    Matches the ``claude /usage`` enumeration: each project directory holds
    one JSONL per session. No filtering on xo-project membership — we want
    the full picture the UI's Settings → Usage tab needs.
    """
    root = _projects_root()
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for proj in sorted(root.iterdir()):
        if not proj.is_dir() or proj.name.startswith("."):
            continue
        for f in sorted(proj.iterdir()):
            if f.is_file() and f.name.endswith(".jsonl"):
                out.append(str(f))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Contract — discovery + parser (Claude-Code-specific)
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """Every Claude Code native JSONL on disk.

    ``agent_id`` is accepted for contract symmetry with openclaw but ignored —
    Claude Code is single-tenant per deployment.
    """
    return _discover()


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse a single Anthropic JSONL transcript.

    Returns ``(meta, entries)`` with entries shaped like openclaw's parser
    output (role=user|assistant, normalized usage keys). Dedups by
    ``message.id`` so streaming records of the same API call count once.
    """
    meta: dict = {"sessionId": None, "sessionFile": os.path.basename(path)}
    # Default session id = filename without `.jsonl` (= Anthropic native id).
    base = os.path.basename(path)
    if base.endswith(".jsonl"):
        meta["sessionId"] = base[: -len(".jsonl")]

    return meta, list(_iter_entries(_iter_jsonl(path), meta, start_ms=start_ms, end_ms=end_ms))


def _iter_entries(
    records: Iterator[dict], meta: dict, *, start_ms: int | None, end_ms: int | None,
) -> Iterator[dict]:
    seen_message_ids: set[str] = set()
    last_user_ts: int | None = None

    for record in records:
        rtype = record.get("type")
        msg = record.get("message", {}) or {}
        ts_str = record.get("timestamp")
        rt = _record_time(ts_str)
        ts_epoch_ms = int(rt.timestamp() * 1000) if rt else None

        if ts_epoch_ms is not None:
            if start_ms and ts_epoch_ms < start_ms:
                continue
            if end_ms and ts_epoch_ms > end_ms:
                continue

        # Prefer record/message-supplied sessionId when present.
        sid = record.get("sessionId") or msg.get("sessionId")
        if isinstance(sid, str) and sid:
            meta["sessionId"] = sid

        if rtype == "user":
            content = msg.get("content", "")
            has_text = (
                (isinstance(content, str) and content.strip()) or
                (isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "text"
                    for b in content
                ))
            )
            if has_text:
                last_user_ts = ts_epoch_ms
                yield {"role": "user", "timestamp": ts_epoch_ms}
            continue

        if rtype != "assistant":
            continue

        usage_data = msg.get("usage") or {}
        if not usage_data:
            continue

        msg_id = msg.get("id")
        if isinstance(msg_id, str) and msg_id:
            if msg_id in seen_message_ids:
                continue
            seen_message_ids.add(msg_id)

        inp = int(usage_data.get("input_tokens", 0) or 0)
        out = int(usage_data.get("output_tokens", 0) or 0)
        cache_r = int(usage_data.get("cache_read_input_tokens", 0) or 0)
        cache_w = int(usage_data.get("cache_creation_input_tokens", 0) or 0)

        duration_ms = (ts_epoch_ms - last_user_ts) if (last_user_ts and ts_epoch_ms) else None

        yield {
            "role": "assistant",
            # Use openclaw-style keys so the shared build_summary can be reused.
            "usage": {
                "input": inp,
                "output": out,
                "cacheRead": cache_r,
                "cacheWrite": cache_w,
                "totalTokens": inp + out + cache_r + cache_w,
                "cost": {"total": 0.0, "input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
            },
            "provider": "anthropic",
            "model": msg.get("model") or "claude",
            "timestamp": ts_epoch_ms,
            "stopReason": msg.get("stop_reason"),
            "toolNames": [],
            "toolResultCounts": {"total": 0, "errors": 0},
            "durationMs": duration_ms,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Claude-Code-specific (separate from the shared entries views): re-parses files
# with empty-session mtime parity and a response-time clamp. Always returns
# total_cost=0.0 (no billing in Anthropic JSONL).
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    days = max(1, min(days, 365))
    z = _resolve_tz(tz)
    today = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=days - 1)
    cutoff_ms = int(cutoff.timestamp() * 1000)

    total_tokens = _empty_tokens()
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()
    by_day: dict[str, dict] = {}
    by_model_key: dict[tuple[str, str], dict] = {}
    session_stats: dict[str, dict] = {}
    response_times: list[float] = []

    for path in _discover():
        # session_id = file basename without ".jsonl" (= Anthropic native id).
        base = os.path.basename(path)
        if not base.endswith(".jsonl"):
            continue
        session_id = base[: -len(".jsonl")]

        # Gateway-style empty-session parity: any file whose mtime falls in
        # the window contributes to total_sessions, even if it has zero
        # messages. (Aligns with the openclaw module's behavior.)
        try:
            if os.path.getmtime(path) * 1000 >= cutoff_ms:
                session_ids.add(session_id)
        except OSError:
            pass

        records = _read_jsonl(path)
        if not records:
            continue

        session_entry = {
            "session_id": session_id,
            "title": _derive_title(records),
            "total_cost": 0.0,
            "total_tokens": 0,
            "message_count": 0,
            "time_created": None,
        }
        first_user_ts: Optional[str] = None
        last_user_time: Optional[datetime] = None
        seen_message_ids: set[str] = set()

        for record in records:
            rtype = record.get("type")
            msg = record.get("message", {}) or {}
            if not msg:
                continue
            ts = record.get("timestamp")
            rt = _record_time(ts)
            if rt is None or rt < cutoff:
                continue

            if rtype == "user":
                content = msg.get("content", "")
                has_text = (
                    (isinstance(content, str) and content.strip()) or
                    (isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "text"
                        for b in content
                    ))
                )
                if has_text:
                    user_messages += 1
                    session_ids.add(session_id)
                last_user_time = rt
                if first_user_ts is None:
                    first_user_ts = ts
                continue

            if rtype != "assistant":
                continue

            usage_data = msg.get("usage") or {}
            if not usage_data:
                continue

            msg_id = msg.get("id")
            if isinstance(msg_id, str) and msg_id:
                if msg_id in seen_message_ids:
                    continue
                seen_message_ids.add(msg_id)

            inp = int(usage_data.get("input_tokens", 0) or 0)
            out = int(usage_data.get("output_tokens", 0) or 0)
            cache_r = int(usage_data.get("cache_read_input_tokens", 0) or 0)
            cache_w = int(usage_data.get("cache_creation_input_tokens", 0) or 0)

            total_tokens["input"] += inp
            total_tokens["output"] += out
            total_tokens["cache_read"] += cache_r
            total_tokens["cache_write"] += cache_w
            assistant_messages += 1
            session_ids.add(session_id)

            if last_user_time is not None:
                delta = (rt - last_user_time).total_seconds()
                if 0 <= delta <= 600:
                    response_times.append(delta)
                last_user_time = None

            day_key = rt.date().isoformat()
            day = by_day.setdefault(day_key, {"date": day_key, "cost": 0.0, "tokens": 0, "messages": 0})
            day["tokens"] += inp + out
            day["messages"] += 1

            model_id = msg.get("model") or "claude"
            mk = (model_id, "anthropic")
            m = by_model_key.setdefault(mk, {
                "model_id": model_id, "provider_id": "anthropic",
                "total_cost": 0.0, "total_tokens": _empty_tokens(), "message_count": 0,
            })
            m["total_tokens"]["input"] += inp
            m["total_tokens"]["output"] += out
            m["total_tokens"]["cache_read"] += cache_r
            m["total_tokens"]["cache_write"] += cache_w
            m["message_count"] += 1

            session_entry["total_tokens"] += inp + out
            session_entry["message_count"] += 1

        if session_entry["message_count"] > 0:
            session_entry["time_created"] = first_user_ts or _iso_now()
            session_stats[session_id] = session_entry

    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).date().isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["message_count"], reverse=True)
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
        "total_cost": 0.0,
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
    """UsageStats — claude_code's /api/usage payload. Wraps aggregate_for_dashboard."""
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _start_ms, _end_ms, derived_days = _uc.window_to_ms(window)
    return aggregate_for_dashboard(days=derived_days or 30, tz="local")


# ─────────────────────────────────────────────────────────────────────────────
# Shared entries-based views — bound to this module's discovery + parser.
# Implementations live in usage_common; the public names/signatures below are
# unchanged so the loader and usage_sync keep working.
# ─────────────────────────────────────────────────────────────────────────────

def _card_entries(path: str, *, start_ms: int | None, end_ms: int | None) -> Iterator[dict]:
    return _iter_entries(_iter_jsonl(path, strict_io=True), {}, start_ms=start_ms, end_ms=end_ms)


_SOURCE = _uc.Source(discover=lambda: get_session_files(), parse_file=parse_file, card_entries=_card_entries)


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
