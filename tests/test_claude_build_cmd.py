"""The Claude Code command carries the turn's model and effort, and nothing else changes.

With no intelligence chosen the argv is byte-identical to what it was before
profiles existed, so Claude Code's own settings decide. A chosen model or
effort becomes ``--model`` / ``--effort`` ahead of the session flags and the
prompt, and no value can become a flag of its own.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.claude_code import adapter as adapter_mod

WORKSPACE = "/home/you/xo-projects/demo"


async def _no_lines():
    return
    yield  # an async generator that yields nothing


class _FinishedProcess:
    """Stands in for ``claude``: prints nothing and exits 0."""

    def __init__(self) -> None:
        self.stdout = _no_lines()
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    async def communicate(self):
        return b'{"result": "ok", "session_id": "n1"}', b""


class BuildCmdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = adapter_mod.Adapter({})

    def test_without_intelligence_the_command_is_unchanged(self) -> None:
        self.assertEqual(
            self.adapter._build_cmd("hi", None, stream=True, cwd=WORKSPACE, new_session_id="n1"),
            ["claude", "--dangerously-skip-permissions", "--add-dir", WORKSPACE, "--print",
             "--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--session-id", "n1", "-p", "hi"],
        )

    def test_model_and_effort_come_before_the_session_and_prompt(self) -> None:
        cmd = self.adapter._build_cmd("hi", "r1", stream=False, cwd=WORKSPACE,
                                      model="claude-opus-5-5", effort="high")
        self.assertEqual(cmd[-8:], ["--model", "claude-opus-5-5", "--effort", "high",
                                    "--resume", "r1", "-p", "hi"])

    def test_effort_alone(self) -> None:
        cmd = self.adapter._build_cmd("hi", None, stream=False, cwd=WORKSPACE, effort="low")
        self.assertIn("--effort", cmd)
        self.assertNotIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_no_value_can_become_a_flag(self) -> None:
        for field, value in (("model", "-p"), ("model", "opus 5"), ("effort", "--max")):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self.adapter._build_cmd("hi", None, stream=False, cwd=WORKSPACE, **{field: value})


class AdapterPassThroughTests(unittest.TestCase):
    """``stream()`` and ``run()`` hand the ``intelligence`` keyword to ``_build_cmd``."""

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
        self.argv: list[str] = []

        async def spawn(*cmd, **_kw):
            self.argv = list(cmd)
            return _FinishedProcess()

        patcher = patch.object(adapter_mod.asyncio, "create_subprocess_exec", spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter = adapter_mod.Adapter({})

    def _stream(self, **kwargs) -> None:
        async def consume():
            return [e async for e in self.adapter.stream("hi", None, our_session_id="s1",
                                                          is_new_session=True, **kwargs)]
        self.assertEqual(asyncio.run(consume())[-1]["done"], True)

    def test_stream_passes_the_chosen_setup(self) -> None:
        self._stream(intelligence={"profile": "deep", "model": None, "effort": "high", "source": "request"})
        self.assertEqual(self.argv[self.argv.index("--effort") + 1], "high")
        self.assertNotIn("--model", self.argv)

    def test_stream_without_intelligence_passes_no_flags(self) -> None:
        self._stream()
        self.assertNotIn("--effort", self.argv)
        self.assertNotIn("--model", self.argv)

    def test_run_passes_the_chosen_setup(self) -> None:
        result = asyncio.run(self.adapter.run("hi", None, intelligence={"model": "sonnet", "effort": "low"}))
        self.assertEqual(result["message"], "ok")
        self.assertEqual(self.argv[self.argv.index("--model") + 1], "sonnet")
        self.assertEqual(self.argv[self.argv.index("--effort") + 1], "low")


if __name__ == "__main__":
    unittest.main()
