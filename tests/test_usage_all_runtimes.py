"""``/api/usage`` (the dashboard) and the daily usage sync cover every runtime with a usage module, not only the Space's agent.

Hermetic: the runtimes are fake modules handed to the one discovery seam
(``usage_loader.usage_providers``, or the loader functions under it), the
watermark lives in a temp folder, and the swarm door is a recorder.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.usage_sync as usage_sync
from routers.cowork_agent import usage as usage_routes
from services.cowork_agent.engine import usage_loader
from services.swarm_api._http import SwarmResult


def _module(**views) -> SimpleNamespace:
    """A runtime's usage module."""
    return SimpleNamespace(**views)


def _dashboard(tokens: int, sessions: list[dict], messages: int, rt: dict | None = None, **extra) -> dict:
    return {
        "total_cost": 0.5, "total_tokens": {"input": tokens, "output": 1, "reasoning": 0, "cache_read": 0,
                                            "cache_write": 0},
        "total_sessions": len(sessions), "total_messages": messages, "avg_tokens_per_session": 0,
        "avg_response_time": (rt or {}).get("avg", 0),
        "by_model": [{"model_id": "m", "provider_id": "p", "total_cost": 0.5,
                      "total_tokens": {"input": tokens, "output": 1}, "message_count": messages}],
        "by_session": sessions,
        "daily": [{"date": "2025-01-01", "cost": 0.5, "tokens": tokens, "messages": messages},
                  {"date": "2025-01-02", "cost": 0.0, "tokens": 0, "messages": 0}],
        "response_time": rt or {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0},
        **extra,
    }


class MergedViewTests(unittest.TestCase):
    """AllRuntimesUsage answers one runtime module's dashboard for every runtime at once."""

    def merged(self, **modules) -> usage_loader.AllRuntimesUsage:
        return usage_loader.AllRuntimesUsage(list(modules.items()))

    def test_dashboard_sums_totals_and_merges_days_models_and_sessions(self) -> None:
        alpha = _module(dashboard=lambda window: _dashboard(10, [{"session_id": "a1", "total_tokens": 5}], 2,
                                                            {"avg": 1, "median": 1, "p95": 1, "min": 1, "max": 1,
                                                             "count": 1}))
        beta = _module(dashboard=lambda window: _dashboard(30, [{"session_id": "b1", "total_tokens": 9}], 4,
                                                           {"avg": 4, "median": 4, "p95": 4, "min": 2, "max": 6,
                                                            "count": 3}, estimated_tokens=True))
        out = self.merged(alpha=alpha, beta=beta).dashboard(window={"days": 2})
        self.assertEqual(out["total_cost"], 1.0)
        self.assertEqual(out["total_tokens"]["input"], 40)
        self.assertEqual((out["total_sessions"], out["total_messages"]), (2, 6))
        self.assertEqual(out["avg_tokens_per_session"], 21.0)   # (40 + 2) / 2
        self.assertEqual(out["daily"][0], {"date": "2025-01-01", "cost": 1.0, "tokens": 40, "messages": 6})
        self.assertEqual(len(out["daily"]), 2)
        self.assertEqual(out["by_model"], [{"model_id": "m", "provider_id": "p", "total_cost": 1.0,
                                            "total_tokens": {"input": 40, "output": 2}, "message_count": 6}])
        self.assertEqual([(s["session_id"], s["runtime"]) for s in out["by_session"]],
                         [("b1", "beta"), ("a1", "alpha")])
        self.assertEqual(out["response_time"], {"avg": 3.25, "median": 3.25, "p95": 3.25, "min": 1, "max": 6,
                                                "count": 4})
        self.assertIs(out["estimated_tokens"], True)

    def test_a_runtime_without_data_contributes_nothing(self) -> None:
        alpha = _module(dashboard=lambda window: _dashboard(10, [{"session_id": "a1", "total_tokens": 5}], 2))
        empty = _module(dashboard=lambda window: {**_dashboard(0, [], 0), "total_cost": 0.0,
                                                  "total_tokens": {"input": 0, "output": 0}})
        alone = alpha.dashboard(window={})
        out = self.merged(empty=empty, alpha=alpha).dashboard(window={})
        self.assertEqual(out, {**alone, "by_session": [{"session_id": "a1", "total_tokens": 5, "runtime": "alpha"}]})

    def test_no_data_anywhere_answers_the_first_runtimes_empty_shape(self) -> None:
        empty = {**_dashboard(0, [], 0), "total_cost": 0.0, "total_tokens": {"input": 0, "output": 0}}
        first = _module(dashboard=lambda window: {**empty, "source": "first"})
        other = _module(dashboard=lambda window: {**empty, "source": "other"})
        self.assertEqual(self.merged(first=first, other=other).dashboard(window={})["source"], "first")

    def test_a_runtime_that_raises_is_left_out(self) -> None:
        def boom(**_kwargs):
            raise RuntimeError("corrupt db")
        alpha = _module(dashboard=lambda window: _dashboard(10, [{"session_id": "a1", "total_tokens": 5}], 2))
        with self.assertLogs(usage_loader.logger, "WARNING"):
            out = self.merged(broken=_module(dashboard=boom), alpha=alpha).dashboard(window={})
        self.assertEqual(out["by_session"], [{"session_id": "a1", "total_tokens": 5, "runtime": "alpha"}])
        with self.assertRaises(RuntimeError), self.assertLogs(usage_loader.logger, "WARNING"):
            self.merged(broken=_module(dashboard=boom)).dashboard(window={})

    def test_a_providers_own_answer_is_never_mutated(self) -> None:
        answer = _dashboard(10, [{"session_id": "a1", "total_tokens": 5}], 2)
        before = json.loads(json.dumps(answer))
        only = _module(dashboard=lambda window: answer)
        other = _module(dashboard=lambda window: _dashboard(3, [{"session_id": "b1", "total_tokens": 1}], 1))
        usage_loader.AllRuntimesUsage([("only", only)]).dashboard(window={})
        usage_loader.AllRuntimesUsage([("only", only), ("other", other)]).dashboard(window={})
        self.assertEqual(answer, before)


class DiscoveryTests(unittest.TestCase):
    def test_every_provider_loads_the_space_agent_first_and_a_broken_one_is_skipped(self) -> None:
        modules = {"alpha": _module(), "gamma": _module()}

        def load(capability, *, agent):
            if agent == "beta":
                raise ImportError("missing dependency")
            return modules.get(agent)
        with mock.patch.object(usage_loader, "list_capability_providers", return_value=["alpha", "beta", "gamma"]), \
                mock.patch.object(usage_loader, "try_load_capability", side_effect=load), \
                mock.patch.object(usage_loader, "_space_agent_name", return_value="gamma"), \
                self.assertLogs(usage_loader.logger, "WARNING") as logs:
            providers = usage_loader.usage_providers()
        self.assertEqual([name for name, _module in providers], ["gamma", "alpha"])
        self.assertIn("beta", logs.output[0])

    def test_the_real_adapters_all_load(self) -> None:
        names = [name for name, _module in usage_loader.usage_providers()]
        for expected in ("antigravity", "claude_code", "codex", "hermes", "openclaw"):
            self.assertIn(expected, names)

    def test_a_priced_openclaw_transcript_keeps_its_cost_in_the_views_and_the_sync(self) -> None:
        from datetime import datetime, timezone
        from services.cowork_agent.adapters.openclaw import usage as openclaw_usage

        with tempfile.TemporaryDirectory() as home:
            sessions = Path(home) / "main" / "sessions"
            sessions.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            message = {"role": "assistant", "provider": "p", "model": "m", "content": [],
                       "usage": {"input": 10, "output": 5, "cost": {"total": 0.0123}}}
            (sessions / "s1.jsonl").write_text(json.dumps({"type": "message", "timestamp": now, "message": message}))
            with mock.patch.dict("os.environ", {"OPENCLAW_AGENTS_DIR": home}):
                merged = usage_loader.AllRuntimesUsage([("openclaw", openclaw_usage)])
                self.assertEqual(merged.dashboard(window={"days": 7, "tz": "utc"})["total_cost"], 0.0123)
                synced = merged.sync_payload()
        [day] = [d for d in synced if not d.startswith("__")]
        self.assertEqual(synced[day]["total_cost"], 0.0123)


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(usage_routes.router)
        self.client = TestClient(app)

    def providers(self, *items):
        return mock.patch.object(usage_loader, "usage_providers", return_value=list(items))

    def test_api_usage_answers_for_every_runtime(self) -> None:
        alpha = _module(dashboard=lambda window: _dashboard(10, [{"session_id": "a1", "total_tokens": 5}], 2))
        beta = _module(dashboard=lambda window: _dashboard(30, [{"session_id": "b1", "total_tokens": 9}], 4))
        with self.providers(("alpha", alpha), ("beta", beta)):
            body = self.client.get("/api/usage").json()
        self.assertEqual(body["total_tokens"]["input"], 40)
        self.assertEqual([(s["session_id"], s["runtime"]) for s in body["by_session"]],
                         [("b1", "beta"), ("a1", "alpha")])

    def test_the_other_views_answer_the_space_agent(self) -> None:
        alpha = _module(list_sessions=lambda: {"count": 1, "sessions": [{"sessionId": "a1"}]})
        with mock.patch.object(usage_routes, "load_usage_module", return_value=alpha):
            body = self.client.get("/api/usage/sessions").json()
        self.assertEqual(body, {"count": 1, "sessions": [{"sessionId": "a1"}]})

    def test_no_usage_module_anywhere_is_501(self) -> None:
        with self.providers():
            self.assertEqual(self.client.get("/api/usage").status_code, 501)

    def test_an_unknown_session_is_404(self) -> None:
        none = _module(get_session=lambda sid, window=None: None)
        with mock.patch.object(usage_routes, "load_usage_module", return_value=none):
            self.assertEqual(self.client.get("/api/usage/sessions/zzz").status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# Daily sync
# ─────────────────────────────────────────────────────────────────────────────


def _day(date: str, tokens: int, cost: float, model: str, tool: str) -> dict:
    return {
        "report_date": date, "total_input_tokens": tokens, "total_output_tokens": 1,
        "total_cache_read_tokens": 0, "total_cache_write_tokens": 0, "total_tokens": tokens + 1,
        "total_cost": cost, "input_cost": cost, "output_cost": 0.0, "cache_read_cost": 0.0,
        "cache_write_cost": 0.0, "total_messages": 2, "total_sessions": 0, "total_tool_calls": 1,
        "model_usage": [{"provider": "p", "model": model, "calls": 1, "tokens": tokens + 1, "cost": cost}],
        "tool_usage": [{"name": tool, "count": 1}],
    }


class _SyncRuntime:
    """A runtime's usage module: ``days`` on disk, filtered by ``since_date`` as the real ones do."""

    def __init__(self, days: dict, *, fail: bool = False, parse_errors: list | None = None) -> None:
        self.days, self.fail, self.calls = days, fail, []
        self.parse_errors = parse_errors or []

    def sync_payload(self, *, since_date=None):
        self.calls.append(since_date)
        if self.fail:
            raise RuntimeError("database is locked")
        days = {d: json.loads(json.dumps(r)) for d, r in self.days.items() if since_date is None or d >= since_date}
        out = {**days, "__session_dates__": {d: 1 for d in days}}
        if self.parse_errors:
            out["__parse_errors__"] = list(self.parse_errors)
        return out


class SyncTests(unittest.TestCase):
    """The sync's one watermark and flow, with every runtime's days added up into one record per day."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_file = Path(tmp.name) / "usage" / "alpha.json"
        self.sent: list = []

        async def report(records):
            self.sent.append(records)
            return SwarmResult(ok=True, status=200, data={"upserted": len(records)})

        async def probe_key():
            return SwarmResult(ok=True, status=200, data={"upserted": 0})
        for patcher in (
            mock.patch.object(usage_sync, "_sync_state_file", return_value=str(self.state_file)),
            mock.patch.object(usage_sync.swarm_usage, "report", side_effect=report),
            mock.patch.object(usage_sync.swarm_usage, "probe_key", side_effect=probe_key, create=True),
            mock.patch.dict(usage_loader._broken, clear=True),
            mock.patch("routers.auth.auth.get_auth_token", return_value="key"),
            mock.patch("builtins.print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_sync(self, **runtimes) -> list:
        before = len(self.sent)
        with mock.patch.object(usage_loader, "usage_providers", return_value=list(runtimes.items())):
            asyncio.run(usage_sync._run_sync())
        return self.sent[-1] if len(self.sent) > before else []

    def watermark(self) -> str | None:
        return json.loads(self.state_file.read_text()).get("last_synced_date") if self.state_file.exists() else None

    def set_watermark(self, mark: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"last_synced_date": mark}))

    def test_two_runtimes_post_one_merged_record_per_day(self) -> None:
        alpha = _SyncRuntime({"2025-01-01": _day("2025-01-01", 10, 0.1, "m1", "Read")})
        beta = _SyncRuntime({"2025-01-01": _day("2025-01-01", 20, 0.2, "m1", "Bash"),
                             "2025-01-02": _day("2025-01-02", 5, 0.0, "m2", "Read")})
        records = self.run_sync(alpha=alpha, beta=beta)
        self.assertEqual([r["report_date"] for r in records], ["2025-01-01", "2025-01-02"])
        first = records[0]
        self.assertEqual(set(first), set(usage_sync._empty_record("w", None, None, "n")))
        self.assertEqual((first["total_input_tokens"], first["total_tokens"], first["total_messages"]), (30, 32, 4))
        self.assertEqual((first["total_cost"], first["total_sessions"], first["total_tool_calls"]), (0.3, 2, 2))
        [model] = first["model_usage"]
        self.assertEqual((model["model"], model["calls"], model["tokens"]), ("m1", 2, 32))
        self.assertAlmostEqual(model["cost"], 0.3)
        self.assertEqual(sorted(t["name"] for t in first["tool_usage"]), ["Bash", "Read"])
        self.assertIsNone(first["note"])
        self.assertEqual(records[1]["total_tokens"], 6)

    def test_one_runtime_sends_exactly_its_own_records(self) -> None:
        day = _day("2025-01-01", 10, 0.1, "m1", "Read")
        records = self.run_sync(alpha=_SyncRuntime({"2025-01-01": day}))
        self.assertEqual(records, [{**day, "total_sessions": 1, "workspace_id": "unknown", "workspace_name": None,
                                    "project_id": None, "note": None}])

    def test_every_runtime_is_read_from_the_one_watermark_which_then_advances(self) -> None:
        self.set_watermark("2025-01-20")
        alpha = _SyncRuntime({"2025-01-10": _day("2025-01-10", 1, 0.0, "m", "t"),
                              "2025-01-21": _day("2025-01-21", 4, 0.0, "m", "t")})
        beta = _SyncRuntime({"2025-01-20": _day("2025-01-20", 8, 0.0, "m", "t")})
        records = self.run_sync(alpha=alpha, beta=beta)
        self.assertEqual((alpha.calls, beta.calls), (["2025-01-20"], ["2025-01-20"]))
        self.assertEqual([(r["report_date"], r["total_input_tokens"]) for r in records],
                         [("2025-01-20", 8), ("2025-01-21", 4)])
        self.assertEqual(self.watermark(), "2025-01-21")

    def test_a_runtime_that_fails_to_read_posts_the_placeholder_and_keeps_the_watermark(self) -> None:
        self.set_watermark("2025-01-20")
        alpha = _SyncRuntime({"2025-01-21": _day("2025-01-21", 4, 0.0, "m", "t")})
        [record] = self.run_sync(alpha=alpha, beta=_SyncRuntime({}, fail=True))
        self.assertEqual((record["total_tokens"], record["total_cost"]), (0, 0.0))
        self.assertEqual(record["note"], "aggregation failed in agent usage module: database is locked")
        self.assertEqual(self.watermark(), "2025-01-20")

    def test_a_module_that_doesnt_load_posts_the_placeholder(self) -> None:
        alpha = _SyncRuntime({"2025-01-01": _day("2025-01-01", 7, 0.0, "m", "t")})

        def load(capability, *, agent):
            if agent == "broken":
                raise ImportError("missing dependency")
            return alpha
        with mock.patch.object(usage_loader, "list_capability_providers", return_value=["alpha", "broken"]), \
                mock.patch.object(usage_loader, "try_load_capability", side_effect=load), \
                mock.patch.object(usage_loader, "_space_agent_name", return_value="alpha"), \
                self.assertLogs(usage_loader.logger, "WARNING"):
            asyncio.run(usage_sync._run_sync())
        [[record]] = self.sent
        self.assertEqual(record["total_tokens"], 0)
        self.assertIn("broken's usage module doesn't load", record["note"])

    def test_inputs_that_all_fail_to_parse_post_the_placeholder(self) -> None:
        [record] = self.run_sync(alpha=_SyncRuntime({}, parse_errors=["a.jsonl: bad"]))
        self.assertEqual(record["note"], "failed to parse all input(s): alpha: a.jsonl: bad")

    def test_a_partial_parse_is_named_in_the_note(self) -> None:
        alpha = _SyncRuntime({"2025-01-01": _day("2025-01-01", 1, 0.0, "m", "t")}, parse_errors=["a.jsonl: bad"])
        [record] = self.run_sync(alpha=alpha)
        self.assertEqual(record["note"], "partial: skipped 1 unparseable input(s): alpha: a.jsonl: bad")

    def test_nothing_new_posts_the_placeholder_and_keeps_the_watermark(self) -> None:
        self.set_watermark("2025-02-01")
        records = self.run_sync(alpha=_SyncRuntime({"2025-01-01": _day("2025-01-01", 1, 0.0, "m", "t")}))
        self.assertEqual(records[0]["note"], "no new entries since watermark 2025-02-01")
        self.assertEqual(self.watermark(), "2025-02-01")


if __name__ == "__main__":
    unittest.main()
