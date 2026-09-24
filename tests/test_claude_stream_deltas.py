"""The Claude Code adapter streams text live.

`--include-partial-messages` makes the CLI emit `stream_event` lines carrying
API deltas, then still repeats the finished text as a complete `assistant`
message. The parser tags delta tokens `partial`; the adapter forwards those
and skips the repeat, so text reaches the SSE stream once and early.
"""
from __future__ import annotations

import json
import unittest

from services.cowork_agent.adapters.claude_code.streaming import parse_stream_line


def line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


# Captured from `claude --print --output-format stream-json --verbose
# --include-partial-messages -p "Reply with exactly: hello world"` (CLI 2.1.263).
CAPTURE = [
    {"type": "system", "subtype": "init", "session_id": "s1"},
    {"type": "stream_event", "event": {"type": "message_start"}},
    {"type": "stream_event", "event": {"type": "content_block_start"}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "h"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ello world"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "..."}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello world"}]}},
    {"type": "stream_event", "event": {"type": "content_block_stop"}},
    {"type": "stream_event", "event": {"type": "message_delta"}},
    {"type": "stream_event", "event": {"type": "message_stop"}},
    {"type": "rate_limit_event"},
    {"type": "result", "result": "hello world", "session_id": "s1"},
]


# A turn that thinks, runs a tool, then answers. The block starts carry the
# API's ``content_block`` (type, and the tool's name); the CLI then repeats
# each finished block as a complete ``assistant`` message, as it does for text.
TOOL_TURN = [
    {"type": "system", "subtype": "init", "session_id": "s2"},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "thinking", "thinking": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "private"}}},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "private"}]}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
                                       "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{\"command\":\"ls\"}"}}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"}]}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "tool_use", "id": "toolu_2", "name": "mcp__cowork__COMPOSIO_SEARCH_TOOLS", "input": {}}}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_2", "name": "mcp__cowork__COMPOSIO_SEARCH_TOOLS", "input": {}}]}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "one file"}}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "one file"}]}},
    {"type": "result", "result": "one file", "session_id": "s2"},
]


def forward(events, kind="token"):
    """The adapter's dedupe rule, replayed for one event kind: forward the
    partial events, skip the complete assistant message that repeats them,
    reset per block. ``token`` returns the text, ``model-loading`` the label."""
    out, saw_partial = [], False
    field = "token" if kind == "token" else "label"
    for e in events:
        if not e or e["type"] != kind:
            continue
        if e.get("partial"):
            saw_partial = True
        elif saw_partial:
            saw_partial = False
            continue
        out.append(e[field])
    return out


class StreamDeltaTests(unittest.TestCase):
    def test_text_deltas_become_partial_tokens_and_other_deltas_are_dropped(self) -> None:
        events = [parse_stream_line(line(o)) for o in CAPTURE]
        tokens = [e for e in events if e and e["type"] == "token"]
        self.assertEqual([(t["token"], t.get("partial", False)) for t in tokens],
                         [("h", True), ("ello world", True), ("hello world", False)])
        self.assertEqual(events[0], {"type": "session_id", "session_id": "s1"})
        self.assertEqual(events[-1]["type"], "result")
        # thinking and tool-input deltas never reach the stream
        self.assertTrue(all("..." not in (t["token"]) and "{" not in t["token"] for t in tokens))

    def test_the_repeat_is_skipped_so_text_goes_out_once(self) -> None:
        events = [parse_stream_line(line(o)) for o in CAPTURE]
        self.assertEqual(forward(events), ["h", "ello world"])
        self.assertEqual("".join(forward(events)), "hello world")

    def test_an_older_cli_without_partials_streams_exactly_as_before(self) -> None:
        old = [o for o in CAPTURE if o["type"] != "stream_event"]
        events = [parse_stream_line(line(o)) for o in old]
        self.assertEqual(forward(events), ["hello world"])

    def test_two_text_blocks_in_one_turn_each_skip_their_own_repeat(self) -> None:
        turn = [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "one"}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},   # no text: no token
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "two"}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "two"}]}},
        ]
        events = [parse_stream_line(line(o)) for o in turn]
        self.assertEqual(forward(events), ["one", "two"])

    def test_thinking_and_tool_use_become_activity_labels_never_content(self) -> None:
        """What codex already streams (a ``model-loading`` label per work
        item) claude_code now streams too: the block start names the
        activity, and neither the thinking text nor the tool input goes out."""
        events = [parse_stream_line(line(o)) for o in TOOL_TURN]
        self.assertEqual(forward(events, "model-loading"),
                         ["thinking", "running Bash", "running COMPOSIO_SEARCH_TOOLS"])
        self.assertEqual(forward(events), ["one file"])
        raw = json.dumps([e for e in events if e])
        self.assertNotIn("private", raw)       # thinking text
        self.assertNotIn("command", raw)       # tool input
        # a tool result is the agent's input, not the reply: nothing is forwarded
        self.assertIsNone(parse_stream_line(line(TOOL_TURN[7])))

    def test_activity_labels_survive_an_older_cli_without_partials(self) -> None:
        """Without ``stream_event`` lines the complete ``assistant`` blocks are
        the only signal, and they still name the activity, once each."""
        old = [o for o in TOOL_TURN if o["type"] != "stream_event"]
        events = [parse_stream_line(line(o)) for o in old]
        self.assertEqual(forward(events, "model-loading"),
                         ["thinking", "running Bash", "running COMPOSIO_SEARCH_TOOLS"])
        self.assertEqual(forward(events), ["one file"])

    def test_every_event_is_in_the_shared_stream_vocabulary(self) -> None:
        from services.cowork_agent.engine import stream_events

        for capture in (CAPTURE, TOOL_TURN):
            for e in (parse_stream_line(line(o)) for o in capture):
                if e is None:
                    continue
                self.assertIn(e["type"], stream_events.EVENT_TYPES, e)
                if e["type"] == stream_events.ACTIVITY:
                    self.assertTrue(stream_events.is_known_label(e["label"]), e)

    def test_the_command_asks_for_partial_messages_when_streaming(self) -> None:
        from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter.__new__(ClaudeCodeAdapter)   # no __init__: needs no config or files
        adapter.config, adapter.commands = {}, {}
        streaming = adapter._build_cmd("hi", None, stream=True)
        one_shot = adapter._build_cmd("hi", None, stream=False)
        self.assertIn("--include-partial-messages", streaming)
        self.assertNotIn("--include-partial-messages", one_shot)
