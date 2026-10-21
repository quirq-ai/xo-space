"""The daily retention prune (design ch 08 §6; §0.106): what the store and
the raw turn logs keep, as fixed values, not settings.

| Data | Kept |
|---|---|
| ``events`` of type ``step.*``, ``progress.*``, ``run.*`` | ``RETENTION_S`` (30 days) |
| other events; messages, threads, cards, review items, rules | always |
| turns, runs | as long as their session (sessions are never deleted) |
| grants | until used or expired, then ``GRANTS_KEPT_S`` (30 days) |
| wakes ``consumed`` / ``dropped`` | ``WAKES_KEPT_S`` (30 days) |
| raw turn logs (``layout.turn_logs_dir()``) | ``TURN_LOGS_KEPT_S`` (14 days) after their last write |

The engine runs it once a day (``Engine.start``), under its lock: the
process that owns the store is the only one that prunes it. Rows go in
short transactions of at most ``BATCH_ROWS``, so the one writer is never
held for long: the engine's own writes land between batches, and a crash
mid-prune leaves every batch kept or undone whole.

Never pruned, whatever their age: the newest event (the store's head, so a
resume point never ends up ahead of it), and the events of a turn still
running or with an open review item (the boot recovery's resume note and a
replay of that turn still read them). A raw log whose turn is running is
kept too. A resume point the prune passed behaves as ch 09 §4 says: the
stream sends ``reset`` (``stream.replay.stale_point``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.cowork_agent.storm.store.db import Store, Tx
from services.cowork_agent.storm.store.lock import EngineLock
from services.storage.layout import turn_logs_dir
from services.timestamps import iso

log = logging.getLogger(__name__)

DAY_S = 24 * 3600
#: How long the shortest-lived stored events are kept (``step.*``,
#: ``progress.*``, ``run.*``). A resume point older than this may have lost
#: events, so the client reloads instead (ch 09 §4).
RETENTION_S = 30 * DAY_S
#: A grant, after it was used or expired.
GRANTS_KEPT_S = 30 * DAY_S
#: A ``consumed`` or ``dropped`` wake, after it ended.
WAKES_KEPT_S = 30 * DAY_S
#: A turn's raw CLI log, after its last write (its mtime).
TURN_LOGS_KEPT_S = 14 * DAY_S
#: The prune runs daily, its first run an hour after the engine starts, so it
#: never adds to a boot under load (like the worktree sweep).
PRUNE_INTERVAL_S = DAY_S
PRUNE_FIRST_S = 3600
#: Rows one transaction deletes: a few milliseconds of the writer's time
#: (measured on a 1M-event store: 4 to 8 ms a batch, median, as load varies).
BATCH_ROWS = 500
#: The event types kept ``RETENTION_S``, by prefix.
PRUNED_EVENT_FAMILIES = ("step.", "progress.", "run.")
#: Turns whose events stay whatever their age: running, or asked about.
_HELD_TURNS = ("SELECT id FROM turns WHERE status = 'running' "
               "UNION SELECT turn_id FROM review_items WHERE state = 'open' AND turn_id IS NOT NULL")
_TURN_LOG_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class Pruned:
    events: int
    grants: int
    wakes: int
    turn_logs: int
    turn_logs_left: int         # old enough, but couldn't be removed


async def prune(store: Store, lock: EngineLock, *, now: float, logs: Path | None = None) -> Pruned:
    """Remove what ch 08 §6 no longer keeps, as of ``now`` (a wall time)."""
    if not lock.held:
        raise RuntimeError("the retention prune runs only under the engine lock")
    started = time.monotonic()
    events_before = _iso(now - RETENTION_S)
    grants_before = _iso(now - GRANTS_KEPT_S)
    wakes_before = _iso(now - WAKES_KEPT_S)
    events = 0
    for event_type in await store.read(_pruned_types):
        events += await _in_batches(store, lambda tx, limit, t=event_type: _events_batch(tx, t, events_before, limit))
    grants = await _in_batches(store, lambda tx, limit: tx.execute(
        "DELETE FROM grants WHERE rowid IN (SELECT rowid FROM grants WHERE coalesce(used_at, expires_at) < ? "
        "LIMIT ?)", (grants_before, limit)).rowcount)
    wakes = await _in_batches(store, lambda tx, limit: tx.execute(
        "DELETE FROM wakes WHERE rowid IN (SELECT rowid FROM wakes WHERE state IN ('consumed', 'dropped') AND "
        "done_at < ? LIMIT ?)", (wakes_before, limit)).rowcount)
    running = await store.read(lambda conn: {r[0] for r in conn.execute(
        "SELECT id FROM turns WHERE status = 'running'")})
    removed, left = await asyncio.to_thread(_remove_turn_logs, logs or turn_logs_dir(), now - TURN_LOGS_KEPT_S,
                                            running)
    pruned = Pruned(events=events, grants=grants, wakes=wakes, turn_logs=removed, turn_logs_left=left)
    log.info("storm retention: pruned %d event(s), %d grant(s), %d wake(s) and %d raw turn log(s) (%d could not be "
             "removed) in %.1f s", pruned.events, pruned.grants, pruned.wakes, pruned.turn_logs,
             pruned.turn_logs_left, time.monotonic() - started)
    return pruned


async def _in_batches(store: Store, batch: Callable[[Tx, int], int]) -> int:
    """Run ``batch(tx, BATCH_ROWS)`` (it returns the rows it deleted), one
    transaction each, until one deletes fewer than a full batch."""
    total = 0
    while True:
        deleted = await store.write(lambda tx: batch(tx, BATCH_ROWS))
        total += deleted
        if deleted < BATCH_ROWS:
            return total


def _pruned_types(conn: sqlite3.Connection) -> list[str]:
    """Every stored event type in the pruned families: one seek of the
    ``(type, ts)`` index per type, never a scan of the events."""
    types: list[str] = []
    for family in PRUNED_EVENT_FAMILIES:
        end, after = family[:-1] + chr(ord(family[-1]) + 1), ""
        while row := conn.execute("SELECT type FROM events WHERE type >= ? AND type < ? AND type > ? "
                                  "ORDER BY type LIMIT 1", (family, end, after)).fetchone():
            after = row[0]
            types.append(after)
    return types


def _events_batch(tx: Tx, event_type: str, cutoff: str, limit: int) -> int:
    return tx.execute(
        "DELETE FROM events WHERE seq IN (SELECT seq FROM events WHERE type = ? AND ts < ? "
        f"AND seq < (SELECT max(seq) FROM events) AND (turn_id IS NULL OR turn_id NOT IN ({_HELD_TURNS})) "
        "LIMIT ?)", (event_type, cutoff, limit)).rowcount


def _remove_turn_logs(folder: Path, cutoff_s: float, running: set[str]) -> tuple[int, int]:
    """Remove each ``<turn>.jsonl`` last written before ``cutoff_s``, but
    a running turn's. Returns (removed, left)."""
    removed = left = 0
    try:
        entries = os.scandir(folder)
    except FileNotFoundError:
        return 0, 0
    with entries:
        for entry in entries:
            if not entry.name.endswith(_TURN_LOG_SUFFIX) or entry.name[:-len(_TURN_LOG_SUFFIX)] in running:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff_s:
                    continue
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if not left:
                    log.warning("storm retention: a raw turn log could not be removed (%s); the next prune tries "
                                "again", exc.strerror or exc)
                left += 1
                continue
            removed += 1
    return removed, left


def _iso(seconds: float) -> str:
    """A wall time as the store writes it, so text order is time order."""
    return iso(datetime.fromtimestamp(seconds, timezone.utc))
