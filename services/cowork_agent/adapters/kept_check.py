"""A CLI check kept until what it reads changes.

A check that starts a CLI (``--version``, its sign-in status, its model list)
is kept under a key of what it reads (``stamp``: the binary, the sign-in
files, the variables it runs with) and run again only once that key moves. An
outcome that isn't an answer is kept ``RETRY_AFTER_S``, so a broken CLI isn't
started on every request. ``get`` blocks (call it in a thread) and callers
arriving mid-check share its outcome; ``forget`` never waits: a check running
when it is called is returned but not kept. Not warmed at boot (owner, 11-27).

``KeptProbe`` is the same rule for a check that is a coroutine (a probe a
runtime runs through its ``Launcher``), shared across threads and event loops.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import os
import threading
import time
from typing import Any, Awaitable, Callable, Generic, Hashable, Iterable, Mapping, TypeVar

from services.cowork_agent.adapters.contract import RuntimeFailure

T = TypeVar("T")

#: How long an outcome that isn't an answer is kept before the CLI is asked again (seconds).
RETRY_AFTER_S = 30.0


def stamp(paths: Iterable[str | os.PathLike[str]], env: Mapping[str, str] | None = None) -> tuple[Hashable, ...]:
    """A key of what a check reads: each path as it stands now (its inode,
    modification time and size, or missing; a link is followed, so an update
    that repoints it moves the key), and a digest of ``env``, the variables it
    runs with that change its answer (never their values)."""
    parts: list[Hashable] = []
    for path in paths:
        name = os.path.expanduser(os.fspath(path))
        try:
            st = os.stat(name)
        except OSError:
            parts.append((name, None))
            continue
        parts.append((name, st.st_ino, st.st_mtime_ns, st.st_size))
    digest = hashlib.sha256()
    for key in sorted(env or {}):
        digest.update(key.encode() + b"=" + (env or {})[key].encode() + b"\0")
    parts.append(digest.hexdigest())
    return tuple(parts)


def _found(kept: tuple[Hashable, Any, float, bool] | None, key: Hashable, now: float,
           retry_after_s: float, answer_for_s: float | None = None) -> bool:
    """The one rule: what is kept under ``key`` (key, outcome, kept at, an answer?) stands while the key
    holds (an answer, also only ``answer_for_s`` when given), an outcome that isn't an answer only
    ``retry_after_s``."""
    if kept is None or kept[0] != key:
        return False
    if kept[3]:
        return answer_for_s is None or now - kept[2] < answer_for_s
    return now - kept[2] < retry_after_s


class KeptCheck(Generic[T]):
    """One check's last outcome and the key it was given under.
    ``answered(outcome)`` says whether an outcome is an answer, kept until the
    key moves; any other outcome is kept ``retry_after_s``."""

    def __init__(self, answered: Callable[[T], bool], *, retry_after_s: float = RETRY_AFTER_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._answered = answered
        self._retry_after_s = retry_after_s
        self._clock = clock
        self._lock = threading.Lock()           # guards the fields below; never held through a check
        self._running = threading.Lock()        # one check at a time; the callers behind it share its outcome
        self._epoch = 0                         # moved by forget(): an outcome from before it isn't kept
        self._kept: tuple[Hashable, T, float, bool] | None = None      # key, outcome, kept at, an answer?

    def _kept_for(self, key: Hashable) -> tuple[bool, T | None]:
        if _found(self._kept, key, self._clock(), self._retry_after_s):
            return True, self._kept[1]
        return False, None

    def get(self, key: Hashable, check: Callable[[], T]) -> T:
        """The outcome kept under ``key``, else ``check()``'s, then kept."""
        with self._lock:
            found, outcome = self._kept_for(key)
        if found:
            return outcome
        with self._running:
            with self._lock:
                found, outcome = self._kept_for(key)    # the check this caller waited behind may have answered
                epoch = self._epoch
            if found:
                return outcome
            outcome = check()
            answered = self._answered(outcome)
            with self._lock:
                if self._epoch == epoch:
                    self._kept = (key, outcome, self._clock(), answered)
            return outcome

    def forget(self) -> None:
        """Ask again next time: the CLI was seen to disagree with what is
        kept. Never waits for a running check."""
        with self._lock:
            self._epoch += 1
            self._kept = None


def _value(setting: float | Callable[[], float] | None) -> float | None:
    return setting() if callable(setting) else setting


class KeptProbe(Generic[T]):
    """``KeptCheck``'s rule for a probe that is a coroutine: its answer is kept
    until the key moves; a ``RuntimeFailure`` it raises is kept
    ``retry_after_s`` (0: not kept) and raised again from memory, retryable,
    saying so. One probe at a time per key, shared by the callers that arrive
    while it runs, on any thread or event loop (``/api/models`` runs each on a
    worker thread with its own); a waiter gives up after ``wait_s`` (None: it
    waits as long as the probe) and the probe goes on for the others. A probe
    cancelled or broken by anything but a ``RuntimeFailure`` keeps nothing.
    ``what`` names what it reads, for those messages. ``answer_for_s``: an
    answer is also read again after that long (None: only when the key
    moves). The durations may be callables, read when used. ``forget`` never waits: a probe
    running then is neither kept nor shared with a caller after it."""

    def __init__(self, what: str, *, retry_after_s: float | Callable[[], float] = RETRY_AFTER_S,
                 answer_for_s: float | Callable[[], float] | None = None,
                 wait_s: float | Callable[[], float] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._what = what
        self._retry_after_s, self._answer_for_s = retry_after_s, answer_for_s
        self._wait_s, self._clock = wait_s, clock
        self._lock = threading.Lock()           # guards the fields below; never held across an await
        self._epoch = 0
        self._kept: tuple[Hashable, Any, float, bool] | None = None     # key, outcome or failure, at, an answer?
        self._running: tuple[Hashable, int, concurrent.futures.Future] | None = None

    async def get(self, key: Hashable, probe: Callable[[], Awaitable[T]], *, fresh: bool = False) -> T:
        """The answer kept under ``key``, else ``probe()``'s, then kept.
        ``fresh``: a kept answer isn't taken (a kept failure and a running
        probe still are)."""
        with self._lock:
            kept = self._kept if _found(self._kept, key, self._clock(), _value(self._retry_after_s),
                                        _value(self._answer_for_s)) else None
            if kept is not None and kept[3] and fresh:
                kept = None
            shared = None
            if kept is None:
                running = self._running
                if running is not None and running[0] == key and running[1] == self._epoch:
                    shared = running[2]
                else:
                    epoch, mine = self._epoch, concurrent.futures.Future()
                    self._running = (key, epoch, mine)
        if kept is not None:
            if kept[3]:
                return kept[1]
            failure: RuntimeFailure = kept[1]
            raise RuntimeFailure(failure.code, f"{failure.message} (failed in the last "
                                               f"{_value(self._retry_after_s):g} s; not asked again yet)",
                                 retryable=True)
        if shared is not None:
            return await self._wait(shared)
        try:
            outcome = await probe()
        except BaseException as exc:
            failure = exc if isinstance(exc, RuntimeFailure) else RuntimeFailure(
                "timeout", f"reading {self._what} was cancelled; ask again", retryable=True)
            self._settle(mine, epoch, (key, exc, self._clock(), False) if isinstance(exc, RuntimeFailure) else None)
            mine.set_exception(failure)
            raise
        self._settle(mine, epoch, (key, outcome, self._clock(), True))
        mine.set_result(outcome)
        return outcome

    def _settle(self, mine: concurrent.futures.Future, epoch: int, kept: tuple | None) -> None:
        with self._lock:
            if kept is not None and self._epoch == epoch:
                self._kept = kept
            if self._running is not None and self._running[2] is mine:
                self._running = None

    async def _wait(self, shared: concurrent.futures.Future) -> T:
        """Another caller's probe; the shield keeps a waiter that gives up from cancelling it for the rest."""
        result = asyncio.wrap_future(shared)
        result.add_done_callback(lambda f: f.cancelled() or f.exception())   # retrieved even if we gave up
        try:
            return await asyncio.wait_for(asyncio.shield(result), _value(self._wait_s))
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"{self._what} is still being read; ask again shortly",
                                 retryable=True) from exc

    def forget(self) -> None:
        with self._lock:
            self._epoch += 1
            self._kept = None
            self._running = None


class KeptLogin(Generic[T]):
    """A CLI's login check (a ``probe`` that starts the CLI), kept until what
    it reads moves: the binary, the files its sign-in writes and the key
    variables (``stamp``), and whether the stored login has lapsed by the
    clock, since the CLI may answer otherwise once it has with no file
    changed. Only a definite answer is kept that long (``decided``: signed
    in, or the CLI saying it is signed out); any other outcome (it didn't
    run, timed out, or failed with a network error) is kept
    ``RETRY_AFTER_S``, never as "signed out". Blocking, except ``forget``."""

    def __init__(self, *, binary: Callable[[], str], env: Callable[[], Mapping[str, str]],
                 variables: tuple[str, ...], files: Callable[[Mapping[str, str]], Iterable[os.PathLike[str]]],
                 lapsed: Callable[[Mapping[str, str]], bool | None], probe: Callable[[], T | None],
                 authenticated: Callable[[T | None], bool], decided: Callable[[T | None], bool]) -> None:
        self._binary, self._env, self._variables = binary, env, variables
        self._files, self._lapsed, self._probe, self._authenticated = files, lapsed, probe, authenticated
        self._decided = decided
        self._kept = KeptCheck[T | None](decided)

    def stamp(self) -> tuple:
        """What the check reads (its key)."""
        env = self._env()
        files = stamp([self._binary(), *self._files(env)], {name: env.get(name, "") for name in self._variables})
        return files, self._lapsed(env)

    def result(self) -> T | None:
        """The probe's kept outcome (a CLI that lists models to say it is signed in lists them here too)."""
        return self._kept.get(self.stamp(), self._probe)

    def logged_in(self) -> bool:
        return self._authenticated(self.result())

    def decided(self) -> bool:
        """Whether the CLI answered (signed in or out); False: ``logged_in`` is False for want of an answer."""
        return self._decided(self.result())

    def forget(self) -> None:
        """Check again next time: a session or an action through XO found it changed."""
        self._kept.forget()
