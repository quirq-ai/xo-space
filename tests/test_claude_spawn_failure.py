"""When ``claude`` cannot start, the stream reports why.

The streaming adapter's cleanup reads the session id and usage the turn
produced. If the spawn itself failed (no ``claude`` binary, a bad cwd) those
were never set, and the cleanup raised ``UnboundLocalError``, which replaced
the real error in the chat's ``agent-error`` event.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.claude_code import adapter as adapter_mod


class SpawnFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(base / "state"),
            "XO_PROJECTS_ROOT": str(base / "projects"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_the_spawn_error_reaches_the_caller(self) -> None:
        async def spawn(*_cmd, **_kw):
            raise FileNotFoundError("claude: not found")

        async def consume():
            async for _ in adapter_mod.Adapter({}).stream("hi", None, our_session_id="s1", is_new_session=True):
                pass

        with patch.object(adapter_mod.asyncio, "create_subprocess_exec", spawn):
            with self.assertRaisesRegex(FileNotFoundError, "claude: not found"):
                asyncio.run(consume())


if __name__ == "__main__":
    unittest.main()
