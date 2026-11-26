"""Who hears that an action through XO changed a runtime: ``on_change``
listeners (the advisory re-reads it), and, on a sign-in, the Space agent's
``account_listeners`` for that account kind (``ON_SIGN_IN = {"openai": fn}``),
each given the runtime and its ``Credential``, so no adapter names another."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

from services.cowork_agent.adapters.loader import try_load_capability
from services.runtime_lifecycle.contract import Credential

log = logging.getLogger(__name__)

Listener = Callable[[str], Awaitable[Any] | None]
_on_change: list[Listener] = []


def on_change(listener: Listener) -> None:
    _on_change.append(listener)


def _sign_in_listener(kind: str | None) -> Callable[[str, Credential | None], Awaitable[Any] | None] | None:
    module = try_load_capability("account_listeners") if kind else None
    return getattr(module, "ON_SIGN_IN", {}).get(kind) if module else None


async def changed(runtime: str, *, signed_in: str | None = None, credential: Credential | None = None) -> None:
    """``runtime`` changed; ``signed_in`` is its account kind when the change was a sign-in."""
    calls: list[Callable[[], Any]] = [lambda listener=listener: listener(runtime) for listener in _on_change]
    if (on_sign_in := _sign_in_listener(signed_in)) is not None:
        calls.append(lambda: on_sign_in(runtime, credential))         # last: a gateway restart is slow
    for call in calls:
        try:
            result = call()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - one listener's failure must not hide the change from the rest
            log.exception("runtime lifecycle: a listener failed on %s's change", runtime)
