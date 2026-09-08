"""Shared presentation helpers for the visualizer BFF routers.

The project-scope (``visualizer.py``) and workspace-scope
(``workspace_visualizer.py``) routers read different ``stats.json`` files but
shape them into the **same** wire payloads. The pure ``stats dict → response``
transforms that were duplicated across both routers live here, so the two
routers differ only in *which* scope they read and the genuinely scope-specific
assembly (per-session summaries, sessionslist unions, presence) — not in how a
stats block becomes tokens / tools / models / latency.

Everything here is a pure function of its arguments (no scope, no I/O), so it is
trivially testable and cannot drift between the two tiers.
"""

from __future__ import annotations

import json
from pathlib import Path

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from routers.cowork_agent.bff._visualizer_models import (
    CostAndTokensEntry,
    MessagesEntry,
    ModelUsageEntry,
    ModelUsageWithTotals,
    PerformanceEntry,
    TokenTotals,
    ToolUsage,
    ToolUsageEntry,
)


# ── small shared utilities ────────────────────────────────────────────────────


def bad_query(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"code": "invalid_query", "message": message},
    )


def date_from_ms(epoch_ms: Optional[int]) -> Optional[str]:
    if not epoch_ms:
        return None
    return (
        datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        .strftime("%Y-%m-%d")
    )


def zero_filled_dates(days: int) -> list[str]:
    today = datetime.now(timezone.utc)
    return [
        (today - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(days)
    ]


def rolling_key_for(days: int) -> str:
    """Pick the watcher's rolling window that best matches the request."""
    return "30d" if days > 7 else "7d"


def provider_for_model(model: str) -> str:
    """Best-effort provider tag from a model id. Empty string when unknown
    so the UI degrades gracefully rather than showing a wrong vendor."""
    if not model:
        return ""
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gpt") or model.startswith("o1") or model.startswith("o3"):
        return "openai"
    if model.startswith("gemini"):
        return "google"
    return ""


def row_total_tokens(usage: dict) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )


# ── stats.json readers ─────────────────────────────────────────────────────────


def by_day_from_stats(stats: dict) -> dict[str, dict]:
    """Return the ``by_day`` block as a date-keyed dict (empty if absent —
    schema 1 files have no by_day)."""
    bd = stats.get("by_day")
    return bd if isinstance(bd, dict) else {}


def tokens_from_stats(stats: dict, days: int) -> int:
    """Sum input+output tokens from a stats.json rolling window.
    No cache_read / cache_creation — matches what users see in the UI."""
    rolling = (stats.get("rolling") or {}).get(rolling_key_for(days)) or {}
    t = rolling.get("tokens") or {}
    return int(t.get("input", 0) or 0) + int(t.get("output", 0) or 0)


def _by_model_from_stats(stats: dict, days: int) -> dict[str, dict]:
    """``rolling.<window>.by_model`` block, with ``<synthetic>`` and other
    zero-token entries filtered out so the UI doesn't render placeholder
    models with no activity."""
    rolling = (stats.get("rolling") or {}).get(rolling_key_for(days)) or {}
    by_model = rolling.get("by_model") or {}
    out: dict[str, dict] = {}
    if isinstance(by_model, dict):
        for model, t in by_model.items():
            if not isinstance(t, dict):
                continue
            inp = int(t.get("input", 0) or 0)
            outp = int(t.get("output", 0) or 0)
            if inp + outp <= 0:
                continue
            out[str(model)] = {"input": inp, "output": outp}
    return out


def model_call_counts_from_by_day(by_day: dict[str, dict], dates: list[str]) -> dict[str, int]:
    """``{model_id: total_count}`` summed across ``by_day.<date>.by_model.<model>.count``
    for the requested ``dates`` window. ``rolling.<window>.by_model`` only carries
    tokens, so call/message counts have to come from per-day."""
    counts: dict[str, int] = {}
    for d in dates:
        day = by_day.get(d) or {}
        bm = (day.get("by_model") or {}) if isinstance(day, dict) else {}
        if not isinstance(bm, dict):
            continue
        for model, entry in bm.items():
            if not isinstance(entry, dict):
                continue
            counts[str(model)] = counts.get(str(model), 0) + int(entry.get("count", 0) or 0)
    return counts


# ── model usage rollups ────────────────────────────────────────────────────────


def model_usage_entries(stats: dict, days: int) -> list[ModelUsageEntry]:
    """``/usage/analytics``-shape per-model rollup. Tokens from
    rolling.<window>.by_model; ``calls`` from per-day by_model summed across
    the same window. ``cost`` stays 0 (no pricing table). Sorted by tokens
    descending so the UI's top-of-list is meaningful."""
    by_day = by_day_from_stats(stats)
    counts = model_call_counts_from_by_day(by_day, zero_filled_dates(days))
    entries = [
        ModelUsageEntry(
            model=model,
            provider=provider_for_model(model),
            calls=int(counts.get(model, 0)),
            tokens=t["input"] + t["output"],
            cost=0.0,
        )
        for model, t in _by_model_from_stats(stats, days).items()
    ]
    entries.sort(key=lambda e: e.tokens, reverse=True)
    return entries


def model_usage_with_totals(stats: dict, days: int) -> list[ModelUsageWithTotals]:
    """``SessionCostSummary``-shape per-model rollup. Same source as
    :func:`model_usage_entries` but the nested ``TokenTotals`` shape; ``count``
    from per-day by_model; cache split / costs zero-fill (cache tokens aren't
    tracked per-model anywhere yet; no pricing table)."""
    by_day = by_day_from_stats(stats)
    counts = model_call_counts_from_by_day(by_day, zero_filled_dates(days))
    entries: list[ModelUsageWithTotals] = []
    for model, t in _by_model_from_stats(stats, days).items():
        total = t["input"] + t["output"]
        entries.append(
            ModelUsageWithTotals(
                provider=provider_for_model(model),
                model=model,
                count=int(counts.get(model, 0)),
                totals=TokenTotals(
                    input=t["input"],
                    output=t["output"],
                    cacheRead=0,
                    cacheWrite=0,
                    totalTokens=total,
                    totalCost=0.0,
                    inputCost=0.0,
                    outputCost=0.0,
                    cacheReadCost=0.0,
                    cacheWriteCost=0.0,
                    missingCostEntries=0,
                ),
            )
        )
    entries.sort(key=lambda e: e.totals.totalTokens, reverse=True)
    return entries


# ── tool usage + latency ───────────────────────────────────────────────────────


def tool_usage_from_stats(stats: dict, days: int) -> ToolUsage:
    """Build a ToolUsage entry from the chosen rolling window's by_tool.
    Sorted by count descending; empty shape when the block is absent or has
    no tools."""
    rolling = (stats.get("rolling") or {}).get(rolling_key_for(days)) or {}
    by_tool = rolling.get("by_tool") or {}
    tools: list[ToolUsageEntry] = []
    if isinstance(by_tool, dict):
        for name, count in by_tool.items():
            n = int(count or 0)
            if n <= 0:
                continue
            tools.append(ToolUsageEntry(name=str(name), count=n))
    tools.sort(key=lambda t: t.count, reverse=True)
    total = sum(t.count for t in tools)
    return ToolUsage(totalCalls=total, uniqueTools=len(tools), tools=tools)


def performance_entry_for_day(date: str, day: dict) -> PerformanceEntry:
    """Build one ``PerformanceEntry`` for a single date from its ``latency``
    sub-block. Returns the all-zero entry when no latency samples were
    observed that day."""
    lat = (day.get("latency") or {}) if isinstance(day, dict) else {}
    count = int(lat.get("count", 0) or 0)
    if count <= 0:
        return PerformanceEntry(date=date, avgMs=0, p95Ms=0, minMs=0, maxMs=0)
    sample = sorted(int(x) for x in (lat.get("p95_sample") or []))
    if sample:
        idx = max(0, int(0.95 * (len(sample) - 1)))
        p95 = sample[idx]
    else:
        p95 = 0
    avg = int(int(lat.get("sum_ms", 0) or 0) / count)
    return PerformanceEntry(
        date=date,
        avgMs=avg,
        p95Ms=p95,
        minMs=int(lat.get("min_ms", 0) or 0),
        maxMs=int(lat.get("max_ms", 0) or 0),
    )


def avg_latency_ms_from_by_day(by_day: dict[str, dict]) -> int:
    """Weighted average across every day's latency block. Used for
    ``stats.avgLatencyMs`` on /usage/analytics."""
    total_sum = 0
    total_count = 0
    for day in by_day.values():
        if not isinstance(day, dict):
            continue
        lat = day.get("latency") or {}
        if not isinstance(lat, dict):
            continue
        c = int(lat.get("count", 0) or 0)
        if c <= 0:
            continue
        total_sum += int(lat.get("sum_ms", 0) or 0)
        total_count += c
    return int(total_sum / total_count) if total_count else 0


def cost_and_tokens_for_dates(by_day: dict[str, dict], dates: list[str]) -> list[CostAndTokensEntry]:
    """One entry per date in ``dates`` (chronological), pulling from ``by_day``
    where present, zero where absent. Tokens = input + output (cache token
    classes not counted in the daily chart)."""
    out: list[CostAndTokensEntry] = []
    for d in dates:
        day = by_day.get(d) or {}
        tk = (day.get("tokens") or {}) if isinstance(day, dict) else {}
        out.append(CostAndTokensEntry(
            date=d,
            tokens=int(tk.get("input", 0) or 0) + int(tk.get("output", 0) or 0),
            cost=0.0,
        ))
    return out


def messages_for_dates(by_day: dict[str, dict], dates: list[str]) -> list[MessagesEntry]:
    out: list[MessagesEntry] = []
    for d in dates:
        day = by_day.get(d) or {}
        msgs = (day.get("messages") or {}) if isinstance(day, dict) else {}
        out.append(MessagesEntry(
            date=d,
            total=int(msgs.get("total", 0) or 0),
            user=int(msgs.get("user", 0) or 0),
            assistant=int(msgs.get("assistant", 0) or 0),
            toolCalls=int(msgs.get("toolCalls", 0) or 0),
        ))
    return out


def performance_for_dates(by_day: dict[str, dict], dates: list[str]) -> list[PerformanceEntry]:
    """One ``PerformanceEntry`` per date in ``dates`` (chronological)."""
    return [performance_entry_for_day(d, by_day.get(d) or {}) for d in dates]


# ── timeline event-type filter ─────────────────────────────────────────────────


def _declared_timeline_types() -> frozenset[str]:
    """Every event type ``timeline.schema.json`` declares a branch for.

    Derived, not listed. This was a hardcoded set of twelve, and it had
    already rotted twice by the time anyone noticed: ``todo.status_changed``
    (widened by T7) and all eight ``workitem.*`` types were emitted, written
    to the log, and served by an unfiltered ``GET /timeline`` — but naming one
    in ``?types=`` returned ``400 unknown timeline type``. The same shape as
    defect R3a, where an emitter was widened and the schema's ``oneOf``
    lagged, and the same shape as the route-parity gate that now asserts an
    invariant instead of pinning a count.

    The schema is the declaration of record, so reading it is what keeps this
    honest: a type cannot be filterable unless it is declared, and cannot be
    declared without becoming filterable. Walk ``oneOf`` rather than
    ``definitions`` — an unreferenced definition is not a reachable branch.

    Falls back to the empty set if the schema cannot be read, which makes
    ``?types=`` reject everything rather than silently accept anything; the
    unfiltered route is unaffected either way.
    """
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "services" / "cowork_agent" / "visualizer" / "schema"
        / "timeline.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover - a packaging accident, not a branch
        logger.warning("timeline.schema.json unreadable; ?types= will reject all")
        return frozenset()

    definitions = schema.get("definitions") or {}
    found: set[str] = set()
    for branch in schema.get("oneOf") or []:
        ref = branch.get("$ref", "") if isinstance(branch, dict) else ""
        node = definitions.get(ref.rsplit("/", 1)[-1]) if ref else branch
        if not isinstance(node, dict):
            continue
        const = ((node.get("properties") or {}).get("type") or {}).get("const")
        if isinstance(const, str) and const:
            found.add(const)
    return frozenset(found)


TIMELINE_TYPES = _declared_timeline_types()


def parse_types_param(types: Optional[str]) -> Optional[frozenset]:
    if types is None:
        return None
    requested = {t.strip() for t in types.split(",") if t.strip()}
    unknown = requested - TIMELINE_TYPES
    if unknown:
        raise bad_query(f"unknown timeline type(s): {sorted(unknown)}")
    return frozenset(requested)
