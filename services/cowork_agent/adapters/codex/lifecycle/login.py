"""Is codex signed in, and with what: read from ``$CODEX_HOME/auth.json``
only, never a vendor call. The key variables are the runtime's to add
(``runtime/account.py``): Connect signs codex's own login in."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from services.runtime_lifecycle.contract import Credential

AUTH_FILE = "auth.json"
#: The variables that log codex in, whatever the person's config adds (``credential_vars``).
AUTH_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """The codex home this runtime runs on: ``$CODEX_HOME``, else ``~/.codex``
    under the live ``HOME``."""
    environ = os.environ if environ is None else environ
    configured = (environ.get("CODEX_HOME") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    home = environ.get("HOME") or str(Path.home())
    return Path(home) / ".codex"


def read_auth(home: Path) -> dict[str, Any] | None:
    """``auth.json``; None when absent, ``{"unreadable": True}`` when broken."""
    try:
        data = json.loads((home / AUTH_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def _tokens(auth: Mapping[str, Any] | None) -> dict[str, Any]:
    tokens = auth.get("tokens") if auth else None
    return tokens if isinstance(tokens, dict) else {}


def signed_in(home: Path | None = None) -> bool:
    """codex's own login file holds ChatGPT tokens or an API key."""
    auth = read_auth(home or codex_home())
    if not auth or auth.get("unreadable"):
        return False
    tokens = _tokens(auth)
    return bool(tokens.get("refresh_token") or tokens.get("access_token") or auth.get("OPENAI_API_KEY"))


def identity(home: Path) -> str:
    """Who ``auth.json`` logs in (its mode, the ChatGPT account id, a key's digest); ``absent`` or ``unreadable``."""
    auth = read_auth(home)
    if auth is None:
        return "absent"
    if auth.get("unreadable"):
        return "unreadable"
    tokens = _tokens(auth)
    key = auth.get("OPENAI_API_KEY")
    return "\0".join((str(auth.get("auth_mode") or ""), str(tokens.get("account_id") or ""),
                      hashlib.sha256(key.encode()).hexdigest() if isinstance(key, str) and key else ""))


def account_fingerprint(home: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Which account codex runs on, as ``acct-<12 hex>``; None when nothing
    logs it in. Blocking: call it in a thread."""
    environ = os.environ if environ is None else environ
    found = identity(home)
    if found in ("absent", "unreadable"):
        key = next((environ[v] for v in AUTH_VARS if environ.get(v)), None)
        found = f"key:{hashlib.sha256(key.encode()).hexdigest()}" if key else ""
    return f"acct-{hashlib.sha256(found.encode()).hexdigest()[:12]}" if found else None


def api_key_login(home: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Whether codex runs on an API key (billed per token): its login file
    says so (``auth_mode`` apikey, or a key and no ChatGPT tokens), or there
    is no login file and a key variable logs it in."""
    environ = os.environ if environ is None else environ
    auth = read_auth(home)
    if auth and not auth.get("unreadable"):
        tokens = _tokens(auth)
        mode = str(auth.get("auth_mode") or "").lower()
        return mode == "apikey" or (not mode and bool(auth.get("OPENAI_API_KEY"))
                                    and not (tokens.get("refresh_token") or tokens.get("access_token")))
    return auth is None and any(environ.get(v) for v in AUTH_VARS)


def logged_in(home: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Its login file, or a key variable."""
    environ = os.environ if environ is None else environ
    return signed_in(home) or any(environ.get(v) for v in AUTH_VARS)


def _jwt_claims(token: object) -> dict[str, Any]:
    """A JWT's payload, unverified (it is codex's own file); ``{}`` when it isn't one."""
    try:
        segment = str(token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def credential() -> Credential | None:
    """The ChatGPT sign-in in ``auth.json``, for the ``openai`` account listeners; ``release`` deletes the file."""
    path = codex_home() / AUTH_FILE
    tokens = _tokens(read_auth(path.parent))
    access = tokens.get("access_token") or tokens.get("id_token")
    if not isinstance(access, str) or not access:
        return None
    claims = _jwt_claims(tokens.get("id_token"))
    exp = claims.get("exp")
    return Credential(access=access, refresh=tokens.get("refresh_token") or None,
                      email=claims.get("email") if isinstance(claims.get("email"), str) else None,
                      expires_ms=int(exp) * 1000 if isinstance(exp, (int, float)) else None,
                      release=lambda: path.unlink(missing_ok=True))
