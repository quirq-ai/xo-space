"""
Antigravity (agy) token accounting: agy's own per-call usage records, read from
its SQLite trajectory store.

Tokens are **not** in the JSONL transcript's turns XO reads. They live in
``conversations/<uuid>.db``: table ``gen_metadata`` holds one protobuf row per
model call, table ``steps`` one row per trajectory step. Verified field paths
(agy 1.1.x; each matches the ``input_tokens``/``cache_read_tokens``/
``output_tokens`` agy writes on the call's transcript step, and the usage agy
reports over ACP, to the token):

    gen_metadata.data
      f1.f4         the call's usage, as the model provider reported it:
        f2          fresh (uncached) input tokens
        f3          output tokens (thinking included)
        f4          cache-write tokens
        f5          cache-read tokens
        f7          the provider's message id
        f9          thinking tokens (already inside f3)
      f1.f21|f19    the model's display name (e.g. "Gemini 3.5 Flash (Low)")
      f2            packed step indices the call produced; the first is its
                    PLANNER_RESPONSE step
      f5            the call's error, when it failed
    steps.metadata
      f1.f1/f1.f2   the step's creation time (seconds, nanos)

A call that failed before the provider answered carries no counts at all (its
``f1.f4`` holds only the model): it is **unreported**, never counted as zero
tokens. Each call is dated by its own step. The older reading of ``f1.f9.f10.f1``
was a client-side context-size estimate and is not used.

**WAL gotcha:** the DB is WAL-mode and agy writes rows into the ``-wal``
sidecar. A read-only (``mode=ro``) connection still honors the ``-wal`` when
the ``-shm`` sidecar is readable, so we open **read-only first** and never
mutate agy's store on the accounting path. Only if that read-only path can't
surface the data (it raises, or returns no rows while a non-empty ``-wal``
suggests uncheckpointed rows are hidden) do we fall back to a short read-write
open that runs ``PRAGMA wal_checkpoint(TRUNCATE)`` and re-reads.

A conversation with no store yet has no calls. A store that exists but can't
be read (locked, or an agy update changed its tables) reads as ``UNREAD``, no
calls, and is never kept: it is read again next time.
"""
from __future__ import annotations

import os
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

from services.cowork_agent.adapters.antigravity.paths import conversation_db

# ── Minimal protobuf wire decoder ─────────────────────────────────────────────


def _varint(b: bytes, i: int) -> tuple[int, int]:
    s = r = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7


def _dec(b: bytes) -> list[tuple[int, int, object]]:
    out: list[tuple[int, int, object]] = []
    i, n = 0, len(b)
    while i < n:
        try:
            k, i = _varint(b, i)
        except IndexError:
            break
        f, wt = k >> 3, k & 7
        try:
            if wt == 0:
                v, i = _varint(b, i)
            elif wt == 1:
                v = struct.unpack("<Q", b[i:i + 8])[0]; i += 8
            elif wt == 2:
                ln, i = _varint(b, i); v = b[i:i + ln]; i += ln
            elif wt == 5:
                v = struct.unpack("<I", b[i:i + 4])[0]; i += 4
            else:
                break
        except (IndexError, struct.error):
            break
        out.append((f, wt, v))
    return out


def _sub(d, fn):
    return next((v for f, wt, v in d if f == fn and wt == 2), None)


def _var(d, fn):
    return next((v for f, wt, v in d if f == fn and wt == 0), None)


def _str(d, fn):
    return next((v.decode("utf-8", "replace") for f, wt, v in d if f == fn and wt == 2), None)


def _packed(b: bytes) -> list[int]:
    """A packed repeated varint field."""
    out: list[int] = []
    i = 0
    try:
        while i < len(b):
            v, i = _varint(b, i)
            out.append(v)
    except IndexError:
        pass
    return out


# ── Extraction ────────────────────────────────────────────────────────────────

USER_INPUT = 14
PLANNER_RESPONSE = 15
#: f1.f4's count fields: a call that carries none of them has no usage report.
_COUNT_FIELDS = (2, 3, 4, 5)


@dataclass(frozen=True)
class Call:
    """One model call, as agy recorded it."""

    index: int                  # gen_metadata.idx
    model: Optional[str]
    message_id: Optional[str]
    reported: bool              # agy recorded the provider's usage for it
    fresh_input: int
    output: int                 # thinking included
    cache_read: int
    cache_write: int
    reasoning: int              # thinking, already inside ``output``
    at: Optional[float]         # epoch seconds: its own step's time (or its neighbour's, see ``_date``)
    step: Optional[int]         # the PLANNER_RESPONSE step it produced


@dataclass(frozen=True)
class Conversation:
    calls: tuple[Call, ...] = ()
    user_times: tuple[float, ...] = ()      # each USER_INPUT step's time
    last_step_at: Optional[float] = None

    @property
    def reported(self) -> tuple[Call, ...]:
        return tuple(c for c in self.calls if c.reported)

    @property
    def unreported(self) -> tuple[Call, ...]:
        return tuple(c for c in self.calls if not c.reported)


_EMPTY = Conversation()
#: A store that exists but can't be read: no calls, and never kept.
UNREAD = Conversation()


def _step_time(metadata: object) -> Optional[float]:
    if not isinstance(metadata, (bytes, bytearray)):
        return None
    stamp = _dec(_sub(_dec(metadata), 1) or b"")
    seconds = _var(stamp, 1)
    if not seconds:
        return None
    return seconds + (_var(stamp, 2) or 0) / 1e9


def _call(idx: int, data: bytes, step_times: dict[int, float]) -> Call:
    top = _dec(data)
    m1 = _dec(_sub(top, 1) or b"")
    usage = _dec(_sub(m1, 4) or b"")
    reported = any(f in _COUNT_FIELDS and wt == 0 for f, wt, _v in usage)
    produced = _packed(_sub(top, 2) or b"")
    step = produced[0] if produced else None
    message_id = _str(usage, 7)
    return Call(
        index=idx, model=_str(m1, 21) or _str(m1, 19), message_id=message_id or None, reported=reported,
        fresh_input=_var(usage, 2) or 0, output=_var(usage, 3) or 0,
        cache_write=_var(usage, 4) or 0, cache_read=_var(usage, 5) or 0, reasoning=_var(usage, 9) or 0,
        at=step_times.get(step) if step is not None else None, step=step,
    )


def _date(calls: list[Call], fallback: Optional[float]) -> list[Call]:
    """A call agy didn't link to a step (one that failed before any step was
    written) takes the time of the call before it, else after it, else the
    conversation's last step."""
    out: list[Call] = []
    for i, call in enumerate(calls):
        if call.at is None:
            before = next((c.at for c in reversed(calls[:i]) if c.at is not None), None)
            after = next((c.at for c in calls[i + 1:] if c.at is not None), None)
            at = before if before is not None else after if after is not None else fallback
            call = Call(**{**call.__dict__, "at": at})
        out.append(call)
    return out


def _read(con: sqlite3.Connection) -> Conversation:
    """May raise ``sqlite3.Error`` (e.g. a missing table), which the callers
    translate into a fallback or ``UNREAD``."""
    step_times: dict[int, float] = {}
    user_times: list[float] = []
    last: Optional[float] = None
    for idx, step_type, metadata in con.execute("SELECT idx, step_type, metadata FROM steps ORDER BY idx"):
        at = _step_time(metadata)
        if at is None:
            continue
        step_times[idx] = at
        last = at if last is None else max(last, at)
        if step_type == USER_INPUT:
            user_times.append(at)
    calls = [_call(idx, data, step_times)
             for idx, data in con.execute("SELECT idx, data FROM gen_metadata ORDER BY idx")
             if isinstance(data, (bytes, bytearray))]
    return Conversation(calls=tuple(_date(calls, last)), user_times=tuple(user_times), last_step_at=last)


def _read_ro(db_path: Path) -> Optional[Conversation]:
    """Read via a **read-only** (non-mutating) connection; ``None`` if the
    store can't be read that way (so the caller can fall back)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return _read(con)
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _read_checkpointed(db_path: Path) -> Conversation:
    """Fallback: open **read-write**, flush the ``-wal`` into the main db via
    ``PRAGMA wal_checkpoint(TRUNCATE)``, then read. This mutates agy's store,
    so it runs only when :func:`_read_ro` can't see the data."""
    try:
        con = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return UNREAD
    try:
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        except sqlite3.Error:
            pass
        return _read(con)
    except sqlite3.Error:
        return UNREAD
    finally:
        con.close()


def _has_pending_wal(db_path: Path) -> bool:
    """True if a non-empty ``-wal`` sidecar exists (maybe uncheckpointed rows)."""
    try:
        return Path(f"{db_path}-wal").stat().st_size > 0
    except OSError:
        return False


def store_stamp(db_path: str | Path) -> tuple:
    """What changes when agy writes the store: the db's and its ``-wal``'s
    size and time (agy writes new rows to the ``-wal`` first)."""
    out: list = []
    for path in (str(db_path), f"{db_path}-wal"):
        try:
            st = os.stat(path)
            out.extend((st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            out.extend((0, 0, 0))
    return tuple(out)


def read_conversation(db_path: str | Path) -> Conversation:
    """Every model call in one conversation's store, with its usage and time.
    ``UNREAD`` when the store is there but can't be read."""
    p = Path(db_path)
    if not p.is_file():
        return _EMPTY
    conversation = _read_ro(p)
    if conversation is None or (not conversation.calls and _has_pending_wal(p)):
        conversation = _read_checkpointed(p)
    return conversation


_kept: dict[str, tuple[tuple, Conversation]] = {}
_kept_lock = Lock()


def conversation_usage(conversation_id: str) -> Conversation:
    """:func:`read_conversation` for one conversation id, kept until agy
    changes its store. A store that can't be read is never kept."""
    path = conversation_db(conversation_id)
    stamp = store_stamp(path)
    with _kept_lock:
        kept = _kept.get(conversation_id)
    if kept is not None and kept[0] == stamp:
        return kept[1]
    conversation = read_conversation(path)
    if conversation is not UNREAD and store_stamp(path) == stamp:   # a store written while read: read again
        with _kept_lock:
            _kept[conversation_id] = stamp, conversation
    return conversation


__all__ = ["Call", "Conversation", "UNREAD", "read_conversation", "conversation_usage", "store_stamp",
           "USER_INPUT", "PLANNER_RESPONSE"]
