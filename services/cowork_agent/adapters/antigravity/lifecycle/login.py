"""Is agy signed in, and as whom: read from agy's own token file and key
variables only, never a vendor call (storm ch 04 §10).

* ``logged_in``: the token file holds a refresh token (agy refreshes the
  access token itself) or an access token not yet expired, or ADC or a
  gateway key is configured.
* ``login_fingerprint``: a digest of who agy is signed in as (its token
  file's sign-in method and account email, and the key variables). agy
  rewrites the file each time it refreshes its access token (hourly), so the
  tokens themselves are left out: a refresh is not a new login. A session
  compares it at each turn boundary; a change means the person connected,
  disconnected or switched accounts, and the engine recycles the session.
* ``account_fingerprint``: a stable, non-secret label of the account, so the
  engine keys its pool by agent × account.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: The variables that carry agy's credentials when set (``auth_env``): storm
#: redacts their values from what it stores.
AUTH_VARS = ("GEMINI_API_KEY", "AGY_LLM_GATEWAY_API_KEY")
#: The variables that log agy in ahead of its token file: a gateway key or Google Cloud credentials, billed per use.
KEY_LOGIN_VARS = ("AGY_ADC_AUTH", "AGY_LLM_GATEWAY_API_KEY")


def token_file(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    home = Path(environ.get("HOME") or str(Path.home()))
    return home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def _token(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(token_file(environ).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def _identity(environ: Mapping[str, str] | None) -> str:
    data = _token(environ)
    if data is None:
        return "absent"
    if data.get("unreadable"):
        return "unreadable"
    token = data.get("token") if isinstance(data.get("token"), dict) else {}
    return "\0".join((str(data.get("auth_method") or ""), str(data.get("id_token_email") or ""),
                      "refresh" if token.get("refresh_token") else "access" if token.get("access_token") else ""))


def login_fingerprint(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    keys = "\0".join(hashlib.sha256(environ[k].encode()).hexdigest() if environ.get(k) else "" for k in AUTH_VARS)
    return hashlib.sha256(f"{_identity(environ)}\0{keys}".encode()).hexdigest()


def account_fingerprint(environ: Mapping[str, str] | None = None) -> str | None:
    """A label of the account that is no credential: a digest of the email
    (or of the key in use), None when agy isn't signed in."""
    environ = os.environ if environ is None else environ
    data = _token(environ)
    email = data.get("id_token_email") if isinstance(data, dict) else None
    if isinstance(email, str) and email:
        return "agy:" + hashlib.sha256(email.encode()).hexdigest()[:16]
    for key in AUTH_VARS:
        if environ.get(key):
            return "agy-key:" + hashlib.sha256(environ[key].encode()).hexdigest()[:16]
    return None


def api_key_login(environ: Mapping[str, str] | None = None) -> bool:
    """Whether agy runs on a key or cloud credentials (billed per use), not its own sign-in."""
    environ = os.environ if environ is None else environ
    return any(environ.get(var) for var in KEY_LOGIN_VARS)


def logged_in(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    if api_key_login(environ):
        return True
    data = _token(environ)
    if not isinstance(data, dict) or data.get("unreadable"):
        return False
    token = data.get("token") if isinstance(data.get("token"), dict) else {}
    if token.get("refresh_token"):
        return True
    return bool(token.get("access_token")) and not _expired(token.get("expiry"))


def _expired(stamp: object) -> bool:
    try:
        expiry = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if isinstance(stamp, str) else None
    except ValueError:
        return False
    if expiry is None:
        return False
    return (expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc)
