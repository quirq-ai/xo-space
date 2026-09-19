"""The shared test harness: one sandbox, one client builder, one fake stream.

New tests use these instead of writing their own ``tempfile`` plus
``patch.dict(os.environ)`` setUp, their own ``FastAPI()`` plus ``TestClient``
builder, or their own async generator standing in for an agent. The reasons
are the usual ones: every private sandbox patched a slightly different set of
variables (one forgot ``QUIRQ_COMMAND_LOG_PATH``, another pointed the state
root at the temp dir itself), every private client builder forgot to install
the service error handler once ``routers/errors.py`` existed, and every fake
stream drifted a little from the shape the dispatcher promises. Fixing one
place is cheaper than fixing thirty.

What is here
------------

:class:`Sandbox`
    A machine-local state root and a projects root in a temp dir, with the
    environment pointed at them for the length of one test::

        def setUp(self) -> None:
            self.sandbox = Sandbox(self)

    By default the roots start from the golden samples: ``tests/fixtures/
    quirq-state`` is copied to ``<tmp>/state`` (its README left out, so the
    root holds exactly the sample's folders) and ``tests/fixtures/xo-project``
    to ``<tmp>/projects/sample-project``. ``Sandbox.fresh(self)`` (or
    ``copy_fixtures=False``) starts from two empty roots instead, for tests
    that lay out their own files or exercise migrations. The environment
    patched is ``QUIRQ_STATE_ROOT``, ``XO_PROJECTS_ROOT``, and the four
    overrides that would otherwise let a store escape the sandbox
    (``QUIRQ_RUNTIME_FILE``, ``QUIRQ_SECRETS_FILE``, ``QUIRQ_COMMAND_LOG``,
    ``QUIRQ_COMMAND_LOG_PATH``, all set to ""). ``env=`` adds or overrides
    variables for the same span. Cleanup (restoring the environment, removing
    the temp dir) is registered with ``case.addCleanup``, so it runs even when
    the test fails, and in the right order.

    Attributes: ``state`` (the state root, what ``quirq_state_dir()``
    answers), ``projects`` (the projects root), ``project`` (the sample
    project folder inside it; exists only when the fixtures were copied) and
    ``base`` (the temp dir holding both, for a scratch file that belongs to
    neither root).

:class:`SandboxTestCase`
    The same as a base class, for a module whose every test wants a sandbox:
    set ``copy_fixtures`` and ``sandbox_env`` on the subclass and read
    ``self.sandbox`` in the tests.

:func:`client`
    ``client(router_a, router_b)`` builds a ``FastAPI()`` with those routers,
    installs the app-wide service error handler (so a
    :class:`services.errors.ServiceError` raised anywhere on the request path
    answers ``{"detail": {"code", "message"}}`` with the raiser's status,
    exactly as the real server does) and returns a ``TestClient``. Pass
    ``app=`` to include the routers on an app you built, and any
    ``TestClient`` keyword (``base_url``, ``client``) straight through.

:func:`fake_stream`
    ``fake_stream(["hel", "lo"])`` returns an async generator function with
    the dispatcher's ``stream(question, session_id=None, **kwargs)``
    signature that yields the shape ``BaseAgentAdapter.stream`` promises:
    ``{"type": "token", "token": t}`` per token, then, when ``error=`` was
    given, ``{"type": "error", "error": error}``, then exactly one
    ``{"done": True, "native_session_id": ...}``. Patch it in as an adapter's
    or a dispatcher's ``stream``; the ``native_session_id`` defaults to
    ``"native-1"`` and may be ``None``.

:func:`collect`
    Runs an async iterator to a list from synchronous test code, for reading
    what a stream produced.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Optional
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.errors import install_service_errors

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
STATE_FIXTURE = FIXTURES / "quirq-state"
PROJECT_FIXTURE = FIXTURES / "xo-project"
SAMPLE_PROJECT = "sample-project"

#: The overrides every sandbox blanks, so no store can read or write outside
#: the temp roots however the developer's shell is configured.
SANDBOX_ENV: Mapping[str, str] = {
    "QUIRQ_RUNTIME_FILE": "",
    "QUIRQ_SECRETS_FILE": "",
    "QUIRQ_COMMAND_LOG": "",
    "QUIRQ_COMMAND_LOG_PATH": "",
}


def _without_top_level_readme(fixture: Path) -> Callable[[str, list[str]], set[str]]:
    """``copytree`` ignore hook: leave the fixture's own README behind. It
    documents the sample; it is not part of the data the sample shows."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        return {"README.md"} if Path(directory) == fixture and "README.md" in names else set()

    return ignore


class Sandbox:
    """A temp state root and projects root, with the environment pointed at
    them until ``case`` is cleaned up. See the module docstring."""

    def __init__(
        self,
        case: unittest.TestCase,
        *,
        copy_fixtures: bool = True,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.state = self.base / "state"
        self.projects = self.base / "projects"
        self.project = self.projects / SAMPLE_PROJECT
        self.copied_fixtures = copy_fixtures
        if copy_fixtures:
            shutil.copytree(STATE_FIXTURE, self.state, ignore=_without_top_level_readme(STATE_FIXTURE))
            shutil.copytree(PROJECT_FIXTURE, self.project, ignore=_without_top_level_readme(PROJECT_FIXTURE))
        else:
            self.state.mkdir()
            self.projects.mkdir()
        values = {
            "QUIRQ_STATE_ROOT": str(self.state),
            "XO_PROJECTS_ROOT": str(self.projects),
            **SANDBOX_ENV,
            **dict(env or {}),
        }
        patcher = patch.dict(os.environ, values)
        patcher.start()
        case.addCleanup(patcher.stop)

    @classmethod
    def fresh(cls, case: unittest.TestCase, *, env: Optional[Mapping[str, str]] = None) -> "Sandbox":
        """Two empty roots instead of the samples."""
        return cls(case, copy_fixtures=False, env=env)

    def __repr__(self) -> str:
        return f"Sandbox(state={str(self.state)!r}, projects={str(self.projects)!r})"


class SandboxTestCase(unittest.TestCase):
    """A ``TestCase`` whose ``setUp`` makes ``self.sandbox``.

    Subclasses set ``copy_fixtures`` (default ``True``) and ``sandbox_env``
    (extra or overriding variables). A subclass that adds its own ``setUp``
    calls ``super().setUp()`` first."""

    copy_fixtures: bool = True
    sandbox_env: Mapping[str, str] = {}
    sandbox: Sandbox

    def setUp(self) -> None:
        super().setUp()
        self.sandbox = Sandbox(self, copy_fixtures=self.copy_fixtures, env=dict(self.sandbox_env))


def client(*routers: Any, app: Optional[FastAPI] = None, **client_kwargs: Any) -> TestClient:
    """A ``TestClient`` over ``routers``, with the service error handler the
    real server installs. ``client_kwargs`` go to ``TestClient``."""
    app = app if app is not None else FastAPI()
    for router in routers:
        app.include_router(router)
    install_service_errors(app)
    return TestClient(app, **client_kwargs)


StreamFactory = Callable[..., AsyncIterator[dict[str, Any]]]


def fake_stream(
    tokens: Iterable[str] | str,
    native_session_id: Optional[str] = "native-1",
    error: Optional[str] = None,
) -> StreamFactory:
    """An async generator function yielding the dispatcher stream shape.

    ``tokens`` is one token when a string, else each item is a token. The
    function accepts and ignores the dispatcher's positional and keyword
    arguments, so it can replace ``adapter.stream`` or ``dispatcher.stream``
    directly. Each call produces a fresh, independent stream."""
    items = [tokens] if isinstance(tokens, str) else list(tokens)

    async def stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for token in items:
            yield {"type": "token", "token": token}
        if error is not None:
            yield {"type": "error", "error": error}
        yield {"done": True, "native_session_id": native_session_id}

    return stream


def collect(events: AsyncIterator[dict[str, Any]] | Awaitable[Any]) -> list:
    """Drain an async iterator (or await a coroutine) from synchronous code."""

    async def drain() -> list:
        if hasattr(events, "__aiter__"):
            return [event async for event in events]
        result = await events  # type: ignore[misc]
        return list(result) if isinstance(result, (list, tuple)) else [result]

    return asyncio.run(drain())


__all__ = [
    "FIXTURES", "PROJECT_FIXTURE", "ROOT", "SAMPLE_PROJECT", "SANDBOX_ENV", "STATE_FIXTURE",
    "Sandbox", "SandboxTestCase", "client", "collect", "fake_stream",
]
