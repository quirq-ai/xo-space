"""cursor-agent's lifecycle (the ``lifecycle`` capability): where its CLI is,
its login check and its sign-in. Its commands are in
``config/agents/cursor/manifest.json``.

Its login is read as the runtime reads it, ``cursor-agent status --format
json`` (``isAuthenticated``): a CLI call, so the advisory asks the runtime
rather than polling it."""

from __future__ import annotations

import json
import re

from services.cowork_agent.adapters.cli_status import locate
from services.cowork_agent.adapters.env_whitelist import base_env
from services.runtime_lifecycle.contract import LifecycleSpec, SignIn
from utils.commands import CommandResult, run_sync

PATH_ENV = "CURSOR_CLI_PATH"
BINARIES = ("cursor-agent", "~/.local/bin/cursor-agent")
AUTH_ENV = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")
EXTRA_ENV = ("CURSOR_CONFIG_DIR",)
STATUS = ("status", "--format", "json")
PROBE_TIMEOUT_S = 10
_LINK = re.compile(r"https://[^\s\"'<>\x00-\x1f]+")


def find_binary() -> str:
    return locate(PATH_ENV, BINARIES) or BINARIES[0]


def authenticated(result: CommandResult | None) -> bool:
    """``status --format json`` says ``isAuthenticated``."""
    try:
        return result is not None and json.loads(result.output).get("isAuthenticated") is True
    except (ValueError, AttributeError):
        return False


def _probe(*, keys: bool) -> CommandResult | None:
    """``{binary} *STATUS`` as the runtime runs it; without the key variables unless ``keys``."""
    env = base_env(names=EXTRA_ENV + AUTH_ENV)
    for var in () if keys else AUTH_ENV:
        env.pop(var, None)
    try:
        return run_sync([find_binary(), *STATUS], timeout=PROBE_TIMEOUT_S, env=env, separate_stderr=True,
                        log_output=False)
    except OSError:
        return None


def logged_in() -> bool:
    """``status`` says it is signed in, by its own login or a key variable."""
    return authenticated(_probe(keys=True))


def own_login() -> bool:
    """Its own login, which a sign-in lands (a key variable doesn't count)."""
    return authenticated(_probe(keys=False))


def auth_url(line: str) -> str | None:
    found = _LINK.search(line)
    return found.group(0) if found else None


SPEC = LifecycleSpec(title="Cursor", binary=find_binary, logged_in=logged_in, login_polled=False,
                     sign_in=SignIn(style="device_code", argv=("login",), auth_url=auth_url,
                                    env={"NO_OPEN_BROWSER": "1"}, landed=own_login, relogin=True))
