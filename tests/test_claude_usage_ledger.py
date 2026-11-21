"""A turn's tokens are every API call it made (the 11-19 usage audit): the
root's calls as the stream counts them, its sub-agents' added and kept apart,
and a cut turn's calls instead of no usage at all."""

from __future__ import annotations

import unittest

from services.cowork_agent.adapters.claude_code.runtime.wire import Wire
from services.cowork_agent.adapters.contract import RunFinished, TurnFinished, Usage


def start(mid: str, inp: int, read: int, write: int, *, parent: str | None = None) -> dict:
    return {"type": "stream_event", "parent_tool_use_id": parent,
            "event": {"type": "message_start", "message": {"id": mid, "usage": {
                "input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
                "output_tokens": 1}}}}


def delta(out: int) -> dict:
    return {"type": "stream_event", "parent_tool_use_id": None,
            "event": {"type": "message_delta", "usage": {"output_tokens": out}}}


def assistant(mid: str, inp: int, read: int, write: int, out: int, *, parent: str | None = None) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"id": mid, "content": [], "usage": {
        "input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
        "output_tokens": out}}}


def result(inp: int, read: int, write: int, out: int, **extra) -> dict:
    return {"type": "result", "subtype": "success", "usage": {
        "input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
        "output_tokens": out}, **extra}


def feed(wire: Wire, lines: list[dict]) -> list:
    outs = []
    for line in lines:
        outs += wire.line(line)
    return outs


def usage_of(outs: list):
    found = [o.fields["usage"] for o in outs if o.cls is Usage]
    return found[-1] if found else None


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        self.wire.start_turn("person")

    def test_the_root_matches_the_result_and_each_call_counts_once(self) -> None:
        outs = feed(self.wire, [start("m1", 3, 100, 10), assistant("m1", 3, 100, 10, 1), delta(40),
                                start("m2", 2, 110, 5), assistant("m2", 2, 110, 5, 1), delta(60),
                                result(5, 210, 15, 100)])
        usage = usage_of(outs)
        self.assertEqual((usage.input, usage.cache_read, usage.cache_write, usage.output), (5, 210, 15, 100))
        self.assertIsNone(usage.subagents)

    def test_a_call_the_result_leaves_out_still_counts(self) -> None:
        """A model fallback: the first try was sent, and the result counts only the second."""
        outs = feed(self.wire, [start("m1", 3, 100, 10), start("m2", 3, 100, 10), delta(50),
                                result(3, 100, 10, 50)])
        usage = usage_of(outs)
        self.assertEqual((usage.input, usage.cache_read, usage.cache_write), (6, 200, 20))

    def test_sub_agents_are_added_and_kept_apart(self) -> None:
        self.wire._runs_by_call["toolu_1"] = "task_1"
        outs = feed(self.wire, [
            start("m1", 3, 100, 10), delta(20),
            assistant("s1", 1, 50, 5, 4, parent="toolu_1"), assistant("s1", 1, 50, 5, 4, parent="toolu_1"),
            assistant("s2", 1, 60, 2, 3, parent="toolu_1"),
            {"type": "system", "subtype": "task_notification", "task_id": "task_1", "status": "completed",
             "usage": {"total_tokens": 1 + 60 + 2 + 30}},
            result(3, 100, 10, 20)])
        usage = usage_of(outs)
        self.assertEqual((usage.subagents.input, usage.subagents.cache_read, usage.subagents.cache_write),
                         (2, 110, 7))
        # the last call's output is made exact by the run's final context; the first stays as seen
        self.assertEqual(usage.subagents.output, 4 + 30)
        self.assertEqual((usage.input, usage.cache_read, usage.output), (5, 210, 54))
        run = [o.fields for o in outs if o.cls is RunFinished][0]
        self.assertEqual(run["usage"].cache_read, 110)

    def test_a_turn_claude_reports_as_zero_keeps_what_it_spent(self) -> None:
        outs = feed(self.wire, [start("m1", 3, 100, 10), delta(20),
                                result(0, 0, 0, 0, terminal_reason="aborted_streaming", subtype="error_during_execution")])
        usage = usage_of(outs)
        self.assertIsNotNone(usage)
        self.assertEqual((usage.cache_read, usage.output), (100, 20))
        self.assertIsNone(usage.cost_usd)

    def test_a_turn_cut_by_the_process_ending_keeps_what_it_spent(self) -> None:
        feed(self.wire, [start("m1", 3, 100, 10), delta(20)])
        end = [o.fields for o in self.wire.close_turn(status="interrupted", error=None) if o.cls is TurnFinished][0]
        self.assertEqual((end["usage"].cache_read, end["usage"].output), (100, 20))

    def test_a_turn_with_no_call_has_no_usage(self) -> None:
        end = [o.fields for o in self.wire.close_turn(status="interrupted", error=None) if o.cls is TurnFinished][0]
        self.assertIsNone(end["usage"])

    def test_the_next_turn_starts_from_nothing(self) -> None:
        feed(self.wire, [start("m1", 3, 100, 10), delta(20), result(3, 100, 10, 20)])
        self.wire.start_turn("person")
        usage = usage_of(feed(self.wire, [start("m2", 1, 7, 1), delta(2), result(1, 7, 1, 2)]))
        self.assertEqual((usage.input, usage.cache_read, usage.output), (1, 7, 2))


if __name__ == "__main__":
    unittest.main()
