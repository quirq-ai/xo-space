"""Is claude signed in, on which account, and by an API key: read from its
own files and today's auth function (``apply_auth``), never a vendor call.
Only digests and labels leave here, never a credential."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter

#: Variables that would log claude in instead of the person's own login.
TOKEN_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
CREDENTIAL_FILES = (".credentials.json", "credentials.json")


def _surviving(environ: Mapping[str, str] | None) -> dict[str, str]:
    """The token variables ``apply_auth`` leaves the CLI (none with a usable native login)."""
    environ = os.environ if environ is None else environ
    return ClaudeCodeAdapter({}).apply_auth({var: environ[var] for var in TOKEN_VARS if environ.get(var)})


def native_login() -> bool:
    """claude's own login, which a sign-in lands."""
    return ClaudeCodeAdapter({}).has_usable_native_login()


def logged_in(environ: Mapping[str, str] | None = None) -> bool:
    """Its own login, or a token variable that logs it in."""
    return native_login() or any(_surviving(environ).values())


def account_fingerprint(claude_home: Path, home: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Which account claude runs on, as ``acct-<12 hex>``: a digest that names
    no credential, following exactly the auth today's ``apply_auth`` leaves
    the CLI. With a usable native login it drops the token variables, so the
    account is claude's own (``oauthAccount.accountUuid`` in
    ``home``/.claude.json, else the login in its credentials file); only a token
    variable that survives ``apply_auth`` (no usable native login) names it.
    None when nothing logs claude in. Blocking: call it in a thread."""
    surviving = _surviving(environ)
    source = next((f"{var}={surviving[var]}" for var in TOKEN_VARS if surviving.get(var)), None)
    if source is None:
        source = _oauth_account(home / ".claude.json")
    if source is None:
        identity = next((i for name in CREDENTIAL_FILES
                         if (i := login_identity(claude_home / name)) not in (b"absent", b"unreadable")), None)
        source = identity.decode(errors="replace") if identity else None
    return f"acct-{hashlib.sha256(source.encode()).hexdigest()[:12]}" if source else None


def api_key_login(environ: Mapping[str, str] | None = None) -> bool:
    """Whether claude runs on an API key (billed per token): the key survives
    today's auth function (``apply_auth``), which drops it when a usable
    native login is there."""
    return bool(_surviving(environ).get("ANTHROPIC_API_KEY"))


def _oauth_account(config: Path) -> str | None:
    try:
        account = json.loads(config.read_text(encoding="utf-8")).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return None
    uuid = account.get("accountUuid") if isinstance(account, dict) else None
    return f"oauth:{uuid}" if isinstance(uuid, str) and uuid else None


def login_identity(path: Path) -> bytes:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return b"absent"
    except (OSError, ValueError):
        return b"unreadable"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        oauth = data if isinstance(data, dict) else {}
    key = oauth.get("refreshToken") or oauth.get("accessToken") or ""
    return f"{key}\0{oauth.get('subscriptionType')}".encode()
