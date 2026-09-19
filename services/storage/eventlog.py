"""An append-only log of event lines that rotates, and the readers over it.

Every event line starts with ``ts`` and ``type`` (the record rule); the
log keeps the order it received lines in and readers sort by ``ts``. Past
``rotate_bytes`` the live file is renamed ``<stem>.<stamp>.jsonl`` and the
oldest rotations beyond ``keep`` are removed. Readers see the live file and
the rotations as one log, newest first.

::

    log = EventLog(path, rotate_bytes=8 << 20, keep=5)
    log.append(lines)
    log.tail(limit=200, before=None, types=None)
    async for line in log.follow(since=None):
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

from services.storage.atomic_write import append_jsonl
from services.storage.flock import locked
from services.storage.reader import read_jsonl_tail_reverse
from services.timestamps import EPOCH, parse_ts

logger = logging.getLogger(__name__)

_STAMP = "%Y%m%dT%H%M%SZ"


def envelope(line: dict) -> dict:
    """The line with ``ts`` and ``type`` first, then everything else in
    the order it came."""
    head = {"ts": line.get("ts"), "type": line.get("type")}
    return {**head, **{k: v for k, v in line.items() if k not in head}}


def ts_key(line: dict) -> datetime:
    return parse_ts(line.get("ts")) or EPOCH


class EventLog:
    def __init__(self, path: Path, *, rotate_bytes: int = 8 << 20, keep: int = 5) -> None:
        self.path = Path(path)
        self.rotate_bytes = int(rotate_bytes)
        self.keep = int(keep)
        # <stem>.<stamp>.jsonl, and <stem>.<stamp>.<n>.jsonl when two rotations
        # land in the same second.
        self._rotation_re = re.compile(
            rf"{re.escape(self.path.stem)}\.\d{{8}}T\d{{6}}Z(\.\d+)?{re.escape(self.path.suffix)}"
        )

    # ── files ────────────────────────────────────────────────────────────

    def rotations(self) -> list[Path]:
        """Rotated segments, oldest first: by the time each was last written,
        then by name (two rotations in one second share a stamp)."""
        if not self.path.parent.is_dir():
            return []
        found = [p for p in self.path.parent.iterdir() if self._rotation_re.fullmatch(p.name)]

        def key(p: Path) -> tuple[int, str]:
            try:
                return p.stat().st_mtime_ns, p.name
            except OSError:
                return 0, p.name

        return sorted(found, key=key)

    def rotate_if_needed(self) -> bool:
        """Rename the live file past the threshold and prune old rotations.
        Call inside the lock. Only stamp-named files are ever pruned, so a
        hand-copied file is left alone. Returns whether a rotation happened."""
        try:
            if not self.path.is_file() or self.path.stat().st_size < self.rotate_bytes:
                return False
            stamp = datetime.now(timezone.utc).strftime(_STAMP)
            target = self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}")
            n = 1
            while target.exists():
                target = self.path.with_name(f"{self.path.stem}.{stamp}.{n}{self.path.suffix}")
                n += 1
            self.path.rename(target)
            for old in self.rotations()[:-self.keep] if self.keep > 0 else self.rotations():
                old.unlink()
            return True
        except OSError as exc:
            logger.warning("eventlog: rotation failed for %s: %s", self.path, exc)
            return False

    # ── writes ───────────────────────────────────────────────────────────

    def append(self, lines: Iterable[dict]) -> int:
        """Rotation check, then one append of the lines in chronological
        order (a stable sort by ``ts``: ties keep their order). Each line is
        put into envelope order. Returns how many were written."""
        ordered = sorted((envelope(dict(l)) for l in lines if isinstance(l, dict)), key=ts_key)
        if not ordered:
            return 0
        with locked(self.path):
            self.rotate_if_needed()
            append_jsonl(self.path, ordered)
        return len(ordered)

    # ── reads ────────────────────────────────────────────────────────────

    def tail(self, *, limit: int, before: Optional[str] = None,
             types: Optional[Iterable[str]] = None) -> list[dict]:
        """Newest first, across the live file and the rotations, at most
        ``limit`` lines; ``before`` (a timestamp, compared parsed) and
        ``types`` filter. Sorted by ``ts`` as a safety net against
        interleaved late writers."""
        wanted = max(1, int(limit))
        allow = None if types is None else frozenset(t for t in types if isinstance(t, str))
        cutoff = parse_ts(before) if before else None
        out: list[dict] = []
        for path in [self.path, *reversed(self.rotations())]:
            for line in read_jsonl_tail_reverse(path, limit=wanted - len(out), types=allow):
                if cutoff is not None:
                    when = parse_ts(line.get("ts"))
                    if when is None or when >= cutoff:
                        continue
                out.append(line)
            if len(out) >= wanted:
                break
        out.sort(key=ts_key, reverse=True)
        return out[:wanted]

    def count(self) -> int:
        """Lines in the live file (rotations not counted); 0 when absent."""
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                return sum(1 for line in fp if line.strip())
        except OSError:
            return 0

    async def follow(self, *, since: Optional[str] = None, types: Optional[Iterable[str]] = None,
                     poll_s: float = 1.0, catch_up: int = 100) -> AsyncIterator[dict]:
        """Yield lines as they are appended, oldest first. With ``since`` the
        lines after that timestamp (at most ``catch_up`` of them) come first;
        without it only new lines. Survives a rotation (a new inode starts
        from offset 0). Ends when the consumer stops iterating."""
        allow = None if types is None else frozenset(t for t in types if isinstance(t, str))
        if since:
            backlog = [l for l in reversed(self.tail(limit=catch_up, types=allow))
                       if (parse_ts(l.get("ts")) or EPOCH) > (parse_ts(since) or EPOCH)]
            for line in backlog:
                yield line
        inode, offset = self._position()
        while True:
            await asyncio.sleep(poll_s)
            new_inode, size = self._position()
            if new_inode != inode:
                inode, offset = new_inode, 0
            if size <= offset:
                continue
            chunk = self._read_from(offset)
            offset += len(chunk.encode("utf-8"))
            for raw in chunk.splitlines():
                if not raw.strip():
                    continue
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(line, dict) and (allow is None or line.get("type") in allow):
                    yield line

    def follow_from_now(self):
        """A follower positioned at the current end, for :func:`follow_many`."""
        return self.follow()

    def _position(self) -> tuple[Optional[int], int]:
        try:
            st = self.path.stat()
            return st.st_ino, st.st_size
        except OSError:
            return None, 0

    def _read_from(self, offset: int) -> str:
        try:
            with self.path.open("rb") as fp:
                fp.seek(offset)
                data = fp.read()
        except OSError:
            return ""
        # Only whole lines: a partial trailing line is read next time.
        cut = data.rfind(b"\n")
        if cut < 0:
            return ""
        return data[: cut + 1].decode("utf-8", errors="replace")


async def follow_many(logs: dict[str, EventLog], *, since: Optional[str] = None,
                      types: Optional[Iterable[str]] = None, tag: str = "source",
                      poll_s: float = 1.0) -> AsyncIterator[dict]:
    """One feed over several logs: each line gains ``tag: <name>``; the
    backlog after ``since`` is replayed in ``ts`` order first, then new
    lines as they land in any of the logs."""
    allow = None if types is None else frozenset(t for t in types if isinstance(t, str))
    if since:
        cutoff = parse_ts(since) or EPOCH
        backlog = []
        for name, log in logs.items():
            for line in log.tail(limit=100, types=allow):
                if (parse_ts(line.get("ts")) or EPOCH) > cutoff:
                    backlog.append({**line, tag: name})
        for line in sorted(backlog, key=ts_key):
            yield line
    queue: "asyncio.Queue[dict]" = asyncio.Queue()

    async def pump(name: str, log: EventLog) -> None:
        async for line in log.follow(types=allow, poll_s=poll_s):
            await queue.put({**line, tag: name})

    pumps = [asyncio.create_task(pump(name, log)) for name, log in logs.items()]
    try:
        while True:
            yield await queue.get()
    finally:
        for task in pumps:
            task.cancel()
