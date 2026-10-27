"""claude's adapter class: its name, its manifest, and the auth rules every
spawned ``claude`` runs with (``cli_env()`` for the ``/providers/status``
probe, ``apply_auth`` for storm's runtime and remote control).

Its plain chat runs on storm's runtime (``runtime/``); the flagged
``AgentDispatcher`` chat path it had is gone (storm PR 19, sedc 09 #5).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.cowork_agent.adapters.base import BaseAgentAdapter


class ClaudeCodeAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "claude_code"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    def cli_env(self) -> dict[str, str]:
        """The environment the ``auth status`` probe behind ``/providers/status``
        runs with: this process's env under claude's auth rules (``apply_auth``,
        which storm's runtime applies to its own env too), so the tile never
        advertises a key the CLI would not use (notably a shadowed
        ``ANTHROPIC_API_KEY`` when a native login is present).
        """
        return self.apply_auth(os.environ.copy())

    def apply_auth(self, env: dict[str, str]) -> dict[str, str]:
        """Today's auth rules for a spawned ``claude``, applied to ``env`` in
        place and returned. ``cli_env()`` and storm's runtime
        (``runtime/env.py``) both use it, so the providers tile and every
        session resolve auth the same way."""
        # Drop empty / placeholder auth vars so the CLI falls back cleanly.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            if env.get(key) in (None, "", "sk-ant-none"):
                env.pop(key, None)
        # An OAuth token (sk-ant-oat...) is not a valid API key; if one leaked into
        # ANTHROPIC_API_KEY the CLI would pick API-key auth and fail. Strip it.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY"):
            if (env.get(key) or "").startswith("sk-ant-oat"):
                env.pop(key, None)
        # Auth precedence for the spawned `claude`:
        #   1. A native login (~/.claude/.credentials.json) — written by the
        #      "Connect Claude" (`claude auth login`) flow. The CLI auto-refreshes
        #      it via its refresh token, so it is the durable, self-healing source.
        #   2. An explicit ANTHROPIC_API_KEY — a deliberate API-billing alternative.
        # Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN outrank the subscription
        # login in the CLI's precedence chain, so when a usable native login is
        # present we drop them from the subprocess env — otherwise a leftover/fanned
        # token (prod fans the API key into CLAUDE_CODE_OAUTH_TOKEN) would override
        # the fresh login and silently bill the API or fail auth. With no native
        # login we leave these intact so explicit API-key mode still works.
        if self._has_usable_native_login():
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                env.pop(key, None)
        return env

    def _agent_home_dir(self) -> Path:
        """The active agent's home (``~/.claude``), from the manifest ``home_dir``."""
        return Path(os.path.expanduser(self.commands.get("home_dir") or "~/.claude"))

    def _has_usable_native_login(self) -> bool:
        """True when ``~/.claude/(.)credentials.json`` holds a login the CLI can
        use: a refresh token (so the CLI renews the access token itself), or an
        access token that has not yet expired. Such a login self-refreshes, so we
        let the CLI use it rather than injecting a static env token."""
        home = self._agent_home_dir()
        for name in (".credentials.json", "credentials.json"):
            path = home / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
            if not isinstance(oauth, dict):
                oauth = data if isinstance(data, dict) else {}
            if oauth.get("refreshToken"):
                return True
            access = oauth.get("accessToken")
            expires_at = oauth.get("expiresAt")
            if access and isinstance(expires_at, (int, float)):
                # expiresAt is epoch milliseconds.
                if expires_at / 1000 > datetime.now(timezone.utc).timestamp():
                    return True
            elif access and expires_at is None:
                return True
        return False

    async def health(self) -> dict[str, Any]:
        cli = self.config.get("cli_path") or "claude"
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            version = stdout.decode().strip()
            return {"ok": True, "version": version}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# Stable discovery alias — the dynamic loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter, so every
# adapter module exposes its class under this name.
Adapter = ClaudeCodeAdapter
