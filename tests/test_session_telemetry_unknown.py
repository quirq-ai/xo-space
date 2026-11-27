"""Session telemetry says when a figure is unknown: the validator passes a
provider's ``null`` through (an absent figure still reads 0, as before), the
merge sums known figures and flags the rest, and Cursor, which writes no token
counts locally, reports null tokens rather than a guess."""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.cursor import session_telemetry as cursor_telemetry
from services.cowork_agent.visualizer import session_telemetry as merged


def _payload(source_id: str, *, tokens, cost, tokens_complete=True) -> dict:
    row = {"id": "s", "key": f"{source_id}:s", "agent": source_id, "started_at": "2026-11-27T09:00:00Z",
           "fresh": None if tokens is None else tokens, "output": None if tokens is None else 0,
           "cache_read": None, "cache_write": None, "unclassified": None if tokens is None else 0,
           "tokens": tokens, "cost": cost}
    return {
        "source": {"id": source_id, "label": source_id, "cost_status": "reported" if cost is not None else "unavailable"},
        "totals": {"sessions": 1, "tokens": tokens, "cost_usd": cost, "tokens_complete": tokens_complete},
        "sessions": [row],
        "daily_models": [{"day": "2026-11-27", "agent": source_id, "model": "m", "tokens": tokens, "cost": cost}],
    }


class ValidatorTests(unittest.TestCase):
    def test_an_explicit_null_stays_null_and_an_absent_figure_reads_zero(self) -> None:
        payload = _payload("a", tokens=None, cost=None)
        payload["sessions"].append({"id": "old", "started_at": "2026-11-26T09:00:00Z", "fresh": 5})
        checked = merged._validate_contribution("a", None, payload)
        unknown, legacy = checked["sessions"]
        self.assertEqual((unknown["tokens"], unknown["fresh"], unknown["cost"], unknown["tokens_known"]),
                         (None, None, None, False))
        self.assertEqual(unknown["own_tokens"], None)
        self.assertEqual((legacy["tokens"], legacy["output"], legacy["cost"]), (5, 0, 0))
        self.assertEqual((checked["daily_models"][0]["tokens"], checked["daily_models"][0]["cost"]), (None, None))

    def test_a_negative_figure_is_still_rejected(self) -> None:
        payload = _payload("a", tokens=-1, cost=None)
        with self.assertRaises(TypeError):
            merged._validate_contribution("a", None, payload)

    def test_the_merge_sums_known_figures_and_says_what_is_missing(self) -> None:
        modules = {"known": _payload("known", tokens=100, cost=1.5),
                   "blind": _payload("blind", tokens=None, cost=None, tokens_complete=False)}

        def load(_capability, *, agent):
            return types.SimpleNamespace(collect_session_telemetry=lambda: modules[agent])
        with mock.patch.object(merged, "list_capability_providers", return_value=list(modules)), \
                mock.patch.object(merged, "try_load_capability", side_effect=load), \
                mock.patch.object(merged, "disabled_source_ids", return_value=set()):
            result = merged.build_session_telemetry()
        totals = result["totals"]
        self.assertEqual((totals["tokens"], totals["cost_usd"]), (100, 1.5))
        self.assertEqual(totals["tokens_by_agent"], {"known": 100, "blind": None})
        self.assertEqual(totals["cost_by_agent"], {"known": 1.5, "blind": None})
        self.assertEqual((totals["tokens_complete"], totals["cost_complete"]), (False, False))
        blind = next(s for s in result["meta"]["sources"] if s["id"] == "blind")
        self.assertIsNone(blind["token_count"])

    def test_a_source_that_says_its_cost_is_partial_makes_the_merged_cost_partial(self) -> None:
        payload = _payload("known", tokens=100, cost=1.5)
        payload["totals"]["cost_complete"] = False
        with mock.patch.object(merged, "list_capability_providers", return_value=["known"]), \
                mock.patch.object(merged, "try_load_capability",
                                  return_value=types.SimpleNamespace(collect_session_telemetry=lambda: payload)), \
                mock.patch.object(merged, "disabled_source_ids", return_value=set()):
            totals = merged.build_session_telemetry()["totals"]
        self.assertEqual((totals["tokens_complete"], totals["cost_complete"]), (True, False))


class CursorTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        transcripts = home / "projects" / "work-app" / "agent-transcripts"
        transcripts.mkdir(parents=True)
        long_prompt = "secret words " * 500
        (transcripts / "s1.jsonl").write_text("".join(json.dumps(row) + "\n" for row in (
            {"role": "user", "message": {"content": [{"type": "text", "text":
                f"<timestamp>2026-11-27T09:00:00Z</timestamp> {long_prompt}"}]}},
            {"role": "assistant", "message": {"content": [{"type": "text", "text": long_prompt}]}},
        )))
        for patcher in (mock.patch.dict(os.environ, {"CURSOR_HOME": str(home)}),
                        mock.patch.object(cursor_telemetry, "_user_data_roots", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_without_cursors_own_counters_the_tokens_are_unknown_not_guessed(self) -> None:
        payload = cursor_telemetry.collect_session_telemetry()
        [row] = payload["sessions"]
        self.assertEqual(row["turns"], 1)
        for key in ("tokens", "fresh", "output", "cache_read", "cache_write", "unclassified", "cost"):
            self.assertIsNone(row[key], key)
        self.assertEqual((row["tokens_known"], row["breakdown_known"], row["cost_known"]), (False, False, False))
        self.assertEqual((payload["totals"]["tokens"], payload["totals"]["tokens_complete"]), (None, False))
        self.assertIsNone(payload["totals"]["cost_usd"])
        self.assertTrue(all(r["tokens"] is None for r in payload["daily_models"] + payload["daily_sessions"]))
        self.assertNotIn("estimated", json.dumps(payload))
        self.assertNotIn("secret", json.dumps(payload))
        checked = merged._validate_contribution("cursor", cursor_telemetry, payload)
        self.assertIsNone(checked["sessions"][0]["tokens"])


if __name__ == "__main__":
    unittest.main()
