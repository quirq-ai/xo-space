"""
Shared usage aggregation over normalized **usage entries**.

Two agent backends — ``claude_code`` and ``openclaw`` — record per-message
transcripts and expose identical usage views. The only things that differ
between them are *where* the transcripts live and *how* one transcript record
maps to a normalized entry. Everything downstream — windowing, per-session
summaries, the analytics/summary/sessions payloads, and the daily sync rollup —
operates purely on the resulting list of **entries** and is therefore shared
here.

An **entry** is one normalized usage record:

    user entry:        {"role": "user", "timestamp": <epoch_ms|None>}
    assistant entry:   {"role": "assistant", "timestamp": <epoch_ms|None>,
                        "usage": {"input","output","cacheRead","cacheWrite",
                                  "totalTokens","cost": {...}},
                        "provider": str, "model": str, "stopReason": str|None,
                        "toolNames": [str], "toolResultCounts": {"total","errors"},
                        "durationMs": int|None}

An adapter wires its discovery + parser into a :class:`Source` and binds the
view functions below to its module surface (see claude_code/openclaw usage.py).
Output is byte-for-byte what the per-adapter copies produced before extraction.

Note: hermes does NOT use this module — it reads rolled-up session rows from
SQLite and synthesizes its own views (no per-message entries exist).
"""
from __future__ import annotations

import os
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from itertools import chain
from threading import Lock
from typing import Callable, Optional


_ERROR_STOP_REASONS = frozenset({"error", "aborted", "timeout"})


# ─────────────────────────────────────────────────────────────────────────────
# Source — the per-adapter seam: discovery + record parsing.
# ─────────────────────────────────────────────────────────────────────────────


class Source:
    """The two adapter-specific operations the shared views depend on.

    discover()  -> list[str]                       every session file/path
    parse_file(path, *, start_ms, end_ms)
                -> tuple[meta: dict, entries: list] one path → normalized entries
    """

    def __init__(
        self,
        *,
        discover: Callable[[], list],
        parse_file: Callable[..., tuple],
        card_entries: Callable[..., Iterator[dict]] | None = None,
    ) -> None:
        self.discover = discover
        self.parse_file = parse_file
        self.card_cache = _CardCache(card_entries) if card_entries is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def resolve_tz(tz: str) -> tzinfo:
    """tz='utc' → UTC; anything else → host local TZ (gateway parity)."""
    if tz == "utc":
        return timezone.utc
    return datetime.now().astimezone().tzinfo or timezone.utc


def empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def date_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _latency_stats(vals: list) -> dict:
    if not vals:
        return {"count": 0, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0}
    s = sorted(vals)
    p95_idx = max(0, int(len(s) * 0.95) - 1)
    return {
        "count": len(s),
        "avgMs": round(sum(s) / len(s)),
        "p95Ms": s[p95_idx],
        "minMs": s[0],
        "maxMs": s[-1],
    }


def window_to_ms(window: dict | None) -> tuple[Optional[int], Optional[int], int]:
    """Resolve a unified window dict to (start_ms, end_ms, range_days).

      {"days": N, "tz": "local"|"utc"}            → gateway parseDateRange
      {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} → explicit (UTC)
      None / empty                                 → no filter
    """
    if not window:
        return None, None, 0
    if "start" in window or "end" in window:
        s = window.get("start"); e = window.get("end")
        start_ms = (
            int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
            if s else None
        )
        end_ms = (
            int(datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000
            if e else None
        )
        if start_ms and end_ms:
            range_days = max(1, (end_ms - start_ms) // 86_400_000)
        else:
            range_days = 0
        return start_ms, end_ms, int(range_days)
    if "days" in window:
        days = int(window["days"])
        tz = window.get("tz", "local")
        z = resolve_tz(tz)
        today_start = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int((today_start - timedelta(days=days - 1)).timestamp() * 1000)
        end_ms = int((today_start + timedelta(days=1)).timestamp() * 1000) - 1
        return start_ms, end_ms, days
    return None, None, 0


def collect_entries(source: Source, start_ms: Optional[int], end_ms: Optional[int]) -> list:
    """Walk every session file via the source's parser and flatten entries."""
    all_entries: list = []
    for sf in source.discover():
        try:
            _, entries = source.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        except Exception:
            continue
        all_entries.extend(entries)
    return all_entries


# ─────────────────────────────────────────────────────────────────────────────
# build_summary — SessionCostSummary-compatible aggregation over entries.
# ─────────────────────────────────────────────────────────────────────────────


def build_summary(session_meta: dict, entries: list) -> dict:
    """Aggregate normalized entries into a SessionCostSummary-compatible dict."""
    total_input = total_output = total_cache_read = total_cache_write = total_tokens = 0
    total_cost = input_cost = output_cost = cache_read_cost = cache_write_cost = 0.0
    missing_cost = 0

    daily_usage: dict = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0})
    daily_messages: dict = defaultdict(
        lambda: {"date": "", "total": 0, "user": 0, "assistant": 0, "toolCalls": 0,
                 "toolResults": 0, "errors": 0}
    )
    daily_latency_buckets: dict = defaultdict(list)
    daily_model_usage: dict = defaultdict(
        lambda: {"date": "", "provider": "", "model": "", "tokens": 0, "cost": 0.0, "count": 0}
    )

    tool_counter: dict = defaultdict(int)
    total_tool_calls = 0
    model_usage_map: dict = {}
    latencies: list = []
    first_activity: Optional[int] = None
    last_activity: Optional[int] = None
    activity_dates: set = set()

    total_user_msgs = 0
    total_assistant_msgs = 0
    total_tool_results = 0
    total_errors = 0

    for entry in entries:
        role = entry.get("role", "assistant")
        ts = entry.get("timestamp")

        if role == "user":
            total_user_msgs += 1
            if ts:
                date_str = date_from_ms(ts)
                activity_dates.add(date_str)
                if first_activity is None or ts < first_activity:
                    first_activity = ts
                if last_activity is None or ts > last_activity:
                    last_activity = ts
                dm = daily_messages[date_str]
                dm["date"] = date_str
                dm["user"] += 1
                dm["total"] += 1
            continue

        total_assistant_msgs += 1
        usage = entry["usage"]
        cost_obj = usage.get("cost", {})

        inp = usage.get("input", 0)
        out = usage.get("output", 0)
        cr = usage.get("cacheRead", 0)
        cw = usage.get("cacheWrite", 0)
        tok = usage.get("totalTokens", 0) or (inp + out + cr + cw)

        total_input += inp
        total_output += out
        total_cache_read += cr
        total_cache_write += cw
        total_tokens += tok

        if cost_obj:
            total_cost += cost_obj.get("total", 0) or 0
            input_cost += cost_obj.get("input", 0) or 0
            output_cost += cost_obj.get("output", 0) or 0
            cache_read_cost += cost_obj.get("cacheRead", 0) or 0
            cache_write_cost += cost_obj.get("cacheWrite", 0) or 0
        else:
            missing_cost += 1

        for tn in entry.get("toolNames", []):
            tool_counter[tn] += 1
            total_tool_calls += 1

        tr = entry.get("toolResultCounts") or {}
        total_tool_results += int(tr.get("total", 0) or 0)
        msg_errors = int(tr.get("errors", 0) or 0)
        if entry.get("stopReason") in _ERROR_STOP_REASONS:
            msg_errors += 1
        total_errors += msg_errors

        if ts:
            date_str = date_from_ms(ts)
            activity_dates.add(date_str)
            if first_activity is None or ts < first_activity:
                first_activity = ts
            if last_activity is None or ts > last_activity:
                last_activity = ts

            d = daily_usage[date_str]
            d["date"] = date_str
            d["tokens"] += tok
            d["cost"] += cost_obj.get("total", 0) or 0

            dm = daily_messages[date_str]
            dm["date"] = date_str
            dm["assistant"] += 1
            dm["total"] += 1
            dm["toolCalls"] += len(entry.get("toolNames", []))
            dm["toolResults"] += int(tr.get("total", 0) or 0)
            dm["errors"] += msg_errors

            dur = entry.get("durationMs")
            if dur and dur > 0:
                daily_latency_buckets[date_str].append(dur)
                latencies.append(dur)

            model_key = f"{date_str}|{entry.get('provider', '')}|{entry.get('model', '')}"
            dmu = daily_model_usage[model_key]
            dmu["date"] = date_str
            dmu["provider"] = entry.get("provider", "")
            dmu["model"] = entry.get("model", "")
            dmu["tokens"] += tok
            dmu["cost"] += cost_obj.get("total", 0) or 0
            dmu["count"] += 1

        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in model_usage_map:
            model_usage_map[mkey] = {
                "provider": entry.get("provider"),
                "model": entry.get("model"),
                "count": 0,
                "totals": {
                    "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                    "totalTokens": 0, "totalCost": 0, "inputCost": 0, "outputCost": 0,
                    "cacheReadCost": 0, "cacheWriteCost": 0, "missingCostEntries": 0,
                },
            }
        mu = model_usage_map[mkey]
        mu["count"] += 1
        mt = mu["totals"]
        mt["input"] += inp
        mt["output"] += out
        mt["cacheRead"] += cr
        mt["cacheWrite"] += cw
        mt["totalTokens"] += tok
        mt["totalCost"] += cost_obj.get("total", 0) or 0
        mt["inputCost"] += cost_obj.get("input", 0) or 0
        mt["outputCost"] += cost_obj.get("output", 0) or 0
        mt["cacheReadCost"] += cost_obj.get("cacheRead", 0) or 0
        mt["cacheWriteCost"] += cost_obj.get("cacheWrite", 0) or 0

    daily_latency_list = []
    for date_str in sorted(daily_latency_buckets.keys()):
        stats = _latency_stats(daily_latency_buckets[date_str])
        stats["date"] = date_str
        daily_latency_list.append(stats)

    return {
        "sessionId": session_meta.get("sessionId"),
        "sessionFile": session_meta.get("sessionFile"),
        "firstActivity": first_activity,
        "lastActivity": last_activity,
        "durationMs": (last_activity - first_activity) if first_activity and last_activity else None,
        "activityDates": sorted(activity_dates),
        "input": total_input,
        "output": total_output,
        "cacheRead": total_cache_read,
        "cacheWrite": total_cache_write,
        "totalTokens": total_tokens,
        "totalCost": round(total_cost, 6),
        "inputCost": round(input_cost, 6),
        "outputCost": round(output_cost, 6),
        "cacheReadCost": round(cache_read_cost, 6),
        "cacheWriteCost": round(cache_write_cost, 6),
        "missingCostEntries": missing_cost,
        "dailyBreakdown": sorted(daily_usage.values(), key=lambda d: d["date"]),
        "dailyMessageCounts": sorted(daily_messages.values(), key=lambda d: d["date"]),
        "dailyLatency": daily_latency_list,
        "dailyModelUsage": sorted(daily_model_usage.values(), key=lambda d: d["date"]),
        "messageCounts": {
            "total": total_user_msgs + total_assistant_msgs,
            "user": total_user_msgs,
            "assistant": total_assistant_msgs,
            "toolCalls": total_tool_calls,
            "toolResults": total_tool_results,
            "errors": total_errors,
        },
        "toolUsage": {
            "totalCalls": total_tool_calls,
            "uniqueTools": len(tool_counter),
            "tools": sorted(
                [{"name": k, "count": v} for k, v in tool_counter.items()],
                key=lambda t: -t["count"],
            ),
        },
        "modelUsage": list(model_usage_map.values()),
        "latency": _latency_stats(latencies),
    }


# ─────────────────────────────────────────────────────────────────────────────
# View functions — bound to each adapter's module surface.
# ─────────────────────────────────────────────────────────────────────────────


def analytics(source: Source, *, window: dict) -> dict:
    """Time-series dashboard payload: stat cards + per-day cost/tokens +
    per-day messages + per-day performance + per-tool counts + per-model totals."""
    start_ms, end_ms, range_days = window_to_ms(window)
    range_days = range_days or 5  # legacy default for unspecified windows

    all_entries = collect_entries(source, start_ms, end_ms)

    empty_response = {
        "stats": {"totalCost": 0, "totalTokens": 0, "totalMessages": 0, "avgLatencyMs": 0},
        "costAndTokens": [],
        "messages": [],
        "performance": [],
        "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "modelUsage": [],
    }
    if not all_entries:
        return empty_response

    total_cost = 0.0
    total_tokens = 0
    total_messages = 0
    latencies: list[int] = []

    daily_cost: dict[str, dict] = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0})
    daily_msgs: dict[str, dict] = defaultdict(
        lambda: {"date": "", "total": 0, "user": 0, "assistant": 0, "toolCalls": 0}
    )
    daily_perf: dict[str, list] = defaultdict(list)

    tool_counter: dict[str, int] = defaultdict(int)
    total_tool_calls = 0
    model_map: dict[str, dict] = {}

    for entry in all_entries:
        role = entry.get("role", "assistant")
        ts = entry.get("timestamp")

        if role == "user":
            total_messages += 1
            if ts:
                d = date_from_ms(ts)
                dm = daily_msgs[d]
                dm["date"] = d
                dm["user"] += 1
                dm["total"] += 1
            continue

        usage = entry["usage"]
        cost_val = usage.get("cost", {}).get("total", 0) or 0
        tok = usage.get("totalTokens", 0) or 0

        total_cost += cost_val
        total_tokens += tok
        total_messages += 1

        dur = entry.get("durationMs")
        if dur and dur > 0:
            latencies.append(dur)

        if ts:
            d = date_from_ms(ts)
            dc = daily_cost[d]
            dc["date"] = d
            dc["tokens"] += tok
            dc["cost"] += cost_val

            dm = daily_msgs[d]
            dm["date"] = d
            dm["assistant"] += 1
            dm["total"] += 1
            dm["toolCalls"] += len(entry.get("toolNames", []))

            if dur and dur > 0:
                daily_perf[d].append(dur)

        for tn in entry.get("toolNames", []):
            tool_counter[tn] += 1
            total_tool_calls += 1

        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in model_map:
            model_map[mkey] = {
                "model": entry.get("model", ""),
                "provider": entry.get("provider", ""),
                "calls": 0, "tokens": 0, "cost": 0.0,
            }
        mm = model_map[mkey]
        mm["calls"] += 1
        mm["tokens"] += tok
        mm["cost"] += cost_val

    # Zero-fill date axis anchored on midnight(today, local TZ).
    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    date_range = [
        (today_local - timedelta(days=range_days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(range_days)
    ]

    cost_and_tokens = []
    messages_list = []
    performance_list = []

    for d in date_range:
        if d in daily_cost:
            dc = daily_cost[d]
            cost_and_tokens.append({"date": d, "tokens": dc["tokens"], "cost": round(dc["cost"], 6)})
        else:
            cost_and_tokens.append({"date": d, "tokens": 0, "cost": 0})

        if d in daily_msgs:
            dm = daily_msgs[d]
            messages_list.append({
                "date": d, "total": dm["total"], "user": dm["user"],
                "assistant": dm["assistant"], "toolCalls": dm["toolCalls"],
            })
        else:
            messages_list.append({"date": d, "total": 0, "user": 0, "assistant": 0, "toolCalls": 0})

        vals = daily_perf.get(d, [])
        if vals:
            vals_sorted = sorted(vals)
            p95_idx = max(0, int(len(vals_sorted) * 0.95) - 1)
            performance_list.append({
                "date": d,
                "avgMs": round(sum(vals_sorted) / len(vals_sorted)),
                "p95Ms": vals_sorted[p95_idx],
                "minMs": vals_sorted[0],
                "maxMs": vals_sorted[-1],
            })
        else:
            performance_list.append({"date": d, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0})

    avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

    return {
        "stats": {
            "totalCost": round(total_cost, 6),
            "totalTokens": total_tokens,
            "totalMessages": total_messages,
            "avgLatencyMs": avg_latency,
        },
        "costAndTokens": cost_and_tokens,
        "messages": messages_list,
        "performance": performance_list,
        "toolUsage": {
            "totalCalls": total_tool_calls,
            "uniqueTools": len(tool_counter),
            "tools": sorted(
                [{"name": k, "count": v} for k, v in tool_counter.items()],
                key=lambda t: -t["count"],
            ),
        },
        "modelUsage": sorted(
            [
                {
                    "model": m["model"], "provider": m["provider"], "calls": m["calls"],
                    "tokens": m["tokens"], "cost": round(m["cost"], 6),
                }
                for m in model_map.values()
            ],
            key=lambda m: -m["cost"],
        ),
    }


_CardRow = tuple[float, int, str | None]
_CardContribution = tuple[bool, tuple[_CardRow, ...]]
_CARD_CACHE_MAX_FILES = 4096
_CARD_CACHE_MAX_ROWS = 131_072


def _card_rows(entries: Iterable[dict]) -> Iterator[_CardRow]:
    for entry in entries:
        if entry.get("role") == "user":
            continue
        usage = entry["usage"]
        cost = usage.get("cost", {}).get("total", 0) or 0
        tokens = usage.get("totalTokens", 0) or 0
        ts = entry.get("timestamp")
        yield cost, tokens, date_from_ms(ts) if ts else None


def _file_stamp(path: str) -> tuple[int, int, int, int, int]:
    stat = os.stat(path)
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class _CardCache:
    """Cache only card fields, retaining entry order for exact cost rounding."""

    def __init__(self, entries: Callable[..., Iterator[dict]]) -> None:
        self._entries = entries
        self._files: OrderedDict[tuple, _CardContribution] = OrderedDict()
        self._rows = 0
        self._lock = Lock()

    def read(self, path: str, start_ms: int | None, end_ms: int | None) -> _CardContribution:
        try:
            stamp = _file_stamp(path)
        except OSError:
            return False, ()
        key = path, stamp, start_ms, end_ms
        with self._lock:
            cached = self._files.get(key)
            if cached is not None:
                self._files.move_to_end(key)
                return cached

        found = False
        rows: list[_CardRow] = []
        try:
            entries = self._entries(path, start_ms=start_ms, end_ms=end_ms)
            first = next(entries, None)
            if first is not None:
                found = True
                rows.extend(_card_rows(chain((first,), entries)))
        except OSError:
            # Preserve the readable prefix, but retry the file next time.
            return found, tuple(rows)
        except Exception:
            # Like collect_entries, discard an invalid transcript.
            return False, ()

        result = found, tuple(rows)
        try:
            stable = _file_stamp(path) == stamp
        except OSError:
            stable = False
        count = len(result[1])
        if stable and count <= _CARD_CACHE_MAX_ROWS:
            with self._lock:
                if key not in self._files:
                    self._files[key] = result
                    self._rows += count
                while len(self._files) > _CARD_CACHE_MAX_FILES or self._rows > _CARD_CACHE_MAX_ROWS:
                    _, (_, evicted) = self._files.popitem(last=False)
                    self._rows -= len(evicted)
        return result


def _card_inputs(
    source: Source, start_ms: int | None, end_ms: int | None,
) -> Iterator[tuple[bool, Iterable[_CardRow]]]:
    cache = source.card_cache
    if cache is None:
        entries = collect_entries(source, start_ms, end_ms)
        yield bool(entries), _card_rows(entries)
        return
    for path in source.discover():
        yield cache.read(path, start_ms, end_ms)


def summary_card(source: Source, *, window: dict) -> dict:
    """Lightweight headline card: totals + per-day cost/tokens/assistant-msgs."""
    days = int(window.get("days", 5)) if "days" in window else 5
    start_ms, end_ms, _ = window_to_ms(window)

    has_entries = False
    daily: dict[str, dict] = defaultdict(lambda: {"date": "", "cost": 0.0, "tokens": 0, "messages": 0})
    total_cost = 0.0
    total_tokens = 0
    total_messages = 0  # legacy semantic: assistant-only count

    for found, rows in _card_inputs(source, start_ms, end_ms):
        has_entries |= found
        for cost, tokens, day in rows:
            total_cost += cost
            total_tokens += tokens
            total_messages += 1

            if day is not None:
                row = daily[day]
                row["date"] = day
                row["cost"] += cost
                row["tokens"] += tokens
                row["messages"] += 1

    if not has_entries:
        return {"days": days, "totalCost": 0, "totalMessages": 0, "totalTokens": 0, "dailyCost": []}

    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_list = []
    for i in range(days):
        d = (today_local - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        if d in daily:
            row = daily[d]
            daily_list.append({
                "date": row["date"], "cost": round(row["cost"], 6),
                "tokens": row["tokens"], "messages": row["messages"],
            })
        else:
            daily_list.append({"date": d, "cost": 0, "tokens": 0, "messages": 0})

    return {
        "days": days,
        "totalCost": round(total_cost, 6),
        "totalMessages": total_messages,
        "totalTokens": total_tokens,
        "dailyCost": daily_list,
    }


def summary(source: Source, *, window: dict) -> dict:
    """Aggregated SessionCostSummary across all sessions in the window,
    plus per-session sub-summaries in ``sessions[]``."""
    start_ms, end_ms, _ = window_to_ms(window)

    files = source.discover()
    if not files:
        return {"error": "No session files found"}

    all_entries: list = []
    session_summaries: list = []
    for sf in files:
        meta, entries = source.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        if entries:
            session_summaries.append(build_summary(meta, entries))
            all_entries.extend(entries)

    if not all_entries:
        return {"error": "No usage data found in the given range"}

    combined = build_summary(
        {"sessionId": "all", "sessionFile": f"{len(files)} files"},
        all_entries,
    )
    combined["sessionCount"] = len(session_summaries)
    combined["sessions"] = session_summaries
    return combined


def list_sessions(source: Source) -> dict:
    """One row per discovered session. ``messageCount`` is assistant-only
    (preserved legacy contract)."""
    sessions = []
    for sf in source.discover():
        meta, entries = source.parse_file(sf, start_ms=None, end_ms=None)
        if not meta:
            continue
        assistant_entries = [e for e in entries if e.get("role") != "user"]
        total_cost = sum(
            (e["usage"].get("cost", {}).get("total", 0) or 0) for e in assistant_entries
        )
        total_tokens = sum(
            (e["usage"].get("totalTokens", 0) or 0) for e in assistant_entries
        )
        timestamps = [e["timestamp"] for e in entries if e.get("timestamp")]

        sessions.append({
            "sessionId": meta.get("sessionId"),
            "sessionFile": meta.get("sessionFile"),
            "messageCount": len(assistant_entries),
            "totalTokens": total_tokens,
            "totalCost": round(total_cost, 6),
            "firstActivity": min(timestamps) if timestamps else None,
            "lastActivity": max(timestamps) if timestamps else None,
        })

    return {"count": len(sessions), "sessions": sessions}


def get_session(source: Source, session_id: str, *, window: dict | None = None) -> dict | None:
    """Detailed SessionCostSummary for one session, optionally windowed.
    Returns None if not found so the caller can 404."""
    start_ms, end_ms, _ = window_to_ms(window or {})

    for sf in source.discover():
        import os
        if session_id in os.path.basename(sf):
            meta, entries = source.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
            if not entries:
                return {"error": "No usage data found for this session in the given range"}
            return build_summary(meta, entries)

    return None


def aggregate_for_sync(source: Source, *, since_date: Optional[str] = None) -> dict:
    """Return ``{report_date: per-day dict}`` matching the swarm /usage/report
    payload shape (minus workspace identifiers, which the orchestrator adds).

    Also returns the per-date count of contributing session files via the
    sentinel key ``"__session_dates__"`` so the orchestrator can fill
    ``total_sessions`` without re-walking the parser. ``"__parse_errors__"``
    carries the basenames that failed to parse, if any."""
    session_files = source.discover()
    if not session_files:
        return {"__session_dates__": {}}

    import os
    all_entries: list = []
    session_dates: dict[str, set] = defaultdict(set)
    parse_errors: list[str] = []

    for sf_idx, sf in enumerate(session_files):
        try:
            _, entries = source.parse_file(sf)
        except Exception as e:
            parse_errors.append(f"{os.path.basename(sf)}: {e}")
            continue

        if since_date:
            entries = [
                e for e in entries
                if e.get("timestamp") and date_from_ms(e["timestamp"]) >= since_date
            ]

        for e in entries:
            if e.get("timestamp"):
                session_dates[date_from_ms(e["timestamp"])].add(sf_idx)

        all_entries.extend(entries)

    if not all_entries:
        return {
            "__session_dates__": {},
            "__parse_errors__": parse_errors,
        }

    days = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "input_cost": 0.0,
        "output_cost": 0.0,
        "cache_read_cost": 0.0,
        "cache_write_cost": 0.0,
        "total_messages": 0,
        "total_tool_calls": 0,
        "_model_map": {},
        "_tool_counter": defaultdict(int),
    })

    for entry in all_entries:
        ts = entry.get("timestamp")
        if not ts:
            continue

        date_str = date_from_ms(ts)
        d = days[date_str]

        # User records still count toward total_messages (matches gateway
        # messageCounts.total = user + assistant) but carry no usage payload.
        if entry.get("role") == "user":
            d["total_messages"] += 1
            continue

        usage = entry.get("usage") or {}
        cost_obj = usage.get("cost", {}) or {}

        inp = usage.get("input", 0) or 0
        out = usage.get("output", 0) or 0
        cr = usage.get("cacheRead", 0) or 0
        cw = usage.get("cacheWrite", 0) or 0
        tok = usage.get("totalTokens", 0) or (inp + out + cr + cw)
        c_total = cost_obj.get("total", 0) or 0

        d["total_input_tokens"] += inp
        d["total_output_tokens"] += out
        d["total_cache_read_tokens"] += cr
        d["total_cache_write_tokens"] += cw
        d["total_tokens"] += tok
        d["total_cost"] += c_total
        d["input_cost"] += cost_obj.get("input", 0) or 0
        d["output_cost"] += cost_obj.get("output", 0) or 0
        d["cache_read_cost"] += cost_obj.get("cacheRead", 0) or 0
        d["cache_write_cost"] += cost_obj.get("cacheWrite", 0) or 0
        d["total_messages"] += 1

        for tn in entry.get("toolNames", []):
            d["_tool_counter"][tn] += 1
            d["total_tool_calls"] += 1

        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in d["_model_map"]:
            d["_model_map"][mkey] = {
                "provider": entry.get("provider", ""),
                "model": entry.get("model", ""),
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        mm = d["_model_map"][mkey]
        mm["calls"] += 1
        mm["tokens"] += tok
        mm["cost"] += c_total

    result: dict = {}
    for date_str, d in days.items():
        result[date_str] = {
            "report_date": date_str,
            "total_input_tokens": d["total_input_tokens"],
            "total_output_tokens": d["total_output_tokens"],
            "total_cache_read_tokens": d["total_cache_read_tokens"],
            "total_cache_write_tokens": d["total_cache_write_tokens"],
            "total_tokens": d["total_tokens"],
            "total_cost": round(d["total_cost"], 8),
            "input_cost": round(d["input_cost"], 8),
            "output_cost": round(d["output_cost"], 8),
            "cache_read_cost": round(d["cache_read_cost"], 8),
            "cache_write_cost": round(d["cache_write_cost"], 8),
            "total_messages": d["total_messages"],
            "total_sessions": 0,  # filled by orchestrator using __session_dates__
            "total_tool_calls": d["total_tool_calls"],
            "model_usage": list(d["_model_map"].values()),
            "tool_usage": [
                {"name": k, "count": v}
                for k, v in sorted(d["_tool_counter"].items(), key=lambda x: -x[1])
            ],
        }
    result["__session_dates__"] = {k: len(v) for k, v in session_dates.items()}
    if parse_errors:
        result["__parse_errors__"] = parse_errors
    return result
