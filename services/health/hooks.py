"""Where the server's own unhandled failures are caught for the record.

- :class:`RecordUnhandledErrors`: a pure ASGI middleware. An exception no
  handler turned into a response is recorded (``http_500``, keyed by the
  route's template, never the raw path) and re-raised, so the response is
  exactly what it was.
- :func:`install_loop_hook`: asyncio's "exception was never retrieved" and
  other loop errors (``crash``), passed on to the previous handler.
- :func:`install_thread_hook`: a thread that died of an exception
  (``crash``), passed on to the previous hook.
- :func:`with_health_record`: wraps the server's lifespan so the run's
  marker is always closed. A shutdown step that raises (awaiting a task that
  had crashed re-raises its error) still ends in a clean-exit mark, and a
  startup that fails is recorded as such, never as an unclean exit.

Nothing here changes what the caller sees; recording never raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any, AsyncIterator, Awaitable, Callable

from services.health import recorder, session

Scope = dict[str, Any]
ASGIApp = Callable[[Scope, Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]]


def _route(scope: Scope) -> str:
    """``GET /api/xo-projects/{project_id}/todos``: the template, so one
    failing route is one record whatever the ids in the URL were."""
    method = scope.get("method", "?")
    route = scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return f"{method} {template}"
    endpoint = scope.get("endpoint")
    name = getattr(endpoint, "__name__", None)
    return f"{method} {name}" if name else f"{method} (no route)"


class RecordUnhandledErrors:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
            recorder.record("http", recorder.HTTP_500, exc=exc, subject=_route(scope))
            raise


def install_loop_hook(loop: asyncio.AbstractEventLoop) -> None:
    previous = loop.get_exception_handler()

    def on_loop_error(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            recorder.record("asyncio", recorder.CRASH, exc=exc)
        else:
            recorder.record("asyncio", recorder.CRASH, error_type="LoopError", message=str(context.get("message", "")))
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(on_loop_error)


def install_thread_hook() -> None:
    previous = threading.excepthook

    def on_thread_error(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not SystemExit and isinstance(args.exc_value, BaseException):
            name = getattr(args.thread, "name", None)
            recorder.record("thread", recorder.CRASH, exc=args.exc_value, subject=name)
        previous(args)

    threading.excepthook = on_thread_error


def with_health_record(lifespan: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """``lifespan`` with this run's health record around it."""

    @contextlib.asynccontextmanager
    async def wrapped(app: Any) -> AsyncIterator[None]:
        session.begin()
        install_loop_hook(asyncio.get_running_loop())
        install_thread_hook()
        alive = asyncio.create_task(session.keep_alive())
        started = False
        try:
            async with lifespan(app):
                started = True
                yield
        except Exception as exc:
            if not started:
                recorder.record("server startup", recorder.CRASH, exc=exc)
            raise
        finally:
            alive.cancel()
            with contextlib.suppress(BaseException):
                await alive
            session.end()

    return wrapped
