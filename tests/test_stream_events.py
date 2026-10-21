"""One stream vocabulary for every chat adapter.

``services/cowork_agent/engine/stream_events.py`` names the events an
adapter's ``stream()`` may yield and the activity labels it may attach. The
chat route only understands these; a parser that invents a type or a label
is a silent gap in the UI, so the vocabulary is pinned here.
"""
from __future__ import annotations

import unittest

from services.cowork_agent.engine import stream_events as se


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


if __name__ == "__main__":
    unittest.main()
