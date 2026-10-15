"""A sealed machine for the frozen-surface tests (storm ch 05 §6, ch 06 §2).

Each agent is probed in its own interpreter, because ``AGENT_NAME`` decides
what is mounted at import time. The interpreter sees only this sandbox: a
temp home with fake logins for claude, codex and agy, fake CLIs on PATH (the
real ones are never run), empty state and projects roots, and fixed fake key
values. Nothing from the developer's machine reaches the result, so the same
code gives the same answer everywhere.
"""

from __future__ import annotations

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.fakes import agy, claude, codex, install, scenario
from utils.commands import run_sync

REPO = Path(__file__).resolve().parents[1]
#: Runs before every probe. A bare ``load_dotenv()`` (server.py,
#: registry/settings.py) walks up from the code's folder and loads the first
#: ``.env`` of any ancestor, which on a developer's machine can hold real
#: keys; in the sandbox it finds nothing.
NO_ANCESTOR_DOTENV = "import dotenv.main\ndotenv.main.find_dotenv = lambda *args, **kwargs: ''\n"
#: Stands in for the sandbox path wherever it appears in a result.
SANDBOX = "<sandbox>"
FAKE_KEYS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-FAKE",
    "OPENAI_API_KEY": "sk-FAKE-openai",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-FAKE",
}


def agents() -> list[str]:
    return sorted(p.parent.name for p in (REPO / "config" / "agents").glob("*/manifest.json"))


class Sandbox:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="frozen-")
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self._login()
        install("claude", self.bin, scenario([], argv=[claude.version(), claude.auth_status(logged_in=True)]))
        install("codex", self.bin, scenario([], argv=[codex.version()]))
        install("agy", self.bin, scenario([], argv=[agy.version()]))

    def _login(self) -> None:
        creds = {"claudeAiOauth": {"accessToken": "fake-access", "refreshToken": "fake-refresh",
                                   "expiresAt": 4102444800000}}
        files = {
            ".claude/.credentials.json": json.dumps(creds),
            ".codex/auth.json": json.dumps({"tokens": {"access_token": "fake"}}),
            ".gemini/antigravity-cli/antigravity-oauth-token": json.dumps({"token": {"refresh_token": "fake"}}),
        }
        for rel, text in files.items():
            path = self.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for folder in ("projects", "state"):
            (self.root / folder).mkdir()

    def env(self, agent: str) -> dict[str, str]:
        state = self.root / "state"
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "AGENT_NAME": agent,
            "XO_PROJECTS_ROOT": str(self.root / "projects"),
            "QUIRQ_STATE_ROOT": str(state),
            "QUIRQ_RUNTIME_FILE": str(state / "settings" / "runtime.env"),
            "QUIRQ_SECRETS_FILE": str(state / "secrets" / "secrets.env"),
            "QUIRQ_SKIP_BOOT_INSTALL": "1",
            "QUIRQ_WATCHER_ENABLED": "false",
            "QUIRQ_COMMAND_LOG": "off",
            "PYTHONDONTWRITEBYTECODE": "1",
            **FAKE_KEYS,
        }

    def probe(self, code: str, *, only: list[str] | None = None,
              env: dict[str, str] | None = None) -> dict[str, object]:
        """Run ``code`` under every agent (or ``only`` these) at once, with
        ``env`` on top of the sandbox's; each run prints one JSON document as
        its last line. Returns ``{agent: document}`` with the sandbox path
        replaced by ``<sandbox>``."""
        def one(agent: str) -> tuple[str, object]:
            result = run_sync([sys.executable, "-c", NO_ANCESTOR_DOTENV + code], cwd=REPO, env={**self.env(agent), **(env or {})},
                              timeout=120, separate_stderr=True)
            if not result.ok:
                raise AssertionError(f"probe failed for AGENT_NAME={agent}:\n{result.stderr[-3000:]}")
            text = result.output.strip().splitlines()[-1].replace(str(self.root), SANDBOX)
            return agent, json.loads(text)

        names = only or agents()
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            return dict(pool.map(one, names))

    def cleanup(self) -> None:
        self._tmp.cleanup()


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    """An env snapshot with only what the sandbox controls: the interpreter's
    own additions (its path, locale coercion) vary by machine."""
    return {k: v for k, v in sorted(env.items()) if k not in {"LC_CTYPE", "_", "PWD", "SHLVL", "OLDPWD"}}


__all__ = ["FAKE_KEYS", "SANDBOX", "Sandbox", "agents", "scrub_env"]
