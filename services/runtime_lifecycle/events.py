"""Who hears that an action through XO changed a runtime: ``on_change``
listeners (the advisory re-reads it), and ``on_sign_in`` listeners for a
sign-in to an account of a kind (``"openai"``), so no adapter names another."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Listener = Callable[[str], Awaitable[Any] | None]
_on_change: list[Listener] = []
_on_sign_in: dict[str, list[Listener]] = {}


def on_change(listener: Listener) -> None:
    _on_change.append(listener)


def on_sign_in(kind: str, listener: Listener) -> None:
    _on_sign_in.setdefault(kind, []).append(listener)


async def changed(runtime: str, *, signed_in: str | None = None) -> None:
    """``runtime`` changed; ``signed_in`` is its account kind when the change was a sign-in."""
    for listener in [*_on_change, *(_on_sign_in.get(signed_in, ()) if signed_in else ())]:
        try:
            result = listener(runtime)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - one listener's failure must not hide the change from the rest
            log.exception("runtime lifecycle: a listener failed on %s's change", runtime)
