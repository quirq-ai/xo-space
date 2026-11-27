"""grok's lifecycle (the ``lifecycle`` capability): where its CLI is, its
login check and its sign-in. Its commands are in
``config/agents/grok_build/manifest.json``.

Its login is read as the runtime reads it, ``grok models`` listing models
without "not authenticated": a CLI call, so the advisory asks the runtime
rather than polling it."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.cli_status import locate
from services.cowork_agent.adapters.env_whitelist import base_env
from services.cowork_agent.adapters.kept_check import KeptLogin
from services.runtime_lifecycle.contract import LifecycleSpec, SignIn
from utils.commands import CommandResult, run_sync

PATH_ENV = "GROK_CLI_PATH"
BINARIES = ("grok", "~/.grok/bin/grok")
AUTH_ENV = ("XAI_API_KEY",)
EXTRA_ENV = ("GROK_HOME",)
STATUS = ("models",)
PROBE_TIMEOUT_S = 10
_LINK = re.compile(r"https://[^\s\"'<>\x00-\x1f]+")


def find_binary() -> str:
    return locate(PATH_ENV, BINARIES) or BINARIES[0]


def _signed_out(result: CommandResult | None) -> bool:
    return result is not None and "not authenticated" in (result.output + (result.stderr or "")).lower()


def authenticated(result: CommandResult | None) -> bool:
    """``grok models`` lists models and doesn't say "not authenticated"."""
    return (result is not None and result.ok and "available models:" in result.output.lower()
            and not _signed_out(result))


def decided(result: CommandResult | None) -> bool:
    """Whether ``grok models`` answered (models listed, or "not authenticated"): only then is it kept.
    Any other failure (a network error, a vendor outage, a timeout) says nothing about the login."""
    return authenticated(result) or _signed_out(result)


def login_files(env: Mapping[str, str]) -> tuple[Path, ...]:
    """The file its sign-in writes: its tokens, in its home (``GROK_HOME``)."""
    return (Path(env.get("GROK_HOME") or "~/.grok").expanduser() / "auth.json",)


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
    """Whether a stored login has passed its ``expires_at``, when one says:
    ``grok models`` may answer otherwise once it has (a refresh that fails for
    good leaves the file as it was), so the kept answer must not outlive it.
    Reads the expiry only; no token leaves this function."""
    try:
        logins = json.loads(login_files(env)[0].read_text())
        ends = [datetime.fromisoformat(login["expires_at"]) for login in logins.values()
                if isinstance(login, dict) and isinstance(login.get("expires_at"), str)]
        return any(end <= datetime.now(timezone.utc) for end in ends) if ends else None
    except (OSError, ValueError, TypeError, AttributeError):     # TypeError: an expiry with no zone
        return None


#: Its login check (grok models), kept until the CLI, the files its sign-in writes, the key variables or the
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


SPEC = LifecycleSpec(title="Grok Build", binary=find_binary, logged_in=logged_in, forget=forget_login,
                     login_polled=False,
                     sign_in=SignIn(style="device_code", argv=("login", "--device-auth"), auth_url=auth_url,
                                    landed=own_login, relogin=True))
