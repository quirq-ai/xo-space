"""An adapter whose agent chats on a storm runtime has no chat of its own
(storm PR 19; sedc 09 #5): it still constructs (``/providers/status`` and the
runtime build ``ClaudeCodeAdapter({})``), its ``run`` and ``stream`` refuse,
and ``chats()`` says so from the class alone."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from services.cowork_agent.adapters.base import BaseAgentAdapter


class NoChat(BaseAgentAdapter):
    @property
    def adapter_name(self) -> str:
        return "no_chat"


class Chats(NoChat):
    async def stream(self, question: str, session_id: str | None = None, **kwargs: Any):
        yield {"type": "token", "token": question}
        yield {"done": True, "native_session_id": None}


class DefaultChatTests(unittest.TestCase):
    def test_an_adapter_without_chat_constructs_and_refuses_a_turn(self) -> None:
        adapter = NoChat({})
        with self.assertRaisesRegex(NotImplementedError, "no_chat has no chat of its own"):
            adapter.stream("hi")
        with self.assertRaisesRegex(NotImplementedError, "no_chat has no chat of its own"):
            asyncio.run(adapter.run("hi"))

    def test_chats_is_read_from_the_class(self) -> None:
        self.assertFalse(NoChat.chats())
        self.assertTrue(Chats.chats())

    def test_every_agent_with_chat_code_today_says_it_chats(self) -> None:
        from services.cowork_agent.adapters.loader import load_capability
        for agent in ("grokbot", "hermes", "openclaw"):
            with self.subTest(agent=agent):
                self.assertTrue(load_capability("adapter", agent=agent).Adapter.chats())


if __name__ == "__main__":
    unittest.main()
