"""Redaction of what XO itself stores (storm ch 16 §3; §0.76).

What is replaced with ``[redacted]``:

* every value in the Space's secrets store (``load_env_entries()``), and the
  live ``os.environ`` value of each of its keys (the value a session gets);
* the live values of the CLIs' auth variables, as each runtime names them in
  its feature list (``auth_env``), so core names no CLI;
* every live session's comms token (the engine adds it when it mints it and
  drops it once the process is gone);
* the known token shapes ``TOKEN_SHAPES``: ``sk-ant-…``, ``sk-…``, GitHub's
  ``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``/``github_pat_`` tokens, and the
  credential after ``Bearer`` (the word itself stays, so the text still reads).

Values are matched exactly, as written and as JSON writes them (a raw turn
line quotes a value with its quotes and backslashes escaped). A value shorter
than ``MIN_SECRET_LENGTH`` is never matched: the store also holds settings
(``1``, ``true``, a region, a short id), and matching those would shred
ordinary text, while the credentials it is for are longer.

**One matcher** serves every call: the exact values, searched as substrings
(a regex alternation of them costs about ten times as much per character),
and one compiled pattern of the shapes. It is rebuilt only when the set of
secrets changes, which each call checks without reading a file: the store
file's identity (``os.stat``: inode, size, mtime; every save replaces the
file), the live values of the auth variables and the tokens' version. A value
added to the store is caught on the next call.

Where it applies (the callers, ch 16 §3): every event the state log keeps
(``Tx.append``), the rows that hold words someone wrote (messages, hops'
notes, threads' titles and status, review items, run titles and summaries),
the raw per-turn CLI output files, and storm's own server logs
(``redact_logs``). Never the CLIs' transcripts or the live stream to the
screen (ephemeral events), which XO doesn't keep.

Nothing here logs what it matched.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Iterable, Iterator, TypeVar

from services.cowork_agent.registry import agent_env

log = logging.getLogger(__name__)

REDACTED = "[redacted]"
#: The shortest value matched exactly; see the module's docstring.
MIN_SECRET_LENGTH = 12
#: Credentials recognised by their shape wherever they come from. Each starts
#: with a literal letter, then checks that no letter or digit comes before it
#: (``(?<!…)``), so the pattern skips ahead by first letter and ``task-…``
#: isn't taken for a key. The credential after ``Bearer`` keeps the word.
TOKEN_SHAPES = (
    r"s(?<![A-Za-z0-9]s)k-ant-[A-Za-z0-9_-]{8,}",          # Anthropic API keys and OAuth tokens
    r"s(?<![A-Za-z0-9]s)k-[A-Za-z0-9_-]{20,}",             # OpenAI-style API keys
    r"g(?<![A-Za-z0-9]g)h[pousr]_[A-Za-z0-9]{20,}",        # GitHub classic tokens
    r"g(?<![A-Za-z0-9]g)ithub_pat_[A-Za-z0-9_]{20,}",      # GitHub fine-grained tokens
    r"B(?<![A-Za-z0-9]B)earer[ \t]+[A-Za-z0-9._~+/-]{16,}=*",
    r"b(?<![A-Za-z0-9]b)earer[ \t]+[A-Za-z0-9._~+/-]{16,}=*",
    r"B(?<![A-Za-z0-9]B)EARER[ \t]+[A-Za-z0-9._~+/-]{16,}=*",
)
_SHAPES = re.compile("|".join(TOKEN_SHAPES))
_SCHEME = re.compile(r"(?:[Bb]earer|BEARER)[ \t]+")
#: The loggers whose records are redacted from this module's import on (boot
#: recovery's included): storm's services and routes. The engine adds each
#: runtime's package (``redact_logs``).
STORM_LOGGERS = ("services.cowork_agent.storm", "routers.cowork_agent.storm")
#: Joins a value's strings for one search; no secret or shape holds it.
_JOIN = "\x00"

T = TypeVar("T")


def _replace(match: re.Match) -> str:
    scheme = _SCHEME.match(match.group(0))
    return scheme.group(0) + REDACTED if scheme else REDACTED


def _variants(value: str) -> set[str]:
    """A value as written and as JSON writes it (escaped, with and without
    non-ASCII escaped)."""
    return {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}


class _Matcher:
    """One secret set, ready to search: the exact values (longest first, so
    where one secret holds another the whole of the longer one goes) through
    substring search, which is far faster than a regex alternation of them,
    and the shapes through one pattern."""

    def __init__(self, values: Iterable[str]) -> None:
        variants = {v for value in values for v in _variants(value)}
        self.literals = tuple(sorted(variants, key=lambda v: (-len(v), v)))

    def found(self, text: str) -> bool:
        return any(secret in text for secret in self.literals) or _SHAPES.search(text) is not None

    def sub(self, text: str) -> str:
        for secret in self.literals:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return _SHAPES.sub(_replace, text)


class _Secrets:
    """The secret set and its matcher; safe to use from any thread (the
    store's, the event loop, the raw turn logs' writers)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._env_names: tuple[str, ...] = ()
        self._tokens: set[str] = set()
        self._tokens_version = 0
        self._unreadable: tuple | None = None       # the store file the last warning was about
        self._built: tuple[Any, _Matcher] = (None, _Matcher(()))

    def matcher(self) -> _Matcher:
        built_key, matcher = self._built
        if self._fingerprint(self._stamp()) == built_key:
            return matcher
        warning = None
        with self._lock:
            # The file's identity is taken before it is read: a save after
            # this changes it, so the next call reads the store again.
            stamp = self._stamp()
            key = self._fingerprint(stamp)
            if key != self._built[0]:
                values, warning = self._values(stamp)
                self._built = (key, _Matcher(values))
            matcher = self._built[1]
        if warning:     # logged outside the lock: this module's own records are redacted too
            log.warning("storm: redaction can't read the secrets store (%s); its values are matched again once it "
                        "can be read", warning)
        return matcher

    def watch_env(self, names: Iterable[str]) -> None:
        with self._lock:
            self._env_names = tuple(sorted(set(names)))

    def add_token(self, token: str) -> None:
        with self._lock:
            self._tokens.add(token)
            self._tokens_version += 1

    def drop_token(self, token: str) -> None:
        with self._lock:
            self._tokens.discard(token)
            self._tokens_version += 1

    @staticmethod
    def _stamp() -> tuple | None:
        """The store file's identity, without opening it."""
        try:
            st = os.stat(agent_env.ENV_FILE)
        except OSError:
            return None
        return str(agent_env.ENV_FILE), st.st_ino, st.st_size, st.st_mtime_ns

    def _fingerprint(self, stamp: tuple | None) -> tuple:
        """What the matcher is built from: the store file, the watched
        variables' live values, the tokens. (The live values of the store's
        own keys are read with the file: the store sets them when it saves.)"""
        environ = os.environ
        return stamp, self._env_names, tuple(environ.get(name) for name in self._env_names), self._tokens_version

    def _values(self, stamp: tuple | None) -> tuple[list[str], str | None]:
        """Every secret value now, with the lock held; and why the store
        couldn't be read, the first time it can't."""
        warning = None
        try:
            entries = agent_env.load_env_entries()
        except OSError as exc:
            entries = []
            if stamp != self._unreadable:
                self._unreadable, warning = stamp, exc.strerror or type(exc).__name__
        store = {e["key"]: e["value"] for e in entries if (e.get("key") or "").strip()}
        environ = os.environ
        values = [*store.values(), *(environ.get(k) for k in store), *(environ.get(n) for n in self._env_names),
                  *self._tokens]
        return [v for v in values if v and len(v) >= MIN_SECRET_LENGTH], warning


_secrets = _Secrets()


def redact(text: str) -> str:
    """``text`` with every secret replaced by ``[redacted]``."""
    matcher = _secrets.matcher()
    return matcher.sub(text) if matcher.found(text) else text


def redact_data(value: T) -> T:
    """A JSON-like value (dicts, lists, tuples, strings) with every string in
    it redacted; the same object when nothing matched. Its strings are
    searched together, once; only a value with a match is rebuilt."""
    matcher = _secrets.matcher()
    if not matcher.found(_JOIN.join(_strings(value))):
        return value
    return _walk(value, matcher)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _walk(value: Any, matcher: _Matcher) -> Any:
    if isinstance(value, str):
        return matcher.sub(value) if matcher.found(value) else value
    if isinstance(value, dict):
        return {k: _walk(v, matcher) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_walk(v, matcher) for v in value]
        return tuple(items) if isinstance(value, tuple) else items
    return value


def watch_env(names: Iterable[str]) -> None:
    """The variables whose live values are secrets: the CLIs' auth variables
    (each runtime's ``auth_env``)."""
    _secrets.watch_env(names)


def add_token(token: str) -> None:
    """A live session's comms token: redacted until ``drop_token``."""
    _secrets.add_token(token)


def drop_token(token: str) -> None:
    _secrets.drop_token(token)


# ── the server logs ──────────────────────────────────────────────────────────

_log_names: frozenset[str] = frozenset()
_log_children: tuple[str, ...] = ()
_factory_installed = False


def redact_logs(loggers: Iterable[str]) -> None:
    """Redact every record of these loggers and of the loggers below them
    (added to the ones named before). A filter on a logger never sees its
    children's records, so this hooks the record factory instead: a record of
    one of these loggers is formatted and redacted as it is made, before any
    handler or filter sees it."""
    global _log_names, _log_children, _factory_installed
    _log_names = _log_names | {name.rstrip(".") for name in loggers}
    _log_children = tuple(name + "." for name in sorted(_log_names))
    if _factory_installed:
        return
    _factory_installed = True
    previous = logging.getLogRecordFactory()

    def make(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if record.name in _log_names or record.name.startswith(_log_children):
            _redact_record(record)
        return record
    logging.setLogRecordFactory(make)


def _redact_record(record: logging.LogRecord) -> None:
    try:
        message = record.getMessage()
    except (TypeError, ValueError):
        return              # a malformed call: logging reports it when the record is emitted
    record.msg, record.args = redact(message), None
    if record.exc_info and not record.exc_text:
        record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


redact_logs(STORM_LOGGERS)
