"""agy's sign-in, ``agy acp --login``: logged out, it prints ``Open this link
in a browser to log in with Google:`` and a Google OAuth URL, then reads the
pasted authorization code (or the landing page's full URL) from stdin. A
rejected code prints ``couldn't log in: token exchange failed: …
"invalid_grant" …`` and exits 1. Success is agy's token file landing."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from services.cowork_agent.adapters.antigravity.lifecycle.login import own_login
from services.runtime_lifecycle.contract import SignIn

_AUTH_URL = re.compile(r"https://[^\s\"'<>\x00-\x1f]*/o/oauth2/[^\s\"'<>\x00-\x1f]+")
_AUTH_URL_FALLBACK = re.compile(r"https://[^\s\"'<>\x00-\x1f]+redirect_uri=[^\s\"'<>\x00-\x1f]+")


def auth_url(line: str) -> str | None:
    found = _AUTH_URL.search(line) or _AUTH_URL_FALLBACK.search(line)
    if not found:
        return None
    raw = found.group(0)
    second = raw.find("https://", len("https://"))      # a redraw concatenated onto the link
    return raw[:second] if second != -1 else raw


def code(pasted: str) -> str:
    """The bare ``code`` from a raw code, a landing-page URL or ``code=…&state=…``;
    agy holds the PKCE verifier and state itself."""
    value = (pasted or "").strip()
    if value.startswith(("http://", "https://")):
        return (parse_qs(urlparse(value).query).get("code") or [value])[0]
    if "code=" in value:
        return (parse_qs(value).get("code") or [value])[0]
    return value


SIGN_IN = SignIn(style="paste_code", argv=("acp", "--login"), auth_url=auth_url, code=code, landed=own_login,
                 env={"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})        # agy reads "true"; it ignores "1"
