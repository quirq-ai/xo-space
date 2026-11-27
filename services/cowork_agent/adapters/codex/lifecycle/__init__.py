"""codex's lifecycle (the ``lifecycle`` capability): where its CLI is, its
login, account, sign-in and the credential an ``openai`` account listener
is given. Its commands and old Connect URLs are in
``config/agents/codex/manifest.json``."""

from __future__ import annotations

import os
from pathlib import Path

from services.cowork_agent.adapters.cli_status import resolve_binary
from services.cowork_agent.adapters.codex.lifecycle import login
from services.cowork_agent.adapters.codex.lifecycle.login import codex_home
from services.cowork_agent.adapters.codex.lifecycle.signin import LEGACY, SIGN_IN
from services.runtime_lifecycle.contract import LifecycleSpec

CLI_PATH_ENV = "CODEX_CLI_PATH"
DEFAULT_BINARY = "codex"


def find_binary(home: Path | None = None) -> str:
    """``CODEX_CLI_PATH``, then PATH, then where codex's own installer puts it
    (a server's PATH often lacks ``~/.local/bin``); else the bare name, which
    fails to start as ``cli_missing``."""
    found = resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)
    if found != DEFAULT_BINARY:
        return found
    standalone = (home or codex_home()) / "packages" / "standalone" / "current"
    for candidate in (Path.home() / ".local" / "bin" / "codex", standalone / "bin" / "codex", standalone / "codex"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return DEFAULT_BINARY


SPEC = LifecycleSpec(title="Codex", binary=find_binary, logged_in=lambda: login.logged_in(codex_home()),
                     account=lambda: login.account_fingerprint(codex_home()),
                     via_api_key=lambda: login.api_key_login(codex_home()), sign_in=SIGN_IN,
                     credential=login.credential, legacy=LEGACY)
