"""Connect Antigravity signs in with agy's own ``agy acp --login``, against a
fake agy that answers only to that argv and prints what the real one printed
when probed live. The events are the ones the xo-swarm dialog reads: ``session``,
``auth_url``, ``stdout`` lines (it scrapes "invalid_grant" and "Authentication
token created successfully"), ``done`` and ``error`` carrying ``message``.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.antigravity import routes

URL = ("https://accounts.google.com/o/oauth2/auth?client_id=x&redirect_uri="
       "https%3A%2F%2Fantigravity.google%2Foauth-callback&state=s")

SCRIPT = """#!/bin/sh
here="$(dirname "$0")"
printf '%s\\n' "$*" > "$here/argv"
if [ "$*" != "acp --login" ]; then echo "unexpected argv: $*" >&2; exit 64; fi
echo "Open this link in a browser to log in with Google:"
echo ""
echo "  {url}"
echo ""
echo "Then paste the authorization code, or the full URL of the page you land on, and press Enter:"
read -r code
if [ "$code" = "good-code" ]; then
  : > "$here/token"
  sleep 30
fi
echo "couldn't log in: token exchange failed: oauth2: \\"invalid_grant\\" \\"Bad Request\\""
exit 1
"""


def frames(chunk: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in chunk.splitlines() if line.startswith("data: ")]


class ConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        home = Path(self.dir.name)
        self.agy = home / "agy"
        self.agy.write_text(SCRIPT.format(url=URL))
        self.agy.chmod(0o755)
        self.token = home / "token"
        for patch in (
            mock.patch.object(routes, "_resolve_agy_path", return_value=str(self.agy)),
            mock.patch.object(routes, "has_usable_login", side_effect=self.token.exists),
            mock.patch.object(routes, "_lock", asyncio.Lock()),
            mock.patch.object(routes, "CONNECT_TIMEOUT_SECONDS", 20),
            mock.patch.object(routes, "SSE_HEARTBEAT_SECONDS", 1),
            mock.patch.dict(os.environ, {"ANTIGRAVITY_CONNECT_USE_PTY": "1"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("_process", "_pty_master", "_session_id", "_ready", "_queue", "_auth_url",
                     "_started_at", "_last_completed_session_id", "_owner", "_session_started_at"):
            setattr(routes, name, None)
        routes._code_submitted = False
        routes._last_completed_ok = False

    def connect(self, code: str) -> list[dict]:
        async def run() -> list[dict]:
            events: list[dict] = []
            response = await routes.antigravity_connect()
            session = None
            async for chunk in response.body_iterator:
                for event in frames(chunk):
                    events.append(event)
                    if event["type"] == "session":
                        session = event["session_id"]
                    if event["type"] == "auth_url":
                        await routes.antigravity_connect_callback(
                            routes.AntigravityConnectCallbackBody(code=code, session_id=session))
            return events
        return asyncio.run(asyncio.wait_for(run(), timeout=15))

    def test_it_runs_agys_own_login_command_and_no_skip_flag(self) -> None:
        self.connect("bad-code")
        self.assertEqual((self.agy.parent / "argv").read_text().strip(), "acp --login")

    def test_the_link_reaches_the_dialog_and_the_token_ends_it(self) -> None:
        events = self.connect("good-code")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "session")
        self.assertIn({"type": "auth_url", "url": URL}, events)
        # The dialog flips to success on this line, which must come before `done`.
        self.assertEqual(events[-2], {"type": "stdout", "line": "Authentication token created successfully"})
        self.assertEqual(events[-1], {"type": "done", "returncode": 0, "ok": True})

    def test_a_rejected_code_reads_as_invalid_grant_then_fails(self) -> None:
        events = self.connect("bad-code")
        lines = [e["line"] for e in events if e["type"] == "stdout"]
        self.assertTrue(any("invalid_grant" in line for line in lines), lines)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["message"], "Antigravity login failed. Please try again.")
        self.assertFalse(self.token.exists())

    def test_already_signed_in_is_done_without_starting_agy(self) -> None:
        self.token.touch()
        events = self.connect("unused")
        self.assertEqual([e["type"] for e in events], ["session", "done"])
        self.assertFalse((self.agy.parent / "argv").exists())


if __name__ == "__main__":
    unittest.main()
