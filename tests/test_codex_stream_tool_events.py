"""Codex wire events → UI tool chips.

``codex exec --json`` emits no token deltas: an ``agent_message`` arrives whole,
and a tool call runs between messages with nothing in between. Turning
``item.started`` / ``item.completed`` into tool-call / tool-result events is what
keeps the UI showing activity during that gap.

Field names are taken from a live authenticated run: ``command_execution``
carries ``id``, ``command``, ``aggregated_output``, ``exit_code`` and
``status`` (``in_progress`` → ``completed``).
"""
from __future__ import annotations

import json
import unittest

from services.cowork_agent.adapters.codex.streaming import parse_stream_line


def wire(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


def started(**item) -> dict | None:
    return parse_stream_line(wire({"type": "item.started", "item": item}))


def completed(**item) -> dict | None:
    return parse_stream_line(wire({"type": "item.completed", "item": item}))


class ToolChipTests(unittest.TestCase):
    def test_a_started_command_opens_a_chip(self) -> None:
        out = started(
            id="item_1", type="command_execution",
            command='/bin/bash -lc "ls -la"',
            aggregated_output="", exit_code=None, status="in_progress",
        )
        self.assertEqual(out, {
            "type": "tool-call", "tool": "shell", "call_id": "item_1",
            "arguments": {"command": '/bin/bash -lc "ls -la"'},
        })

    def test_a_completed_command_fills_the_chip(self) -> None:
        out = completed(
            id="item_1", type="command_execution",
            command='/bin/bash -lc "ls -la"',
            aggregated_output="total 4\nREADME.md", exit_code=0, status="completed",
        )
        self.assertEqual(out, {
            "type": "tool-result", "tool": "shell", "call_id": "item_1",
            "output": "total 4\nREADME.md",
        })

    def test_a_nonzero_exit_is_an_error_chip(self) -> None:
        out = completed(
            id="item_2", type="command_execution", command="false",
            aggregated_output="boom", exit_code=1, status="completed",
        )
        self.assertEqual(out["type"], "tool-error")
        self.assertEqual(out["call_id"], "item_2")

    def test_a_failed_status_is_an_error_chip_even_without_an_exit_code(self) -> None:
        out = completed(
            id="item_3", type="command_execution", command="x",
            aggregated_output="", exit_code=None, status="failed",
        )
        self.assertEqual(out["type"], "tool-error")

    def test_huge_output_is_capped_so_one_command_cannot_stall_the_stream(self) -> None:
        out = completed(
            id="item_4", type="command_execution", command="cat big.log",
            aggregated_output="x" * 250_000, exit_code=0, status="completed",
        )
        self.assertLess(len(out["output"]), 250_000)
        self.assertTrue(out["output"].startswith("x" * 1000))
        self.assertIn("truncated", out["output"])

    def test_the_call_id_correlates_start_and_result(self) -> None:
        opened = started(id="item_9", type="command_execution", command="pwd")
        done = completed(
            id="item_9", type="command_execution", command="pwd",
            aggregated_output="/tmp", exit_code=0, status="completed",
        )
        self.assertEqual(opened["call_id"], done["call_id"])

    def test_an_item_without_an_id_is_skipped(self) -> None:
        """Nothing could correlate the result, so no half-open chip is made."""
        self.assertIsNone(started(type="command_execution", command="pwd"))

    def test_item_updated_adds_nothing(self) -> None:
        out = parse_stream_line(wire({
            "type": "item.updated",
            "item": {"id": "item_1", "type": "command_execution", "command": "pwd"},
        }))
        self.assertIsNone(out)


class UnchangedBehaviourTests(unittest.TestCase):
    """The tool change must not disturb the rest of the wire contract."""

    def test_assistant_text_is_still_a_token(self) -> None:
        out = parse_stream_line(wire({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "hello"},
        }))
        self.assertEqual(out, {"type": "token", "token": "hello"})

    def test_reasoning_is_still_a_progress_label_and_never_text(self) -> None:
        out = parse_stream_line(wire({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "reasoning"},
        }))
        self.assertEqual(out, {"type": "model-loading", "label": "thinking"})

    def test_thread_started_still_carries_the_native_session_id(self) -> None:
        out = parse_stream_line(wire({"type": "thread.started", "thread_id": "abc"}))
        self.assertEqual(out, {"type": "session_id", "session_id": "abc"})

    def test_turn_completed_still_carries_usage(self) -> None:
        out = parse_stream_line(wire({
            "type": "turn.completed", "usage": {"input_tokens": 5},
        }))
        self.assertEqual(out, {"type": "result", "usage": {"input_tokens": 5}})

    def test_turn_failed_is_still_an_error(self) -> None:
        out = parse_stream_line(wire({
            "type": "turn.failed", "error": {"message": "401 Unauthorized"},
        }))
        self.assertEqual(out, {"type": "error", "error": "401 Unauthorized"})

    def test_an_unknown_item_type_is_still_skipped(self) -> None:
        self.assertIsNone(started(id="item_1", type="not_a_thing_yet"))


if __name__ == "__main__":
    unittest.main()
