"""Custody adapter: ledger discovery, event mapping, and telemetry shape."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.custody import session_telemetry, visualizer_source
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
)


def _entry(seq: int, action: str, **overrides: object) -> dict:
    """One ledger entry with every field custody always writes."""
    base: dict = {
        "seq": seq,
        "ts": "2026-09-20T12:00:%02dZ" % seq,
        "actor": "harness",
        "action": action,
        "target": "CUS-1",
        "detail": {"free": "text that must never be emitted"},
        "files_touched": [],
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "verdict": "",
        "prev_hash": "p" * 64,
        "entry_hash": "%02x" % seq * 32,
    }
    base.update(overrides)
    return base


def _write_ledger(repo: Path, entries: list[dict]) -> Path:
    ledger = repo / ".custody" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    return ledger


def _run_entries() -> list[dict]:
    return [
        _entry(0, "run.started", entry_hash="a" * 64),
        _entry(1, "contract.declared", actor="remediator",
               tokens_in=120, tokens_out=40, cost_usd=0.0031),
        _entry(2, "files.written", actor="remediator", files_touched=["app.py"]),
        _entry(3, "case.ruled", actor="auditor", verdict="PROVEN"),
    ]


class SourceTests(unittest.TestCase):
    """The watcher feed: discovery and PII-safe event mapping."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / "demo"
        self.ledger = _write_ledger(self.repo, _run_entries())
        self.patches = [
            mock.patch.object(visualizer_source, "xo_projects_root", return_value=self.root),
            mock.patch.object(visualizer_source, "list_project_ids", return_value=["demo"]),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)
        offsets = jsonl_tail.OffsetStore(self.root / "offsets.json")
        self.source = visualizer_source.Source(offsets=offsets)

    def test_discovery_finds_project_ledgers(self) -> None:
        found = list(visualizer_source.iter_ledgers())
        self.assertEqual(found, [("demo", self.ledger)])

    def test_nested_repo_ledgers_are_discovered(self) -> None:
        nested = _write_ledger(self.root / "demo" / "target", _run_entries())
        found = [path for _, path in visualizer_source.iter_ledgers()]
        self.assertIn(nested, found)

    def test_events_map_without_leaking_free_text(self) -> None:
        events = list(self.source.poll_events())
        kinds = [type(event).__name__ for event in events]
        self.assertIn("SessionFirstSeen", kinds)
        self.assertIn("MessageObserved", kinds)
        self.assertIn("ToolUseObserved", kinds)
        self.assertIn("FileTouched", kinds)
        self.assertIn("UsageObserved", kinds)
        rendered = repr(events)
        self.assertNotIn("free", rendered)
        self.assertNotIn("never be emitted", rendered)

    def test_session_id_is_the_run_seal_prefix(self) -> None:
        events = list(self.source.poll_events())
        self.assertTrue(events)
        self.assertEqual({event.native_session_id for event in events}, {"a" * 12})
        self.assertEqual({event.runtime for event in events}, {"custody"})
        self.assertEqual({event.project_id for event in events}, {"demo"})

    def test_verdicts_surface_as_named_tools(self) -> None:
        tools = {
            event.tool for event in self.source.poll_events()
            if isinstance(event, ToolUseObserved)
        }
        self.assertIn("case.ruled", tools)
        self.assertIn("verdict.proven", tools)

    def test_usage_carries_token_counts(self) -> None:
        usage = [e for e in self.source.poll_events() if isinstance(e, UsageObserved)]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].input_tokens, 120)
        self.assertEqual(usage[0].output_tokens, 40)

    def test_escaping_paths_are_dropped(self) -> None:
        _write_ledger(self.repo, _run_entries() + [
            _entry(4, "files.written", files_touched=["../outside.py", "/abs.py", "ok.py"]),
        ])
        touched = {
            e.relative_path for e in self.source.poll_events()
            if isinstance(e, FileTouched)
        }
        self.assertIn("ok.py", touched)
        self.assertNotIn("../outside.py", touched)
        self.assertNotIn("/abs.py", touched)

    def test_second_poll_emits_nothing_new(self) -> None:
        list(self.source.poll_events())
        self.assertEqual(list(self.source.poll_events()), [])

    def test_presence_is_an_empty_snapshot(self) -> None:
        self.assertEqual(self.source.poll_presence(), [])


class TelemetryTests(unittest.TestCase):
    """The cost channel: one session per run, JSON-safe payload."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        _write_ledger(self.root / "demo", _run_entries() + [
            _entry(4, "run.started", entry_hash="b" * 64),
            _entry(5, "case.ruled", actor="auditor", verdict="REJECTED",
                   tokens_in=10, tokens_out=5, cost_usd=0.001),
        ])
        mock.patch.object(
            visualizer_source, "xo_projects_root", return_value=self.root
        ).start()
        mock.patch.object(
            visualizer_source, "list_project_ids", return_value=["demo"]
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_runs_become_sessions_with_cost(self) -> None:
        payload = session_telemetry.collect_session_telemetry()
        self.assertEqual(payload["totals"]["sessions"], 2)
        self.assertEqual(payload["totals"]["cost_usd"], 0.0041)
        keys = {row["key"] for row in payload["sessions"]}
        self.assertEqual(keys, {"custody:" + "a" * 12, "custody:" + "b" * 12})
        for row in payload["sessions"]:
            self.assertTrue(row["cost_known"])
            self.assertEqual(row["agent"], "custody")

    def test_payload_is_strict_json(self) -> None:
        payload = session_telemetry.collect_session_telemetry()
        json.dumps(payload, allow_nan=False)

    def test_missing_ledger_raises_a_reason(self) -> None:
        mock.patch.object(
            visualizer_source, "list_project_ids", return_value=[]
        ).start()
        with self.assertRaises(Exception) as caught:
            session_telemetry.collect_session_telemetry()
        self.assertIn("no custody ledger", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
