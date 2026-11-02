"""Model-free smoke tests against the installed app-server, with isolated config."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.codex.runtime import params as p
from services.cowork_agent.adapters.codex.runtime.core import CodexRuntime
from tests.fakes.host import ScriptedHost


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        binary = shutil.which("codex")
        if not binary:
            self.skipTest("no installed codex CLI")
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-optimization-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / ".codex"
        self.home.mkdir()
        (self.home / "config.toml").write_text(
            'model_provider="audit"\nmodel="audit-model"\n[analytics]\nenabled=false\n'
            '[model_providers.audit]\nname="audit"\nbase_url="http://127.0.0.1:9/v1"\n'
            'wire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\n')
        self.env = mock.patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root),
            "CODEX_HOME": str(self.home), "CODEX_CLI_PATH": binary,
            "QUIRQ_STATE_ROOT": str(self.root / "state"), "QUIRQ_COMMAND_LOG": "off",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.host = ScriptedHost()
        self.runtime = CodexRuntime(home=self.home)

    async def open(self, **kwargs):
        session = await self.runtime.open(c.SessionSpec(cwd=str(self.root), **kwargs), self.host,
                                          session_id="cse_native_test")
        self.addAsyncCleanup(session.close)
        return session

    def comms(self, *, empty=False):
        fixture = str(Path(__file__).parent / "fakes" / "mcp_stdio.py")
        return c.McpServer(name="audit_comms", transport="stdio",
                           command=(sys.executable, "-u", fixture, *(["--empty"] if empty else [])),
                           auto_approve=True, terminal_tools=("reply",))

    async def test_required_comms_inventory_uses_existing_native_connection(self):
        optional = c.McpServer(name="optional", transport="http", url="http://127.0.0.1:9/mcp")
        session = await self.open(mcp_servers=(self.comms(), optional))
        self.assertTrue(session.native_id)
        self.assertEqual(session.state, "idle")

    async def test_empty_required_tool_catalog_is_refused_retryably(self):
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.open(mcp_servers=(self.comms(empty=True),))
        self.assertTrue(caught.exception.retryable)
        self.assertIn("conversation tools", caught.exception.message)

    async def test_required_mcp_connection_failure_is_refused_retryably(self):
        bad = c.McpServer(name="unavailable", transport="http", url="http://127.0.0.1:9/mcp",
                          terminal_tools=("reply",))
        with mock.patch.object(p, "COMMS_START_TIMEOUT_S", 0.2):
            with self.assertRaises(c.RuntimeFailure) as caught:
                await self.open(mcp_servers=(bad,))
        self.assertTrue(caught.exception.retryable)
        self.assertIn("conversation tools", caught.exception.message)

    async def test_allocated_only_resume_has_specific_failure_without_replacement(self):
        session = await self.open()
        native_id = session.native_id
        await session.close()
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.open(resume=native_id)
        self.assertEqual(caught.exception.code, "session_not_found")
        self.assertFalse(caught.exception.retryable)

    async def test_durable_synthetic_history_resumes_with_exclusion(self):
        session = await self.open()
        await session._rpc.request("thread/inject_items", {"threadId": session.native_id, "items": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "test history"}]}]},
            timeout=10)
        native_id = session.native_id
        await session.close()
        resumed = await self.open(resume=native_id)
        self.assertEqual(resumed.native_id, native_id)
