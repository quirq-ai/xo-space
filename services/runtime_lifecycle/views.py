"""The sign-in's events as the engine's own frames (``standard``), and the
pieces a runtime's ``Legacy.view`` reuses. No FastAPI: an adapter's
``lifecycle/`` imports this, and its ``runtime/`` imports that."""

from __future__ import annotations

from typing import AsyncIterator, Iterable

#: The deployed xo-swarm dialogs flip to success only on this stdout line; it precedes ``done``.
SUCCESS_LINE = "Authentication token created successfully"
EXPIRED_LINE = "Sign-in link expired — issuing a fresh one…"


def error_event(message: str, **extra) -> dict:
    """``message`` under both keys: the xo-swarm dialogs read ``message``, others ``error``."""
    return {"type": "error", "error": message, "message": message, **extra}


def render(event: dict) -> Iterable[dict]:
    """One event as the engine's frames."""
    kind = event["type"]
    if kind == "line":
        if event["auth_url"]:
            yield {"type": "auth_url", "url": event["auth_url"]}
        elif event["line"].strip():
            yield {"type": "stdout", "line": event["line"]}
    elif kind == "expired":
        yield {"type": "stdout", "line": EXPIRED_LINE}
    elif kind == "signed_in":
        if not event["already"]:
            yield {"type": "stdout", "line": SUCCESS_LINE}
        yield {"type": "done", "returncode": 0, **({} if event["already"] else {"ok": True})}
    elif kind == "failed":
        yield error_event(event["message"])
    else:
        yield event


async def standard(flow: AsyncIterator[dict]) -> AsyncIterator[dict]:
    async for event in flow:
        for out in render(event):
            yield out
