"""The ``audr.jsonl`` sink: one OpenAudr v1.0.0 record per metered event.

Every line it writes must satisfy the vendored AUDR schema
(``visualizer/schema/audr.schema.json``), which is the spec's own.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.cowork_agent.visualizer.ingest.events import (
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
)
from services.cowork_agent.visualizer.sinks import audr as audr_sink

SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "services" / "cowork_agent" / "visualizer" / "schema" / "audr.schema.json"
)
SESSION = "11111111-1111-4111-8111-111111111111"
TS = "2026-10-08T12:00:00.123456Z"

#: The spec's own example record (spec/examples/record.json), verbatim.
SPEC_EXAMPLE = {
    "spec_version": "1.0.0",
    "record_id": "01K4N8D2J4P7Q9R3S6T8V1W5XY",
    "emitter": {"component": "router", "name": "@audr/openrouter", "version": "0.5.1"},
    "timing": {"event_time": "2026-09-08T12:00:00.000Z"},
    "resource": {
        "provider": "anthropic",
        "type": "model",
        "name": "claude-sonnet-4-20250514",
        "operation": "generation",
        "modality": "text",
    },
    "run": {"run_id": "01K4N8B0M2C5F7H9J1L3N6P8QR", "span_id": "model-call-1"},
    "attribution": {"environment": "production", "account_id": "account-42"},
    "usage": {"llm": {"input_tokens": 1200, "output_tokens": 300}},
}


def _usage(**over) -> UsageObserved:
    fields = dict(
        ts=TS, native_session_id=SESSION, runtime="some_runtime", project_id="demo",
        input_tokens=1200, output_tokens=300, cache_read_input_tokens=50,
        cache_creation_input_tokens=7, model="claude-sonnet-5", latency_ms=812,
    )
    fields.update(over)
    return UsageObserved(**fields)


def _tool(name: str = "Bash", **over) -> ToolUseObserved:
    fields = dict(ts=TS, native_session_id=SESSION, runtime="some_runtime",
                  project_id="demo", tool=name)
    fields.update(over)
    return ToolUseObserved(**fields)


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def written(self) -> list[dict]:
        path = self.root / "audr.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ModelRecordTests(_Sandbox):
    def test_a_usage_event_becomes_one_model_record(self) -> None:
        audr_sink.apply(self.root, [_usage()])
        [record] = self.written()

        self.assertEqual(record["spec_version"], "1.0.0")
        self.assertEqual(
            record["emitter"], {"component": "harness", "name": "xo-space", "version": "1.0.0"},
        )
        self.assertEqual(record["resource"], {
            "provider": "anthropic", "type": "model", "name": "claude-sonnet-5",
            "operation": "generation", "modality": "text",
        })
        self.assertEqual(record["usage"], {"llm": {
            "input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 50,
            "cache_write_tokens": 7, "requests": 1,
        }})
        self.assertEqual(record["run"]["run_id"], SESSION)
        self.assertEqual(record["run"]["run_type"], "agent_run")
        self.assertEqual(record["timing"], {
            "event_time": "2026-10-08T12:00:00.123Z", "duration_ms": 812,
        })

    def test_the_provider_comes_from_the_model_name(self) -> None:
        cases = {
            "claude-opus-5-5": "anthropic",
            "gpt-5.1-codex": "openai",
            "o3-mini": "openai",
            "gemini-3-pro": "google",
            "grok-4": "xai",
            "llama-4": "some-runtime",  # unknown model: the runtime, slugged
        }
        for model, provider in cases.items():
            with self.subTest(model=model):
                [record] = audr_sink.apply(self.root, [_usage(model=model)])
                self.assertEqual(record["resource"]["provider"], provider)

    def test_a_turn_without_a_model_or_latency_still_conforms(self) -> None:
        [record] = audr_sink.apply(self.root, [_usage(model=None, latency_ms=None)])
        self.assertEqual(record["resource"]["name"], "unknown")
        self.assertNotIn("duration_ms", record["timing"])


class ToolRecordTests(_Sandbox):
    def test_a_tool_use_becomes_one_local_invocation(self) -> None:
        audr_sink.apply(self.root, [_tool("Edit")])
        [record] = self.written()

        self.assertEqual(record["resource"], {
            "provider": "self-hosted", "type": "tool", "name": "Edit",
            "operation": "tool_execution",
        })
        self.assertEqual(record["usage"], {"tool": {"type": "invocation", "call_count": 1}})
        self.assertNotIn("llm", record["usage"])

    def test_repeated_tools_get_distinct_span_ids(self) -> None:
        records = audr_sink.apply(self.root, [_tool("Bash"), _tool("Bash"), _usage()])
        spans = [r["run"]["span_id"] for r in records]
        self.assertEqual(len(spans), len(set(spans)))
        records_ids = [r["record_id"] for r in records]
        self.assertEqual(len(records_ids), len(set(records_ids)))


class EnvelopeTests(_Sandbox):
    def test_attribution_is_the_fixed_minimum(self) -> None:
        for record in audr_sink.apply(self.root, [_usage(), _tool()]):
            self.assertEqual(record["attribution"], {"environment": "development"})

    def test_events_that_meter_nothing_are_ignored(self) -> None:
        events = [
            SessionFirstSeen(ts=TS, native_session_id=SESSION, runtime="r", cwd="/x"),
            MessageObserved(ts=TS, native_session_id=SESSION, runtime="r", role="user"),
            FileTouched(ts=TS, native_session_id=SESSION, runtime="r", relative_path="a.py"),
        ]
        self.assertEqual(audr_sink.apply(self.root, events), [])
        self.assertFalse((self.root / "audr.jsonl").exists())

    def test_an_unparseable_timestamp_is_skipped_not_fatal(self) -> None:
        records = audr_sink.apply(self.root, [_usage(ts="not-a-time"), _tool()])
        self.assertEqual([r["resource"]["type"] for r in records], ["tool"])

    def test_appends_across_ticks(self) -> None:
        audr_sink.apply(self.root, [_usage()])
        audr_sink.apply(self.root, [_tool()])
        self.assertEqual(len(self.written()), 2)

    def test_rotates_past_the_threshold(self) -> None:
        audr_sink.apply(self.root, [_usage()])
        with patch.object(audr_sink, "_ROTATE_BYTES", 1):
            audr_sink.apply(self.root, [_tool()])
        self.assertEqual(len(list(self.root.glob("audr.*.jsonl"))), 1)
        self.assertEqual([r["resource"]["type"] for r in self.written()], ["tool"])


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class SchemaTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        import jsonschema

        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        )

    def test_the_vendored_schema_is_v1_0_0(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "https://openaudr.dev/spec/v1.0.0/audr.schema.json")

    def test_the_spec_example_satisfies_the_schema(self) -> None:
        self.validator.validate(SPEC_EXAMPLE)

    def test_every_written_line_satisfies_the_schema(self) -> None:
        audr_sink.apply(self.root, [
            _usage(), _usage(model=None, latency_ms=None, cache_read_input_tokens=0),
            _tool("Bash"), _tool("mcp__server__do_thing"),
        ])
        lines = self.written()
        self.assertEqual(len(lines), 4)
        for line in lines:
            with self.subTest(span=line["run"]["span_id"]):
                self.validator.validate(line)


class _FakeSource:
    name = "stub"

    def __init__(self, events: list) -> None:
        self._events = events

    def poll_events(self) -> list:
        events, self._events = self._events, []
        return events

    def poll_presence(self) -> list:
        return []


class WatcherWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.base / "projects"),
            "QUIRQ_STATE_ROOT": str(self.base / "state"),
            "QUIRQ_COMMAND_LOG": "off",
            "XO_SCHEDULER_ENABLED": "false",
        })
        env.start()
        self.addCleanup(env.stop)
        self.rt = self.base / "state" / "projects" / "demo"
        self.rt.mkdir(parents=True)

    def _watcher(self, events: list):
        from services.cowork_agent.visualizer import watcher as watcher_mod

        with patch.object(watcher_mod, "get_active_agent", return_value=SimpleNamespace(name="stub")), \
             patch.object(watcher_mod, "try_load_capability", return_value=None):
            w = watcher_mod.Watcher()
        w.sources = [_FakeSource(events)]
        return watcher_mod, w

    def test_a_tick_writes_audr_records_to_the_runtime_home(self) -> None:
        watcher_mod, w = self._watcher([_usage(), _tool()])
        with patch.object(watcher_mod, "runtime_dir_for_project", return_value=self.rt), \
             patch.object(watcher_mod.project_json, "fill_identity"):
            w.tick()

        lines = (self.rt / "audr.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [json.loads(line)["resource"]["type"] for line in lines], ["model", "tool"],
        )
        self.assertFalse([e for e in w.step_errors if "audr" in e], w.step_errors)

    def test_an_audr_failure_is_reported_without_costing_the_timeline(self) -> None:
        watcher_mod, w = self._watcher([_tool()])
        with patch.object(watcher_mod, "runtime_dir_for_project", return_value=self.rt), \
             patch.object(watcher_mod.project_json, "fill_identity"), \
             patch.object(watcher_mod.audr, "apply", side_effect=RuntimeError("boom")), \
             self.assertLogs(watcher_mod.logger, "ERROR"):
            w.tick()

        self.assertTrue(any(e.startswith("audr sink for demo") for e in w.step_errors))
        self.assertTrue((self.rt / "stats.json").is_file())


if __name__ == "__main__":
    unittest.main()
