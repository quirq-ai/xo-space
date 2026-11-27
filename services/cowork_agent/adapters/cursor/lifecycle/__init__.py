"""cursor-agent's lifecycle (the ``lifecycle`` capability): where its CLI is,
its login check and its sign-in. Its commands are in
``config/agents/cursor/manifest.json``.

Its login is read as the runtime reads it, ``cursor-agent status --format
json`` (``isAuthenticated``): a CLI call, so the advisory asks the runtime
rather than polling it."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.cli_status import locate
from services.cowork_agent.adapters.env_whitelist import base_env
from services.cowork_agent.adapters.kept_check import KeptLogin
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


def _status(result: CommandResult | None) -> bool | None:
    """``isAuthenticated`` as ``status --format json`` says it; None when it said nothing (it didn't run,
    timed out, or printed no such JSON: a network error, say)."""
    try:
        value = json.loads(result.output).get("isAuthenticated") if result is not None else None
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def authenticated(result: CommandResult | None) -> bool:
    """``status --format json`` says ``isAuthenticated``."""
    return _status(result) is True


def decided(result: CommandResult | None) -> bool:
    """Whether ``status`` answered at all (signed in or out): only then is its answer kept."""
    return _status(result) is not None


def login_files(env: Mapping[str, str]) -> tuple[Path, ...]:
    """The files its sign-in writes, where ``env`` puts them: its tokens and its CLI config (``authInfo``)."""
    config = Path(env.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    own = Path(env.get("CURSOR_CONFIG_DIR") or "~/.cursor").expanduser()
    return config / "cursor" / "auth.json", own / "cli-config.json"


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


def _lapsed(env: Mapping[str, str]) -> bool | None:
    """Whether its stored access token has passed its expiry (its ``exp``
    claim), when there is one: ``status`` may answer otherwise once it has,
    with no file changed, so the kept answer must not outlive it. Reads the
    claim only; no token leaves this function."""
    try:
        payload = json.loads(login_files(env)[0].read_text())["accessToken"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"]) <= time.time()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None


#: Its login check (status), kept until the CLI, the files its sign-in writes, the key variables or the
#: stored login's expiry move (``kept_check.KeptLogin``).
_login = KeptLogin[CommandResult](
    binary=lambda: find_binary(), env=lambda: base_env(names=EXTRA_ENV + AUTH_ENV), variables=EXTRA_ENV + AUTH_ENV,
    files=lambda env: login_files(env), lapsed=lambda env: _lapsed(env), probe=lambda: _probe(keys=True),
    authenticated=lambda result: authenticated(result), decided=lambda result: decided(result))
login_stamp, login_result, logged_in, forget_login = _login.stamp, _login.result, _login.logged_in, _login.forget
login_decided = _login.decided


def own_login() -> bool:
    """Its own login, which a sign-in lands (a key variable doesn't count)."""
    return authenticated(_probe(keys=False))


def auth_url(line: str) -> str | None:
    found = _LINK.search(line)
    return found.group(0) if found else None


SPEC = LifecycleSpec(title="Cursor", binary=find_binary, logged_in=logged_in, forget=forget_login, login_polled=False,
                     sign_in=SignIn(style="device_code", argv=("login",), auth_url=auth_url,
                                    env={"NO_OPEN_BROWSER": "1"}, landed=own_login, relogin=True))
