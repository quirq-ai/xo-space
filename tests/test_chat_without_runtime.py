"""H4 (ch 17 §5; sedc 09 #6): an agent without a storm runtime chats
exactly as it did before the chat API switch.

``POST /api/chat/prompt``, ``GET /api/chat/stream/{stream_id}`` and
``POST /api/chat/abort {stream_id}`` are driven through ``server.app`` in
each agent's sealed interpreter (``tests/chat_today_probe.py``): openclaw,
hermes and grokbot as the Space's own agent, every Space with a body naming
each of them, and naming cursor (no adapter: today's stream fails, kept as
it is) or an agent that doesn't exist. The answer, the stream's bytes, a
reconnect's and what each agent's chat code was handed are compared with
``tests/fixtures/golden/chat-without-runtime.json``, captured on the code
before PR 19 (6febccd). ``/api/chat/respond`` (removed), ``/api/chat/active``
(storm's now) and an abort naming nothing (now refused) changed on purpose
and aren't in it.

A deliberate change is a contract change for the callers of these agents:
regenerate with ``XO_UPDATE_GOLDEN=1`` and say why in the pull request.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests.frozen_sandbox import Sandbox

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "chat-without-runtime.json"
PROBE = "from tests.chat_today_probe import main\nmain()\n"


class ChatWithoutRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        box = Sandbox()
        try:
            cls.observed = box.probe(PROBE)
        finally:
            box.cleanup()
        if os.environ.get("XO_UPDATE_GOLDEN") == "1":
            GOLDEN.write_text(json.dumps(cls.observed, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_every_agent_is_observed(self) -> None:
        self.assertEqual(sorted(self.observed), sorted(self.golden))

    def test_chat_answers_byte_for_byte_as_before(self) -> None:
        for agent, scenarios in self.golden.items():
            for name, expected in scenarios.items():
                with self.subTest(space=agent, scenario=name):
                    self.assertEqual(self.observed[agent][name], expected)


if __name__ == "__main__":
    unittest.main()
