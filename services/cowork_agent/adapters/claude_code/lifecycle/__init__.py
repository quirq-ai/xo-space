"""claude's lifecycle (the ``lifecycle`` capability): where its CLI is, its
login, account and sign-in. Its commands and old Connect URLs are in
``config/agents/claude_code/manifest.json``."""

from __future__ import annotations

import os
from pathlib import Path

from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter, config_file
from services.cowork_agent.adapters.claude_code.lifecycle.login import account_fingerprint, api_key_login, logged_in
from services.cowork_agent.adapters.claude_code.lifecycle.signin import LEGACY, SIGN_IN
from services.cowork_agent.adapters.cli_status import resolve_binary
from services.runtime_lifecycle.contract import LifecycleSpec

CLI_PATH_ENV = "CLAUDE_CLI_PATH"
DEFAULT_BINARY = "claude"


def find_binary() -> str:
    """``CLAUDE_CLI_PATH``, then PATH, then where claude's own installers put
    it (a server's PATH often lacks ``~/.local/bin``); else the bare name,
    which fails to start as ``cli_missing``."""
    found = resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)
    if found != DEFAULT_BINARY:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "claude", Path.home() / ".claude" / "local" / "claude"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return DEFAULT_BINARY


def _account() -> str | None:
    return account_fingerprint(ClaudeCodeAdapter({}).credentials_dir(), config_file().parent)


SPEC = LifecycleSpec(title="Claude", binary=find_binary, logged_in=logged_in, account=_account,
                     via_api_key=api_key_login, sign_in=SIGN_IN, legacy=LEGACY)
