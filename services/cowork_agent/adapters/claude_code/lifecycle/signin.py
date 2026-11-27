"""claude's sign-in, ``claude auth login --claudeai``: it prints ``If the
browser didn't open, visit: <url>`` and reads the ``code#state`` Claude's page
shows; a wrong one prints ``Invalid code…`` and it reads again. It runs even
over a login already there (to repair it or switch accounts); success is its
exit 0 with its own login there, so its own writes are done before ``finish``
marks onboarding done (a terminal ``claude`` then skips its first-run login
screens).

``LEGACY`` renders the events as ``/connect/claude-code`` always answered."""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from services.cowork_agent.adapters.claude_code.adapter import config_file
from services.cowork_agent.adapters.claude_code.lifecycle.login import TOKEN_VARS, native_login
from services.runtime_lifecycle.contract import Legacy, SignIn
from services.runtime_lifecycle.views import SUCCESS_LINE, render
from services.storage.atomic_write import write_text_durable

log = logging.getLogger(__name__)

_VISIT_URL = re.compile(r"visit:\s*(https://[^\s\"'<>\x00-\x1f]+)", re.IGNORECASE)
_AUTH_URL = re.compile(r"https://[^\s\"'<>\x00-\x1f]+/oauth/authorize\?[^\s\"'<>\x00-\x1f]+")


def auth_url(line: str) -> str | None:
    """The authorize link after ``visit:`` (else a bare one), each parameter once."""
    found = _VISIT_URL.search(line)
    raw = found.group(1) if found else (m.group(0) if (m := _AUTH_URL.search(line)) else None)
    if not raw:
        return None
    second = raw.find("https://", len("https://"))      # a redraw concatenated onto the link
    split = urlsplit(raw[:second] if second != -1 else raw)
    params: dict[str, str] = {}
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        params.setdefault(key, "".join(ch for ch in value if ord(ch) >= 0x20))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(params), "")) if params else None


def code(pasted: str) -> str:
    """``code#state`` (what the CLI reads) from itself, a landing-page URL or ``code=…&state=…``."""
    value = (pasted or "").strip()
    if value.startswith(("http://", "https://")):
        params = parse_qs(urlparse(value).query)
    elif "code=" in value and "state=" in value:
        params = parse_qs(value)
    else:
        return value
    found, state = (params.get("code") or [None])[0], (params.get("state") or [None])[0]
    return f"{found}#{state}" if found and state else found or value


def mark_onboarded() -> None:
    """Merge ``hasCompletedOnboarding: true`` into the CLI's ``.claude.json``; an unreadable one is left alone."""
    path = config_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        log.warning("claude sign-in: %s unreadable (%s); onboarding left unmarked", path, exc)
        return
    if not isinstance(data, dict) or data.get("hasCompletedOnboarding") is True:
        return
    try:
        write_text_durable(path, json.dumps({**data, "hasCompletedOnboarding": True}, indent=2))
    except OSError as exc:
        log.warning("claude sign-in: couldn't mark onboarding in %s (%s)", path, exc)


async def legacy_view(flow: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """The old ``/connect/claude-code`` frames: the link echoed after ``auth_url``,
    ``code_rejected`` on a wrong code, the success line as soon as claude says
    so, a bare ``done``, ``error`` with claude's own failure, a ``ping`` after
    each heartbeat."""
    failure, success_sent = None, False
    async for event in flow:
        kind = event["type"]
        if kind == "line":
            line = event["line"].strip()
            if "Login failed" in line:
                failure = line[line.find("Login failed"):]
            if "Invalid code" in line:
                yield {"type": "code_rejected", "retryable": True, "message": line[line.find("Invalid code"):]}
            if "Login successful" in line and not success_sent:
                success_sent = True
                yield {"type": "stdout", "line": SUCCESS_LINE}
            if event["auth_url"]:
                yield {"type": "auth_url", "url": event["auth_url"]}
                yield {"type": "stdout", "line": event["auth_url"]}
            elif line and not event["link"]:                  # a redraw of the link is dropped
                yield {"type": "stdout", "line": event["line"]}
        elif kind == "signed_in":
            if not (success_sent or event["already"]):
                yield {"type": "stdout", "line": SUCCESS_LINE}
            yield {"type": "done", "returncode": 0}
        elif kind == "failed":
            yield {"type": "error", "error": failure or "Login failed. Please try again."}
        elif kind == "error":
            yield {"type": "error", "error": "Setup timed out" if event.get("code") == "timeout" else event["message"]}
        else:
            for out in render(event):
                yield out
            if kind == "heartbeat":
                yield {"type": "ping"}


SIGN_IN = SignIn(style="paste_code", argv=("auth", "login", "--claudeai"), auth_url=auth_url, code=code,
                 unset=TOKEN_VARS, finish=mark_onboarded, landed=native_login, relogin=True)
LEGACY = Legacy(view=legacy_view, code_sent="Code sent to CLI")
