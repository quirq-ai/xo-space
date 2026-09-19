"""The usage wire models: what ``/api/usage/*``, ``/api/xo-projects/usage/*``
and ``/api/xo-projects/{id}/usage/*`` answer with.

Every model forbids extra keys, so a route can never leak a field the
contract does not name. ``routers/cowork_agent/bff/_visualizer_models.py``
re-exports these under their old names for one release.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class _ForbidExtra(BaseModel):
    """Base for every response model: extra keys raise."""

    model_config = ConfigDict(extra="forbid")


# ── /usage/analytics ──────────────────────────────────────────────────────────


class AnalyticsStats(_ForbidExtra):
    totalCost: float
    totalTokens: int
    totalMessages: int
    avgLatencyMs: int


class CostAndTokensEntry(_ForbidExtra):
    date: str
    tokens: int
    cost: float


class MessagesEntry(_ForbidExtra):
    date: str
    total: int
    user: int
    assistant: int
    toolCalls: int


class PerformanceEntry(_ForbidExtra):
    date: str
    avgMs: int
    p95Ms: int
    minMs: int
    maxMs: int


class ToolUsageEntry(_ForbidExtra):
    name: str
    count: int


class ToolUsage(_ForbidExtra):
    totalCalls: int
    uniqueTools: int
    tools: list[ToolUsageEntry]


class ModelUsageEntry(_ForbidExtra):
    model: str
    provider: str
    calls: int
    tokens: int
    cost: float


class UsageAnalyticsResponse(_ForbidExtra):
    stats: AnalyticsStats
    costAndTokens: list[CostAndTokensEntry]
    messages: list[MessagesEntry]
    performance: list[PerformanceEntry]
    toolUsage: ToolUsage
    modelUsage: list[ModelUsageEntry]


# ── /usage/summary/card ───────────────────────────────────────────────────────


class DailyCostEntry(_ForbidExtra):
    date: str
    cost: float
    tokens: int
    messages: int


class UsageSummaryCardResponse(_ForbidExtra):
    days: int
    totalCost: float
    totalMessages: int
    totalTokens: int
    dailyCost: list[DailyCostEntry]


# ── /usage/summary (the full SessionCostSummary) ──────────────────────────────


class TokenTotals(_ForbidExtra):
    input: int
    output: int
    cacheRead: int
    cacheWrite: int
    totalTokens: int
    totalCost: float
    inputCost: float
    outputCost: float
    cacheReadCost: float
    cacheWriteCost: float
    missingCostEntries: int


class DailyBreakdownEntry(_ForbidExtra):
    date: str
    tokens: int
    cost: float


class DailyLatencyEntry(_ForbidExtra):
    date: str
    count: int
    avgMs: int
    p95Ms: int
    minMs: int
    maxMs: int


class DailyModelUsageEntry(_ForbidExtra):
    date: str
    provider: str
    model: str
    tokens: int
    cost: float
    count: int


class MessageCounts(_ForbidExtra):
    total: int
    user: int
    assistant: int
    toolCalls: int
    toolResults: int
    errors: int


class ModelUsageWithTotals(_ForbidExtra):
    provider: str
    model: str
    count: int
    totals: TokenTotals


class SessionCostSummary(_ForbidExtra):
    """One session's full cost and activity summary, or the aggregate
    across many (then ``sessionCount`` and ``sessions`` are set)."""

    sessionId: str
    sessionFile: str
    firstActivity: Optional[int] = None
    lastActivity: Optional[int] = None
    durationMs: Optional[int] = None
    activityDates: list[str] = []

    input: int = 0
    output: int = 0
    cacheRead: int = 0
    cacheWrite: int = 0
    totalTokens: int = 0
    totalCost: float = 0.0
    inputCost: float = 0.0
    outputCost: float = 0.0
    cacheReadCost: float = 0.0
    cacheWriteCost: float = 0.0
    missingCostEntries: int = 0

    dailyBreakdown: list[DailyBreakdownEntry] = []
    dailyLatency: list[DailyLatencyEntry] = []
    dailyModelUsage: list[DailyModelUsageEntry] = []

    messageCounts: MessageCounts = MessageCounts(
        total=0, user=0, assistant=0, toolCalls=0, toolResults=0, errors=0
    )
    toolUsage: ToolUsage = ToolUsage(totalCalls=0, uniqueTools=0, tools=[])
    modelUsage: list[ModelUsageWithTotals] = []

    # Only populated on the aggregate ``/usage/summary``; absent on a
    # single-session ``/usage/sessions/{id}`` response.
    sessionCount: Optional[int] = None
    sessions: Optional[list["SessionCostSummary"]] = None


# ── /usage/sessions ───────────────────────────────────────────────────────────


class SessionListItem(_ForbidExtra):
    sessionId: str
    sessionFile: str
    messageCount: int
    totalTokens: int
    totalCost: float
    firstActivity: Optional[int] = None
    lastActivity: Optional[int] = None
    # Workspace scope adds projectId; project scope omits.
    projectId: Optional[str] = None


class SessionListResponse(_ForbidExtra):
    # Present on project scope (filterable by ``agent_id``); omitted on
    # workspace scope where multiple agents may appear in one list.
    agentId: Optional[str] = None
    count: int
    sessions: list[SessionListItem]
