"""A stream-json line longer than asyncio's 64 KiB default must not end the turn.

Claude Code prints one JSON line per event, and a tool result that carries a
screenshot or a large file is one line of hundreds of KB. With the default
StreamReader limit, reading that line raised, the chat got ``agent-error``
and lost its reply, while ``claude`` kept working unseen.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.claude_code import adapter as adapter_mod

BIG = 300_000


def fake_cli(folder: Path) -> Path:
    """A stand-in ``claude`` that prints a huge tool-result line, then a reply."""
    lines = [
        {"type": "system", "subtype": "init", "session_id": "native-1"},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * BIG}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "finished"}]}},
        {"type": "result", "result": "finished", "session_id": "native-1", "num_turns": 2,
         "total_cost_usd": 0.01, "usage": {}},
    ]
    data = folder / "lines.jsonl"
    data.write_text("".join(json.dumps(line) + "\n" for line in lines))
    script = folder / "claude"
    script.write_text(f"#!/bin/sh\ncat '{data}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class LongLineTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.base / "s"),
                                      "XO_PROJECTS_ROOT": str(self.base / "p")})
        env.start()
        self.addCleanup(env.stop)

    def test_a_line_over_64_kib_is_read_and_the_reply_arrives(self) -> None:
        cli = fake_cli(self.base)

        async def consume() -> list[dict]:
            events = []
            adapter = adapter_mod.Adapter({"cli_path": str(cli)})
            async for event in adapter.stream("hi", None, our_session_id="s1", is_new_session=True):
                events.append(event)
            return events

        events = asyncio.run(consume())
        self.assertFalse([e for e in events if e.get("type") == "error"])
        self.assertIn("finished", "".join(e.get("token", "") for e in events if e.get("type") == "token"))
        self.assertTrue(events[-1].get("done"))

    def test_the_limit_is_well_above_a_screenshot(self) -> None:
        self.assertGreaterEqual(adapter_mod.STREAM_LINE_LIMIT, 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
