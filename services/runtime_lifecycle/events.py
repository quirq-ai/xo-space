"""Who hears that an action through XO changed a runtime: ``on_change``
listeners (the advisory re-reads it), and, on a sign-in, the Space agent's
``account_listeners`` for that account kind (``ON_SIGN_IN = {"openai": fn}``),
each given the runtime and its ``Credential``, so no adapter names another."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

from services.cowork_agent.adapters.describe_once import forget as forget_check
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
    """``runtime`` changed; ``signed_in`` is its account kind when the change was a sign-in. First its
    lifecycle drops any login answer it keeps (``LifecycleSpec.forget``) and a check already running is
    shared no more (``describe_once.forget``), so no listener reads an answer from before."""
    _forget(runtime)
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


def _forget(runtime: str) -> None:
    forget_check(runtime)
    try:
        lifecycle = try_load_capability("lifecycle", agent=runtime)
        if lifecycle is not None:
            lifecycle.SPEC.forget()
    except Exception:  # noqa: BLE001 - a kept answer it can't drop must not hide the change from the listeners
        log.exception("runtime lifecycle: %s's kept login couldn't be dropped", runtime)
