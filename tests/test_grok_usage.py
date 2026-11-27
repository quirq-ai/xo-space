"""Grok Build usage over Grok's own ledger (usage.json): fresh input is the
prompt minus its cache hits, cost is ticks at 10**10 per USD (unknown when
absent, never free), a forked session's inherited turns count once, a
sub-agent's session isn't counted again, and a cancelled turn is unreported."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.grok_build import session_telemetry as grok_telemetry
from services.cowork_agent.adapters.grok_build import usage as grok_usage

TURN_1 = {"endedAt": "2026-11-26T10:00:30.123456789+00:00", "turnNumber": 1, "primaryModelId": "grok-4",
          "modelUsage": {"grok-4": {"inputTokens": 1000, "cachedReadTokens": 600, "cacheCreationTokens": 100,
                                    "outputTokens": 50, "reasoningTokens": 20, "costUsdTicks": 25_000_000_000}}}
TURN_2 = {"endedAt": "2026-11-27T09:00:20.5+00:00", "turnNumber": 2, "primaryModelId": "grok-4",
          "modelUsage": {"grok-4": {"inputTokens": 500, "cachedReadTokens": 400, "cacheCreationTokens": 0,
                                    "outputTokens": 10}}}     # no costUsdTicks: cost unreported


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()


def _update(stamp: str, kind: str, **extra) -> dict:
    return {"timestamp": _epoch(stamp), "params": {"update": {"sessionUpdate": kind, **extra}}}


class _Nov27(datetime):
    @classmethod
    def now(cls, tz=None):
        moment = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


class GrokUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"GROK_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        grok_usage._kept.clear()
        self.addCleanup(grok_usage._kept.clear)
        # s1: a turn on 11-26, then a prompt on 11-27 whose turn was cancelled.
        s1 = self.session("s1", "2026-11-26T10:00:00+00:00", turns=[TURN_1], updates=[
            _update("2026-11-26T10:00:00", "user_message_chunk", content={"text": "secret prompt"}),
            _update("2026-11-26T10:00:00", "user_message_chunk", content={"text": "more of it"}),
            _update("2026-11-26T10:00:30", "turn_completed", usage={"inputTokens": 1000}),
            _update("2026-11-27T08:00:00", "user_message_chunk", content={"text": "secret"}),
            _update("2026-11-27T08:00:05", "turn_completed"),
        ])
        # s2: forked from s1, so its ledger and log repeat s1's first turn.
        self.session("s2", "2026-11-27T09:00:00+00:00", parent="s1", turns=[TURN_1, TURN_2], updates=[
            _update("2026-11-26T10:00:00", "user_message_chunk", content={"text": "secret prompt"}),
            _update("2026-11-26T10:00:30", "turn_completed", usage={"inputTokens": 1000}),
            _update("2026-11-27T09:00:00", "user_message_chunk", content={"text": "secret"}),
            _update("2026-11-27T09:00:20", "turn_completed", usage={"inputTokens": 500}),
        ])
        # s3: a sub-agent s1 ran; its usage is already in s1's turn.
        self.session("s3", "2026-11-26T10:00:10+00:00", turns=[
            {**TURN_1, "endedAt": "2026-11-26T10:00:20+00:00", "turnNumber": 1}], updates=[])
        (s1 / "subagents" / "s3").mkdir(parents=True)
        (s1 / "subagents" / "s3" / "meta.json").write_text("{}")

    def session(self, sid: str, created: str, *, turns, updates, parent=None) -> Path:
        folder = self.home / "sessions" / "-work-app" / sid
        folder.mkdir(parents=True)
        summary = {"info": {"id": sid, "cwd": "/work/app"}, "created_at": created, "updated_at": created,
                   "current_model_id": "grok-4", "generated_title": "secret title"}
        if parent:
            summary["parent_session_id"] = parent
        (folder / "summary.json").write_text(json.dumps(summary))
        if turns is not None:
            (folder / "usage.json").write_text(json.dumps({"turns": turns}))
        (folder / "updates.jsonl").write_text("".join(json.dumps(row) + "\n" for row in updates))
        return folder

    def test_each_day_has_grok_s_own_figures_each_turn_once(self) -> None:
        payload = grok_usage.sync_payload(since_date=None)
        day26, day27 = payload["2026-11-26"], payload["2026-11-27"]
        self.assertEqual((day26["total_input_tokens"], day26["total_output_tokens"],
                          day26["total_cache_read_tokens"], day26["total_cache_write_tokens"]), (300, 50, 600, 100))
        self.assertEqual(day26["total_cost"], 2.5)
        self.assertEqual((day27["total_input_tokens"], day27["total_output_tokens"],
                          day27["total_cache_read_tokens"]), (100, 10, 400))
        self.assertEqual(day27["total_cost"], 0.0)
        self.assertEqual((day26["total_messages"], day27["total_messages"]), (2, 3))
        self.assertEqual(payload["__session_dates__"], {"2026-11-26": 1, "2026-11-27": 2})
        [model] = day26["model_usage"]
        self.assertEqual((model["provider"], model["model"]), ("xai", "grok-4"))

    def test_the_sub_agents_session_is_not_listed(self) -> None:
        self.assertEqual(sorted(folder.name for _s, folder in grok_usage.sessions()), ["s1", "s2"])
        self.assertEqual(grok_usage.list_sessions()["count"], 2)

    def test_the_dashboard_carries_the_reported_cost_and_says_what_is_unknown(self) -> None:
        with mock.patch.object(grok_usage, "datetime", _Nov27), mock.patch.object(_uc, "datetime", _Nov27):
            stats = grok_usage.dashboard(window={"days": 30, "tz": "utc"})
            week = grok_usage.dashboard(window={"days": 7, "tz": "utc"})
        tokens = stats["total_tokens"]
        self.assertEqual((tokens["input"], tokens["output"], tokens["cache_read"], tokens["cache_write"]),
                         (400, 60, 1000, 100))
        self.assertEqual(stats["total_cost"], 2.5)
        self.assertEqual(stats["unpriced_messages"], 1)
        self.assertNotIn("unreported_calls", stats)
        daily = {row["date"]: (row["tokens"], row["cost"]) for row in stats["daily"] if row["messages"]}
        self.assertEqual(daily, {"2026-11-26": (350, 2.5), "2026-11-27": (110, 0.0)})
        self.assertEqual(week["total_tokens"], stats["total_tokens"])
        self.assertEqual(stats["by_model"][0]["total_cost"], 2.5)

    def test_session_telemetry_carries_real_figures_and_null_for_the_unknown(self) -> None:
        self.session("s4", "2026-11-27T10:00:00+00:00", turns=None, updates=[
            _update("2026-11-27T10:00:00", "user_message_chunk", content={"text": "secret"})])
        payload = grok_telemetry.collect_session_telemetry()
        rows = {row["id"]: row for row in payload["sessions"]}
        self.assertEqual(sorted(rows), ["s1", "s2", "s4"])
        s1, s2, s4 = rows["s1"], rows["s2"], rows["s4"]
        self.assertEqual((s1["fresh"], s1["output"], s1["cache_read"], s1["cache_write"], s1["tokens"]),
                         (300, 50, 600, 100, 1050))
        self.assertEqual((s1["cost"], s1["cost_known"], s1["turns"]), (2.5, True, 2))
        self.assertEqual((s2["tokens"], s2["cost"], s2["cost_known"]), (510, None, False))
        self.assertEqual((s4["tokens"], s4["fresh"], s4["tokens_known"]), (None, None, False))
        totals = payload["totals"]
        self.assertEqual((totals["tokens"], totals["tokens_complete"]), (1560, False))
        self.assertEqual((totals["cost_usd"], totals["cost_complete"]), (2.5, False))
        self.assertNotIn("secret", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
