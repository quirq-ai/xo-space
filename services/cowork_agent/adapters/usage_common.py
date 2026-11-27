"""
Shared usage aggregation over normalized **usage entries**.

Every runtime whose CLI leaves per-call records on disk exposes the same usage
views. What differs is *where* its records live and *how* one record maps to a
normalized entry (its :class:`Source`). Everything downstream (windowing,
per-session summaries, the analytics/summary/sessions payloads and the daily
sync rollup) works on the resulting **entries** and is shared here; the
``/api/usage`` dashboard reads each file's kept :class:`Digest` instead, so a
file is parsed again only once it changes.

An **entry** is one normalized usage record:

    user entry:        {"role": "user", "timestamp": <epoch_ms|None>}
    assistant entry:   {"role": "assistant", "timestamp": <epoch_ms|None>,
                        "usage": {"input","output","cacheRead","cacheWrite",
                                  "totalTokens","cost": {...}},
                        "provider": str, "model": str, "stopReason": str|None,
                        "toolNames": [str], "toolResultCounts": {"total","errors"},
                        "durationMs": int|None}

An adapter wires its discovery + parser into a :class:`Source` and binds the
view functions below to its module surface. A model call or a sent message
that appears in more than one file (a fork or a resume copies them) counts
once. A file that can't be parsed is left out (the sync names how many).

A runtime that keeps only rolled-up rows (no per-call records) synthesizes its
own views instead.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import zlib
from array import array
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from itertools import chain
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from services.storage.atomic_write import write_bytes_durable
from services.storage.layout import usage_dir

logger = logging.getLogger(__name__)


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
        session_of: Callable[[str], Optional[str]] | None = None,
    ) -> None:
        self.discover = discover
        self.parse_file = parse_file
        #: The session a file's turns belong to, read cheaply (a sub-agent's
        #: file: its parent's); without it a session is the file named after it.
        self.session_of = session_of
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


def date_from_ms(epoch_ms: int, zone: tzinfo = timezone.utc) -> str:
    """The calendar day of a time in ``zone`` (UTC unless a window names another)."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=zone).strftime("%Y-%m-%d")


def window_zone(window: dict | None) -> tzinfo:
    """The zone a window's days are in: a ``days`` window's own (``tz``), else
    UTC, as an explicit ``start``/``end`` window is."""
    if window and "days" in window and "start" not in window and "end" not in window:
        return resolve_tz(window.get("tz", "local"))
    return timezone.utc


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
            int(datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000 - 1
            if e else None
        )
        if start_ms and end_ms:
            range_days = max(1, (end_ms + 1 - start_ms) // 86_400_000)
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


def counted_once(entries: Iterable[dict], counted: set) -> list:
    """``entries`` without the model calls and sent messages already in
    ``counted`` (by their ``messageId``: a call's response id, a message's own
    id), which it then holds too. A record can be in more than one file: a
    sub-agent's or a fork's transcript copies records of its parent's."""
    out = []
    for entry in entries:
        key = entry.get("messageId")
        if key:
            key = (entry.get("role") == "user", key)
            if key in counted:
                continue
            counted.add(key)
        out.append(entry)
    return out


def parsed_files(source: Source, start_ms: Optional[int], end_ms: Optional[int]) -> Iterator[tuple[str, dict, list]]:
    """``(path, meta, entries)`` for every file the source parses, each model
    call in the first file it appears in only. A file that fails to parse is
    left out."""
    counted: set = set()
    for sf in source.discover():
        try:
            meta, entries = source.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        except Exception:
            continue
        yield sf, meta, counted_once(entries, counted)


def collect_entries(source: Source, start_ms: Optional[int], end_ms: Optional[int]) -> list:
    """Walk every session file via the source's parser and flatten entries."""
    all_entries: list = []
    for _sf, _meta, entries in parsed_files(source, start_ms, end_ms):
        all_entries.extend(entries)
    return all_entries


# ─────────────────────────────────────────────────────────────────────────────
# build_summary — SessionCostSummary-compatible aggregation over entries.
# ─────────────────────────────────────────────────────────────────────────────


def build_summary(session_meta: dict, entries: list, *, zone: tzinfo = timezone.utc) -> dict:
    """Aggregate normalized entries into a SessionCostSummary-compatible dict
    (its days are calendar days in ``zone``)."""
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
                date_str = date_from_ms(ts, zone)
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
            date_str = date_from_ms(ts, zone)
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
    zone = window_zone(window)

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
                d = date_from_ms(ts, zone)
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
            d = date_from_ms(ts, zone)
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

    # Zero-fill date axis anchored on midnight(today, in the window's zone).
    today_local = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
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


#: A model call on the card: its cost, tokens, time (epoch ms) and message key.
_CardRow = tuple[float, int, int | None, int]
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
        yield cost, tokens, entry.get("timestamp") or None, message_key(entry.get("messageId"))


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
    zone = window_zone(window)
    day_of = _Days(zone)
    counted: set[int] = set()

    has_entries = False
    daily: dict[str, dict] = defaultdict(lambda: {"date": "", "cost": 0.0, "tokens": 0, "messages": 0})
    total_cost = 0.0
    total_tokens = 0
    total_messages = 0  # legacy semantic: assistant-only count

    for found, rows in _card_inputs(source, start_ms, end_ms):
        has_entries |= found
        for cost, tokens, ts, key in rows:
            if key:
                if key in counted:
                    continue  # a call another file holds too (a sub-agent's copy)
                counted.add(key)
            day = day_of(ts * 1000) if ts else None
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

    today_local = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
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


def _by_session(files: Iterable[tuple[str, dict, list]]) -> dict[str, tuple[dict, list]]:
    """Each session's meta (its first file's) and entries, its files (its
    sub-agents') together, in discovery order."""
    sessions: dict[str, tuple[dict, list]] = {}
    for sf, meta, entries in files:
        key = (meta or {}).get("sessionId") or sf
        if key in sessions:
            sessions[key][1].extend(entries)
        else:
            sessions[key] = (meta or {}, list(entries))
    return sessions


def summary(source: Source, *, window: dict) -> dict:
    """Aggregated SessionCostSummary across all sessions in the window,
    plus per-session sub-summaries in ``sessions[]``."""
    start_ms, end_ms, _ = window_to_ms(window)
    zone = window_zone(window)

    files = source.discover()
    if not files:
        return {"error": "No session files found"}

    all_entries: list = []
    session_summaries: list = []
    for meta, entries in _by_session(parsed_files(source, start_ms, end_ms)).values():
        if entries:
            session_summaries.append(build_summary(meta, entries, zone=zone))
            all_entries.extend(entries)

    if not all_entries:
        return {"error": "No usage data found in the given range"}

    combined = build_summary(
        {"sessionId": "all", "sessionFile": f"{len(files)} files"},
        all_entries, zone=zone,
    )
    combined["sessionCount"] = len(session_summaries)
    combined["sessions"] = session_summaries
    return combined


def list_sessions(source: Source) -> dict:
    """One row per session (a session's sub-agent files in its row).
    ``messageCount`` is assistant-only (preserved legacy contract)."""
    sessions = []
    for meta, entries in _by_session(parsed_files(source, None, None)).values():
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
    """Detailed SessionCostSummary for one session (with its sub-agents'
    files, where the source can tell them), optionally windowed.
    Returns None if not found so the caller can 404."""
    start_ms, end_ms, _ = window_to_ms(window or {})

    def belongs(sf: str) -> bool:
        if source.session_of is not None:
            try:
                return source.session_of(sf) == session_id
            except Exception:
                return False
        return session_id in os.path.basename(sf)

    files = [sf for sf in source.discover() if belongs(sf)]
    if not files:
        return None
    counted: set = set()
    meta: dict = {}
    entries: list = []
    for sf in files:
        try:
            file_meta, file_entries = source.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        except Exception:
            continue
        meta = meta or file_meta
        entries.extend(counted_once(file_entries, counted))
    if not entries:
        return {"error": "No usage data found for this session in the given range"}
    return build_summary(meta, entries, zone=window_zone(window))


def aggregate_for_sync(source: Source, *, since_date: Optional[str] = None) -> dict:
    """Return ``{report_date: per-day dict}`` matching the swarm /usage/report
    payload shape (minus workspace identifiers, which the orchestrator adds).

    Also returns the per-date count of contributing sessions (a session's
    sub-agent files count once, as it) via the sentinel key
    ``"__session_dates__"`` so the orchestrator can fill ``total_sessions``
    without re-walking the parser. ``"__parse_errors__"`` carries the
    basenames that failed to parse, if any. Days are UTC days; a model call in
    more than one file counts once, in the first."""
    session_files = source.discover()
    if not session_files:
        return {"__session_dates__": {}}

    all_entries: list = []
    session_dates: dict[str, set] = defaultdict(set)
    parse_errors: list[str] = []
    counted: set = set()

    for sf in session_files:
        try:
            meta, entries = source.parse_file(sf)
        except Exception as e:
            parse_errors.append(f"{os.path.basename(sf)}: {e}")
            continue
        entries = counted_once(entries, counted)
        sf_idx = (meta or {}).get("sessionId") or sf

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


# ─────────────────────────────────────────────────────────────────────────────
# The /api/usage dashboard over kept digests.
#
# A transcript holds far more than its usage: the CLIs' transcripts run to
# gigabytes of tool output, and the dashboard used to read and parse all of it
# on every call (13 s and 9 s on 11-27 for 4 GiB of transcripts). A digest is
# what the dashboard reads of one file: its turns' times and token counts, in
# file order, packed into one array. It holds nothing that depends on the
# window, so one digest per file serves every window: it is kept until the
# file changes and re-read only then (a live session's file, while it runs),
# and kept on disk too, so a restart re-reads only the files that changed.
# ─────────────────────────────────────────────────────────────────────────────

_MICROSECOND = timedelta(microseconds=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_QUARTER_HOUR_US = 15 * 60 * 1_000_000
#: Each event is ``_STRIDE`` ints: kind, time (µs since the epoch), message
#: key (``message_key``; 0 for none), ref (a user turn's timestamp or a reply's
#: model, a string ref), then a reply's input, output, cache-read and
#: cache-write tokens and the tokens its day and session count.
_STRIDE = 9
_USER, _USER_COUNTED, _REPLY = 0, 1, 2


def message_key(message_id: object) -> int:
    """A message id as a positive 63-bit int, 0 for none: what tells one model
    call's copies apart from another's (in another part of a streamed reply,
    or in another file: a sub-agent's transcript copies its parent's records),
    in 8 bytes rather than the id's 80. Two different ids share a key with
    odds of about one in 10**9 across a million messages."""
    if not isinstance(message_id, str) or not message_id:
        return 0
    return (int.from_bytes(hashlib.blake2b(message_id.encode(), digest_size=8).digest(), "big") >> 1) or 1


class Digest:
    """What the dashboard reads of one transcript; built by :class:`DigestBuilder`."""

    __slots__ = ("file_id", "session_id", "title", "ints", "strings")

    def __init__(self, file_id: str, session_id: str, title: str, ints: "array", strings: tuple[str, ...]) -> None:
        self.file_id = file_id          # the session a recently changed file counts as, even with no turns
        self.session_id = session_id    # the session its turns count under (a sub-agent's: its parent's)
        self.title = title              # "" when the file doesn't name its session (a sub-agent's)
        self.ints = ints
        self.strings = strings

    def __len__(self) -> int:
        return len(self.ints) // _STRIDE


class DigestBuilder:
    """Collects a transcript's turns in file order. Only turns with a readable,
    zoned time belong here: no window could place the others."""

    def __init__(self) -> None:
        self._ints = array("q")
        self._refs: dict[str, int] = {}

    def _ref(self, text: str) -> int:
        return self._refs.setdefault(text, len(self._refs))

    def user(self, at: datetime, stamp: str, *, counted: bool, message_id: str | None = None) -> None:
        """A user turn: ``counted`` when it is a message a person (or XO, for
        them) sent; one the CLI made itself (a tool result, an interruption
        marker) isn't counted but still times the next reply. Records sharing
        a ``message_id`` are one message (copied into another file)."""
        if at.tzinfo is None:
            return
        self._ints.extend((_USER_COUNTED if counted else _USER, (at - _EPOCH) // _MICROSECOND,
                           message_key(message_id), self._ref(stamp), 0, 0, 0, 0, 0))

    def reply(self, at: datetime, *, model: str, input: int, output: int, cache_read: int, cache_write: int,
              tokens: int, message_id: str | None = None) -> None:
        """One model call's usage. Records sharing a ``message_id`` are one call
        (in parts, or copied into another file): the first in the window counts."""
        if at.tzinfo is None:
            return
        self._ints.extend((_REPLY, (at - _EPOCH) // _MICROSECOND, message_key(message_id), self._ref(model),
                           input, output, cache_read, cache_write, tokens))

    def build(self, *, file_id: str, session_id: str, title: str) -> Digest:
        return Digest(file_id, session_id, title, self._ints, tuple(self._refs))


class Digests:
    """One kept digest per transcript, read again only once the file changes.

    With ``kept_as`` (the runtime's name) the digests are also kept on disk,
    in ``<usage_dir>/<kept_as>.digests``, so a restart re-reads only the files
    that changed since: see :class:`_DigestFile`. The file is written after a
    read that changed what is kept, at most once a minute (a live session's
    transcript changes on every read): a change not written yet only costs a
    restart re-reading that file."""

    def __init__(self, digest: Callable[[str], Optional[Digest]], *, kept_as: Optional[str] = None) -> None:
        self._digest = digest
        self._kept: dict[str, tuple[tuple, Optional[Digest]]] = {}
        self._lock = Lock()
        self._file = _DigestFile(kept_as, digest) if kept_as else None
        self._loaded = self._file is None
        self._saved_at = float("-inf")

    def read(self, paths: list[str]) -> Iterator[tuple[str, Optional[Digest]]]:
        """``(path, digest)`` for each path, in order; a file that can't be read
        yields ``None``. Files no longer on disk are let go."""
        with self._lock:
            if not self._loaded:            # the first read after a start: what the last run kept
                self._kept, self._loaded = self._file.load(), True
            gone = self._kept.keys() - set(paths)
            for path in gone:
                del self._kept[path]
        changed = bool(gone)
        for path in paths:
            try:
                stamp = _file_stamp(path)
            except OSError:
                yield path, None
                continue
            with self._lock:
                kept = self._kept.get(path)
            if kept is not None and kept[0] == stamp:
                yield path, kept[1]
                continue
            digest = self._digest(path)
            try:
                stable = _file_stamp(path) == stamp
            except OSError:
                stable = False
            if stable:  # a file written to while it was read is read again next time
                with self._lock:
                    self._kept[path] = stamp, digest
                changed = True
            yield path, digest
        if changed and self._file is not None:
            with self._lock:
                due = time.monotonic() - self._saved_at >= _SAVE_EVERY_S
                if due:
                    self._saved_at = time.monotonic()
                    kept = dict(self._kept)
            if due:
                self._file.save(kept)


#: The least time between two writes of a runtime's digests file.
_SAVE_EVERY_S = 60.0


#: Bump when a digest's layout or meaning changes: an older file is then read as empty.
_DIGEST_FILE_SCHEMA = 1


class _DigestFile:
    """A runtime's digests on disk, so a restart doesn't re-read every transcript.

    The transcripts stay the source of truth: a digest is used only while its
    file's stamp (device, inode, size, mtime, ctime) is the one it was read
    at, and only by the code that wrote it (``code``: a hash of the usage code
    that builds and reads digests). A file that is missing, unreadable, from
    another schema or from other code reads as empty: every transcript is read
    again, as after any start before. A write replaces the whole file
    atomically, and only after a read changed what is kept.

    Layout: zlib of one JSON header line, then every digest's ints (int64, the
    header's byte order) back to back, in the header's order.
    """

    def __init__(self, name: str, digest: Callable) -> None:
        self.path = usage_dir() / f"{name}.digests"
        self._digest = digest
        self._code: Optional[str] = None
        self._save_lock = Lock()

    def code(self) -> str:
        """The usage code's hash: this module and every module beside the digest's builder."""
        if self._code is None:
            builder = Path(sys.modules[self._digest.__module__].__file__).parent
            h = hashlib.blake2b(digest_size=16)
            for source in [Path(__file__), *sorted(builder.glob("*.py"))]:
                h.update(source.name.encode() + b"\0" + source.read_bytes())
            self._code = h.hexdigest()
        return self._code

    def load(self) -> dict[str, tuple[tuple, Optional[Digest]]]:
        try:
            raw = zlib.decompress(self.path.read_bytes())
            head, _, blob = raw.partition(b"\n")
            header = json.loads(head)
            if (header.get("schema"), header.get("code"), header.get("byteorder")) != (
                    _DIGEST_FILE_SCHEMA, self.code(), sys.byteorder):
                return {}
            ints = array("q")
            ints.frombytes(blob)
            kept: dict[str, tuple[tuple, Optional[Digest]]] = {}
            at = 0
            for path, stamp, file_id, session_id, title, strings, count in header["files"]:
                kept[path] = tuple(stamp), Digest(file_id, session_id, title, ints[at:at + count], tuple(strings))
                at += count
            if at != len(ints):
                return {}
            return kept
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 - a bad cache costs a re-read, never the dashboard
            logger.info("usage: %s can't be used (%s: %s); reading every transcript", self.path,
                        type(exc).__name__, exc)
            return {}

    def save(self, kept: dict[str, tuple[tuple, Optional[Digest]]]) -> None:
        files, ints = [], array("q")
        for path, (stamp, digest) in kept.items():
            if digest is None:      # a file that can't be read is tried again after a start
                continue
            files.append((path, stamp, digest.file_id, digest.session_id, digest.title, digest.strings,
                          len(digest.ints)))
            ints.extend(digest.ints)
        header = {"schema": _DIGEST_FILE_SCHEMA, "code": self.code(), "byteorder": sys.byteorder, "files": files}
        data = zlib.compress(json.dumps(header, separators=(",", ":")).encode() + b"\n" + ints.tobytes(), 1)
        try:
            with self._save_lock:
                write_bytes_durable(self.path, data)
        except OSError as exc:
            logger.warning("usage: can't keep digests in %s: %s", self.path, exc)


class _Days:
    """The calendar day in ``zone`` of a time in µs. Every zone's offset and
    every change of it falls on a quarter hour, so one lookup per quarter hour
    is exact."""

    def __init__(self, zone: tzinfo) -> None:
        self._zone = zone
        self._seen: dict[int, str] = {}

    def __call__(self, micros: int) -> str:
        quarter = micros // _QUARTER_HOUR_US
        day = self._seen.get(quarter)
        if day is None:
            moment = _EPOCH + timedelta(microseconds=quarter * _QUARTER_HOUR_US)
            day = self._seen[quarter] = moment.astimezone(self._zone).date().isoformat()
        return day


def _empty_day(date: str) -> dict:
    """A dashboard day: ``tokens`` is fresh input + output; ``total_tokens`` the
    day's split, keyed as the window's own ``total_tokens``, so the days add up to it."""
    return {"date": date, "cost": 0.0, "tokens": 0, "messages": 0, "total_tokens": empty_tokens()}


def dashboard_from_digests(
    digests: Digests, paths: list[str], *, days: int, zone: tzinfo, provider: str,
) -> dict:
    """UsageStats for the last ``days`` days in ``zone``, from each file's digest.

    A turn before the window's start (midnight in ``zone``) is skipped; a model
    call or a sent message is counted once, the first time it appears in the
    window in any file;
    a reply is timed from the user turn before it in its own file when that is
    at most ten minutes earlier; any file changed in the window counts as a
    session even with no turns in it; a session's files (its sub-agents')
    add up to one row. Days are calendar days in ``zone``. Cost is always 0.0.
    """
    days = max(1, min(days, 365))
    today = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=days - 1)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    since = (cutoff - _EPOCH) // _MICROSECOND
    day_of = _Days(zone)

    total_tokens = empty_tokens()
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()
    counted: set[int] = set()
    sent: set[int] = set()
    titles: dict[str, str] = {}
    by_day: dict[str, dict] = {}
    by_model_key: dict[tuple[str, str], dict] = {}
    session_stats: dict[str, dict] = {}
    response_times: list[float] = []

    for path, digest in digests.read(paths):
        if digest is None:
            continue
        try:
            if os.path.getmtime(path) * 1000 >= cutoff_ms:
                session_ids.add(digest.file_id)
        except OSError:
            pass
        if digest.title and digest.session_id not in titles:
            titles[digest.session_id] = digest.title
        if not len(digest):
            continue

        ints, strings = digest.ints, digest.strings
        session_tokens = 0
        session_messages = 0
        first_user_ts: Optional[str] = None
        last_user_us: Optional[int] = None
        for i in range(0, len(ints), _STRIDE):
            kind, at, key, ref, inp, out, cache_r, cache_w, tokens = ints[i:i + _STRIDE]
            if at < since:
                continue
            if kind != _REPLY:
                if kind == _USER_COUNTED and not (key and key in sent):
                    if key:
                        sent.add(key)
                    user_messages += 1
                    session_ids.add(digest.session_id)
                last_user_us = at
                if first_user_ts is None:
                    first_user_ts = strings[ref]
                continue
            if key:
                if key in counted:
                    continue  # this call was already counted in the window
                counted.add(key)

            total_tokens["input"] += inp
            total_tokens["output"] += out
            total_tokens["cache_read"] += cache_r
            total_tokens["cache_write"] += cache_w
            assistant_messages += 1
            session_ids.add(digest.session_id)

            if last_user_us is not None:
                delta = (at - last_user_us) / 1_000_000
                if 0 <= delta <= 600:
                    response_times.append(delta)
                last_user_us = None

            day_key = day_of(at)
            day = by_day.get(day_key)
            if day is None:
                day = by_day[day_key] = _empty_day(day_key)
            day["tokens"] += tokens
            day["messages"] += 1
            day_tokens = day["total_tokens"]
            day_tokens["input"] += inp
            day_tokens["output"] += out
            day_tokens["cache_read"] += cache_r
            day_tokens["cache_write"] += cache_w

            model_id = strings[ref]
            m = by_model_key.setdefault((model_id, provider), {
                "model_id": model_id, "provider_id": provider,
                "total_cost": 0.0, "total_tokens": empty_tokens(), "message_count": 0,
            })
            m["total_tokens"]["input"] += inp
            m["total_tokens"]["output"] += out
            m["total_tokens"]["cache_read"] += cache_r
            m["total_tokens"]["cache_write"] += cache_w
            m["message_count"] += 1

            session_tokens += tokens
            session_messages += 1

        if session_messages:
            row = session_stats.get(digest.session_id)
            if row is None:
                session_stats[digest.session_id] = {
                    "session_id": digest.session_id, "title": digest.title, "total_cost": 0.0,
                    "total_tokens": session_tokens, "message_count": session_messages,
                    "time_created": first_user_ts,
                }
            else:  # another of the session's files: a sub-agent's
                row["total_tokens"] += session_tokens
                row["message_count"] += session_messages
                row["time_created"] = row["time_created"] or first_user_ts

    for session_id, row in session_stats.items():
        row["title"] = titles.get(session_id) or row["title"] or "Untitled Session"
        row["time_created"] = row["time_created"] or datetime.now(timezone.utc).isoformat()

    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).date().isoformat()
        daily.append(by_day.get(d) or _empty_day(d))

    by_model = sorted(by_model_key.values(), key=lambda m: m["message_count"], reverse=True)
    by_session = sorted(session_stats.values(), key=lambda s: s["total_tokens"], reverse=True)[:10]

    if response_times:
        sorted_rt = sorted(response_times)
        n = len(sorted_rt)
        rt_stats = {
            "avg": sum(sorted_rt) / n, "median": sorted_rt[n // 2],
            "p95": sorted_rt[min(n - 1, int(n * 0.95))], "min": sorted_rt[0], "max": sorted_rt[-1], "count": n,
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
