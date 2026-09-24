"""One stream vocabulary for every chat adapter.

``services/cowork_agent/engine/stream_events.py`` names the events an
adapter's ``stream()`` may yield and the activity labels it may attach. The
chat route only understands these; a parser that invents a type or a label
is a silent gap in the UI, so the vocabulary is pinned here and each
project-tied parser is replayed against a wire capture to show it speaks
nothing else.
"""
from __future__ import annotations

import json
import unittest

from services.cowork_agent.engine import stream_events as se


def line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


class VocabularyTests(unittest.TestCase):
    def test_the_five_event_types_and_their_constructors(self) -> None:
        self.assertEqual(
            se.EVENT_TYPES, frozenset({"token", "model-loading", "session_id", "result", "error"})
        )
        self.assertEqual(se.token("hi"), {"type": "token", "token": "hi"})
        self.assertEqual(se.token("h", partial=True), {"type": "token", "token": "h", "partial": True})
        self.assertEqual(se.activity(se.THINKING), {"type": "model-loading", "label": "thinking"})
        self.assertEqual(se.running("Bash"), {"type": "model-loading", "label": "running Bash"})
        self.assertEqual(se.session_id("s1"), {"type": "session_id", "session_id": "s1"})
        self.assertEqual(se.error("boom"), {"type": "error", "error": "boom"})
        self.assertEqual(se.result(usage={"x": 1})["type"], "result")

    def test_labels_are_the_shared_set_or_a_named_tool(self) -> None:
        for label in (se.THINKING, se.RUNNING_COMMAND, se.EDITING_FILES, se.CALLING_TOOL):
            self.assertTrue(se.is_known_label(label), label)
        self.assertTrue(se.is_known_label("running Bash"))
        self.assertFalse(se.is_known_label("loading model weights"))
        self.assertFalse(se.is_known_label(""))

    def test_a_tool_name_is_shown_without_its_mcp_routing_prefix(self) -> None:
        self.assertEqual(se.running("mcp__cowork__COMPOSIO_SEARCH_TOOLS")["label"],
                         "running COMPOSIO_SEARCH_TOOLS")
        self.assertEqual(se.running("")["label"], se.CALLING_TOOL)


class CodexParserSpeaksTheVocabulary(unittest.TestCase):
    # The wire shapes the codex parser documents (thread.started, item.*,
    # turn.completed, turn.failed), one of each.
    WIRE = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "reasoning"}},
        {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "file_change"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
        {"type": "turn.failed", "error": {"message": "nope"}},
    ]

    def test_every_event_and_label_is_known(self) -> None:
        from services.cowork_agent.adapters.codex.streaming import parse_stream_line

        events = [parse_stream_line(line(o)) for o in self.WIRE]
        kinds = [(e["type"], e.get("label")) for e in events if e]
        self.assertEqual(kinds, [
            ("session_id", None),
            ("model-loading", "thinking"),
            ("model-loading", "running command"),
            ("model-loading", "editing files"),
            ("model-loading", "calling tool"),
            ("token", None),
            ("result", None),
            ("error", None),
        ])
        for e in events:
            if e is None:
                continue
            self.assertIn(e["type"], se.EVENT_TYPES)
            if e["type"] == se.ACTIVITY:
                self.assertTrue(se.is_known_label(e["label"]), e)
        # a command's text is the agent's input and never leaves as a label
        self.assertNotIn("ls", json.dumps([e for e in events if e]))


if __name__ == "__main__":
    unittest.main()
