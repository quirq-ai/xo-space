"""Golden recordings of every old Connect URL, against stub CLIs.

Each scenario drives an old Connect route as xo-swarm does (the SSE stream,
pasting a code on ``auth_url`` and again on ``code_rejected``) and compares
the raw SSE frames and callback answers with ``golden/connect.json``, byte
for byte, session ids aside. The recordings were made with the old flows,
before any of them moved onto the lifecycle engine; a flow that moves must
replay them unchanged. ``RECORD_GOLDEN=1`` rewrites the file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import typing
import unittest
from pathlib import Path
from unittest import mock

from fastapi.routing import APIRoute

GOLDEN = Path(__file__).parent / "golden" / "connect.json"
SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

CLAUDE = """#!/bin/sh
[ "$*" = "auth login --claudeai" ] || { echo "unexpected argv: $*" >&2; exit 64; }
echo "Opening browser to sign in..."
echo "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=c&state=s"
printf 'Paste code here if prompted > '
while read -r code; do
  case "$code" in
    good*) echo "Login successful."; mkdir -p "$HOME/.claude"
           printf '{"claudeAiOauth": {"refreshToken": "r"}}' > "$HOME/.claude/.credentials.json"; sleep 0.3; exit 0 ;;
    fail*) echo "Login failed: Request failed with status code 400"; sleep 0.3; exit 1 ;;
    *) echo "Invalid code. Please make sure the full code was copied"; printf 'Paste code here if prompted > ' ;;
  esac
done
exit 1
"""

CODEX = """#!/bin/sh
[ "$*" = "login --device-auth" ] || { echo "unexpected argv: $*" >&2; exit 64; }
echo "Follow these steps to sign in with ChatGPT using device code authorization:"
echo ""
echo "1. Open this link in your browser and sign in to your account"
echo "   https://auth.openai.com/codex/device"
echo ""
echo "2. Enter this one-time code (expires in 15 minutes)"
echo "   ABCD-EFGH"
sleep 0.3
if [ {exit} = 0 ]; then mkdir -p "$HOME/.codex"; printf '{"tokens": {"access_token": "a", "refresh_token": "r"}}' > "$HOME/.codex/auth.json"; fi
exit {exit}
"""

AGY = """#!/bin/sh
token="$HOME/.gemini/antigravity-cli/antigravity-oauth-token"
state="$(dirname "$0")/.version"
case "$*" in
  --version) cat "$state"; exit 0 ;;
  update)
    echo "Checking for updates..."
    sleep 0.1
    if [ "{fail}" = "1" ]; then echo "network unreachable" >&2; exit 3; fi
    echo "Updated to 1.5.20"
    printf '1.5.20\\n' > "$state"; exit 0 ;;
  "acp --login") ;;
  *) echo "unexpected argv: $*" >&2; exit 64 ;;
esac
echo "Open this link in a browser to log in with Google:"
echo ""
echo "  https://accounts.google.com/o/oauth2/auth?client_id=x&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&state=s"
echo ""
echo "Then paste the authorization code, or the full URL of the page you land on, and press Enter:"
read -r code
if [ "$code" = "good-code" ]; then
  mkdir -p "$(dirname "$token")"
  printf '{"token": {"refresh_token": "r"}, "auth_method": "consumer"}' > "$token"
  sleep 30
fi
echo "couldn't log in: token exchange failed: oauth2: \\"invalid_grant\\" \\"Bad Request\\""
sleep 0.3
exit 1
"""


def routers() -> list:
    """Every router that serves an old Connect URL."""
    from services.runtime_lifecycle import legacy

    return [legacy.router()]


def endpoint(method: str, path: str):
    for router in routers():
        for route in router.routes:
            if isinstance(route, APIRoute) and route.path == path and method in route.methods:
                return route.endpoint
    raise LookupError(f"{method} {path} is not served")


#: name -> (stream path, codes to paste in turn, the stub CLIs to install, extra setup)
SCENARIOS = {
    "claude_success": ("/connect/claude-code", ["good#s"], {"claude": CLAUDE}, None),
    "claude_alias_success": ("/claude/setup-token", ["good#s"], {"claude": CLAUDE}, None),
    "claude_rejected_then_success": ("/connect/claude-code", ["bad", "good#s"], {"claude": CLAUDE}, None),
    "claude_failure": ("/connect/claude-code", ["fail#s"], {"claude": CLAUDE}, None),
    "codex_success": ("/connect/codex", [], {"codex": CODEX.replace("{exit}", "0")}, None),
    "codex_alias_failure": ("/codex/setup", [], {"codex": CODEX.replace("{exit}", "1")}, None),
    "codex_not_installed": ("/connect/codex", [], {}, None),
    "claude_already_signed_in": ("/connect/claude-code", ["good#s"], {"claude": CLAUDE}, "logins"),
    "codex_already_signed_in": ("/connect/codex", [], {"codex": CODEX.replace("{exit}", "0")}, "logins"),
    "agy_success": ("/connect/antigravity", ["good-code"], {"agy": AGY.replace("{fail}", "0")}, None),
    "agy_rejected": ("/connect/antigravity", ["bad-code"], {"agy": AGY.replace("{fail}", "0")}, None),
    "agy_already_signed_in": ("/connect/antigravity", ["unused"], {"agy": AGY.replace("{fail}", "0")}, "token"),
    "agy_update": ("/connect/antigravity/update", [], {"agy": AGY.replace("{fail}", "0")}, None),
    "agy_update_fails": ("/connect/antigravity/update", [], {"agy": AGY.replace("{fail}", "1")}, None),
}


def frames(chunk: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in chunk.splitlines() if line.startswith("data: ")]


class Stubs:
    """A HOME with stub CLIs on PATH, the env every old flow reads."""

    def __init__(self, scripts: dict[str, str], setup: str | None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name, text in scripts.items():
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)
        (self.bin / ".version").write_text("1.4.10\n")
        self.token = self.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        if setup == "token":
            self.token.parent.mkdir(parents=True)
            self.token.write_text('{"token": {"refresh_token": "r"}, "auth_method": "consumer"}')
        if setup == "logins":
            for path, text in ((".claude/.credentials.json", '{"claudeAiOauth": {"refreshToken": "old"}}'),
                               (".codex/auth.json", '{"OPENAI_API_KEY": "sk-old"}')):
                (self.home / path).parent.mkdir(parents=True)
                (self.home / path).write_text(text)
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
                    "CODEX_HOME": str(self.home / ".codex"),
                    "CLAUDE_CLI_PATH": str(self.bin / "claude"), "AGY_CLI_PATH": str(self.bin / "agy")}

    def patches(self) -> list:
        return [mock.patch.dict(os.environ, self.env)]

    def cleanup(self) -> None:
        self._tmp.cleanup()


async def replay(path: str, codes: list[str]) -> dict:
    """Drive one Connect stream; the raw frames and each callback's answer."""
    raw: list[str] = []
    answers: list = []
    codes = list(codes)
    response = await endpoint("POST", path)()
    session = None
    async for chunk in response.body_iterator:
        chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
        if chunk.startswith(": heartbeat") or '"type": "ping"' in chunk:
            continue
        raw.append(SESSION.sub("<session>", chunk))
        for event in frames(chunk):
            if event["type"] == "session":
                session = event["session_id"]
            if event["type"] in ("auth_url", "code_rejected") and codes:
                callback = endpoint("POST", path + "/callback")
                body = next(iter(typing.get_type_hints(callback).values()))
                answer = await callback(body(code=codes.pop(0), session_id=session))
                answers.append(SESSION.sub("<session>", json.dumps(answer, sort_keys=True)))
    return {"frames": raw, "callbacks": answers}


def record(name: str) -> dict:
    path, codes, scripts, setup = SCENARIOS[name]
    stubs = Stubs(scripts, setup)
    patches = stubs.patches()
    try:
        for patch in patches:
            patch.start()
        return asyncio.run(asyncio.wait_for(replay(path, codes), timeout=20))
    finally:
        for patch in reversed(patches):
            patch.stop()
        stubs.cleanup()


class GoldenConnectTests(unittest.TestCase):
    def test_every_old_connect_url_replays_its_recording(self) -> None:
        if os.environ.get("RECORD_GOLDEN") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps({name: record(name) for name in SCENARIOS}, indent=1) + "\n")
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual(set(golden), set(SCENARIOS))
        for name in SCENARIOS:
            with self.subTest(name):
                self.assertEqual(record(name), golden[name])

    def test_claude_and_codex_sign_in_again_over_a_login(self) -> None:
        golden = json.loads(GOLDEN.read_text())
        for runtime in ("claude", "codex"):
            with self.subTest(runtime):
                self.assertEqual(golden[f"{runtime}_already_signed_in"], golden[f"{runtime}_success"])


if __name__ == "__main__":
    unittest.main()
