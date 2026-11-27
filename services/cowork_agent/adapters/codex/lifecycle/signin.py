"""codex's sign-in, ``codex login --device-auth``: it prints a link and a
one-time code, and waits until the person has entered it. It runs even over
a login already there (to refresh the token the ``openai`` listeners are
given, or to move from an API key to ChatGPT); success is its exit 0 with
``auth.json`` holding a login.

``LEGACY`` renders the events as ``/connect/codex`` always answered: every
line as ``stdout`` (blank ones and the link's too), then ``done`` with the
CLI's exit code."""

from __future__ import annotations

import re
from typing import AsyncIterator

from services.cowork_agent.adapters.codex.lifecycle.login import signed_in
from services.runtime_lifecycle.contract import Legacy, SignIn

_LINK = re.compile(r"https://[^\s\"'<>\x00-\x1f]+")


def auth_url(line: str) -> str | None:
    found = _LINK.search(line)
    return found.group(0) if found else None


async def legacy_view(flow: AsyncIterator[dict]) -> AsyncIterator[dict]:
    async for event in flow:
        kind = event["type"]
        if kind == "line":
            yield {"type": "stdout", "line": event["line"]}
        elif kind == "signed_in":
            yield {"type": "done", "returncode": 0}
        elif kind == "failed":
            yield {"type": "done", "returncode": event["returncode"]}
        elif kind == "error":
            yield {"type": "error", "error": "Login timed out" if event.get("code") == "timeout" else event["message"]}
        elif kind == "heartbeat":
            yield event


SIGN_IN = SignIn(style="device_code", argv=("login", "--device-auth"), auth_url=auth_url, landed=signed_in,
                 relogin=True)
LEGACY = Legacy(view=legacy_view)
