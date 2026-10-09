"""The read side of ``audr.jsonl``: what the Activity page's usage panel shows."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer import audr_summary
from services.cowork_agent.visualizer.ingest.events import ToolUseObserved, UsageObserved
from services.cowork_agent.visualizer.sinks import audr as audr_sink

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"


def _usage(session=S1, ts="2026-10-08T12:00:00.000Z", model="claude-opus-5-5", **tokens):
    fields = dict(input_tokens=2, output_tokens=74, cache_read_input_tokens=21_869,
                  cache_creation_input_tokens=18_725)
    fields.update(tokens)
    return UsageObserved(ts=ts, native_session_id=session, runtime="r", project_id="p",
                         model=model, **fields)


def _tool(name="Bash", session=S1, ts="2026-10-08T12:00:01.000Z"):
    return ToolUseObserved(ts=ts, native_session_id=session, runtime="r", project_id="p", tool=name)


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        audr_summary._cache.clear()

    def lines(self) -> list[dict]:
        return [json.loads(l) for l in (self.root / "audr.jsonl").read_text().splitlines()]

    def append(self, *records: dict) -> None:
        with open(self.root / "audr.jsonl", "a", encoding="utf-8") as fp:
            for record in records:
                fp.write(json.dumps(record) + "\n")

    def test_nothing_recorded_is_zeros(self) -> None:
        summary = audr_summary.summarize(self.root)
        self.assertEqual((summary["records"], summary["total_cost"], summary["by_model"]), (0, 0.0, []))
        self.assertEqual(audr_summary.summarize(None)["records"], 0)

    def test_totals_and_breakdowns(self) -> None:
        audr_sink.apply(self.root, [
            _usage(), _tool("Bash"), _tool("Bash"), _tool("Edit"),
            _usage(session=S2, ts="2026-10-08T13:00:00.000Z", model="claude-haiku-5-5"),
            _usage(session=S2, ts="2026-10-08T13:00:01.000Z", model="mystery-model"),
        ])
        s = audr_summary.summarize(self.root)
        self.assertEqual((s["records"], s["model_turns"], s["tool_calls"]), (6, 3, 3))
        self.assertEqual((s["priced_turns"], s["unpriced_turns"]), (2, 1))
        self.assertEqual(s["tokens"]["output"], 74 * 3)
        opus = s["by_model"][0]
        self.assertEqual((opus["model"], opus["cost"], opus["unpriced_turns"]), ("claude-opus-5-5", 0.0994868, 0))
        mystery = next(m for m in s["by_model"] if m["model"] == "mystery-model")
        self.assertEqual((mystery["cost"], mystery["unpriced_turns"]), (0.0, 1))
        self.assertAlmostEqual(s["total_cost"], sum(m["cost"] for m in s["by_model"]))
        self.assertEqual(s["by_tool"], [{"tool": "Bash", "calls": 2}, {"tool": "Edit", "calls": 1}])
        self.assertEqual([x["run_id"] for x in s["by_session"]], [S2, S1], "newest session first")
        self.assertEqual(s["by_session"][1]["tool_calls"], 3)
        self.assertEqual(s["recent"][0]["name"], "mystery-model")
        self.assertIsNone(s["recent"][0]["cost"])

    def test_a_duplicate_record_counts_once(self) -> None:
        audr_sink.apply(self.root, [_usage()])
        self.append(self.lines()[0])
        self.assertEqual(audr_summary.summarize(self.root)["model_turns"], 1)

    def test_a_correction_replaces_the_record_it_names(self) -> None:
        audr_sink.apply(self.root, [_usage(model="mystery-model")])
        [original] = self.lines()
        corrected = {**original, "record_id": "01a11bf0-0000-7000-8000-000000000001",
                     "corrects": original["record_id"],
                     "cost": {"total_cost": 0.5, "currency": "USD"}}
        self.append(corrected)
        s = audr_summary.summarize(self.root)
        self.assertEqual((s["model_turns"], s["priced_turns"], s["total_cost"]), (1, 1, 0.5))

    def test_rotations_are_included_and_bad_lines_counted(self) -> None:
        audr_sink.apply(self.root, [_usage()])
        (self.root / "audr.jsonl").rename(self.root / "audr.20261008T000000Z.jsonl")
        audr_sink.apply(self.root, [_tool()])
        with open(self.root / "audr.jsonl", "a", encoding="utf-8") as fp:
            fp.write("{not json\n")
        s = audr_summary.summarize(self.root)
        self.assertEqual((s["records"], s["unreadable_lines"]), (2, 1))

    def test_an_unchanged_file_is_not_reparsed(self) -> None:
        audr_sink.apply(self.root, [_usage()])
        audr_summary.summarize(self.root)
        with patch.object(audr_summary, "_records", side_effect=AssertionError("reparsed")):
            audr_summary.summarize(self.root)
        audr_sink.apply(self.root, [_tool()])
        self.assertEqual(audr_summary.summarize(self.root)["records"], 2)


class RouteTests(unittest.TestCase):
    def test_the_route_serves_the_summary_and_404s_an_unknown_project(self) -> None:
        from fastapi.testclient import TestClient

        import server
        from routers.cowork_agent.bff import visualizer

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        audr_summary._cache.clear()
        audr_sink.apply(root, [_usage(), _tool()])

        class Scope:
            def project_exists(self):
                return True

            def read_usage_records(self):
                return audr_summary.summarize(root)

        client = TestClient(server.app)
        with patch.object(visualizer.scopes, "resolve_scope", return_value=Scope()):
            reply = client.get("/api/xo-projects/demo/usage-records")
        self.assertEqual(reply.status_code, 200)
        body = reply.json()
        self.assertEqual((body["project_id"], body["model_turns"], body["tool_calls"]), ("demo", 1, 1))
        self.assertEqual(body["total_cost"], 0.0994868)

        missing = type("Missing", (), {"project_exists": lambda self: False})()
        with patch.object(visualizer.scopes, "resolve_scope", return_value=missing):
            self.assertEqual(client.get("/api/xo-projects/nope/usage-records").status_code, 404)


if __name__ == "__main__":
    unittest.main()
