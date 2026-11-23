"""Each turn's raw CLI stdout, redacted, appended to a kept per-turn file for debugging; repeated blocks stored once."""
# Never in the turn's way: the tee only queues; a full queue drops the line and a failed write is logged once.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import hashlib
import json
import re
from collections import deque
from typing import Any, AsyncIterator, Callable

from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.storm.redact import redact
from services.storage.layout import turn_logs_dir

log = logging.getLogger(__name__)

#: Lines waiting for the writer, in bytes; past this a line is dropped.
QUEUE_BYTES = 8 * 1024 * 1024
#: Lines held for a turn that hasn't started yet, in bytes.
HELD_BYTES = 1024 * 1024
#: A line shorter than this is written as it is, never offered to the agent's ``split``.
SPLIT_MIN_BYTES = 4096
FOLDER_MODE = 0o700
FILE_MODE = 0o600
#: A turn id that can name a file in the folder as it is.
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class RawTurnLog:
    """One session process's raw output, split into its turns' files. The
    loop calls ``begin``, ``hold`` and ``line``; ``close`` writes what is
    left."""

    def __init__(self, label: str, split: "Split | None" = None) -> None:
        self._label = label
        self._turn: str | None = None        # where lines go now; None: held for the next turn
        self._last: str | None = None        # the latest turn begun
        self._held: list[bytes] = []
        self._held_bytes = 0
        self._queue: deque[tuple[str, bytes]] = deque()
        self._queued = 0
        self._writer: asyncio.Task | None = None
        self._closed = False
        self.dropped = 0
        # The writer's own, used from its thread only, one batch at a time.
        self._file: tuple[str, int] | None = None
        self._failed = False
        self._unnamed: set[str] = set()
        self._blocks = Blocks(split)

    def begin(self, turn_id: str) -> None:
        """A turn started: its lines go to its file, after the held ones."""
        if turn_id == self._turn:
            return
        self._turn = self._last = turn_id
        self._release_held(turn_id)

    def hold(self) -> None:
        """An input is on its way to a CLI with no turn running: what the CLI
        says next belongs to the turn that input starts."""
        self._turn = None

    def line(self, data: bytes) -> None:
        if self._closed:
            return
        if self._turn is not None:
            self._put(self._turn, data)
        elif self._held and self._held_bytes + len(data) > HELD_BYTES:
            self.dropped += 1
        else:
            self._held.append(data)
            self._held_bytes += len(data)

    async def close(self) -> None:
        """The process is gone: write what waits (held lines go to the last
        turn, if it ran one) and close the file."""
        if self._closed:
            return
        self._closed = True
        if self._last is not None:
            self._release_held(self._last)
        self._held, self._held_bytes = [], 0
        if self._writer is not None:
            try:
                await self._writer
            except Exception:  # noqa: BLE001 - a debugging log must never stop the session's end
                log.exception("storm: session %s: writing its raw turn log failed", self._label)
        await asyncio.to_thread(self._close_file)
        if self.dropped:
            log.warning("storm: session %s: %d raw CLI line(s) were not kept in its turn logs (written too slowly)",
                        self._label, self.dropped)

    def _release_held(self, turn_id: str) -> None:
        held, self._held, self._held_bytes = self._held, [], 0
        for data in held:
            self._put(turn_id, data)

    def _put(self, turn_id: str, data: bytes) -> None:
        if self._queue and self._queued + len(data) > QUEUE_BYTES:
            self.dropped += 1
            return
        self._queue.append((turn_id, data))
        self._queued += len(data)
        if self._writer is None:
            self._writer = asyncio.create_task(self._drain(), name=f"storm-raw-turns:{self._label}")

    async def _drain(self) -> None:
        try:
            while self._queue:
                batch = list(self._queue)
                self._queue.clear()
                self._queued = 0
                await asyncio.to_thread(self._write, batch)
        finally:
            self._writer = None

    # ── the writer's thread ──────────────────────────────────────────────

    def _write(self, batch: list[tuple[str, bytes]]) -> None:
        try:
            for turn_id, chunk in _by_turn(batch):
                fd = self._fd(turn_id)
                if fd is not None:
                    _write_all(fd, b"".join(self._blocks.encode(_redacted(data)) for data in chunk))
        except OSError as exc:
            self._close_file()
            if not self._failed:
                self._failed = True
                log.warning("storm: session %s: its raw turn log can't be written (%s); the turn goes on without "
                            "it", self._label, exc.strerror or exc)

    def _fd(self, turn_id: str) -> int | None:
        if self._file is not None and self._file[0] == turn_id:
            return self._file[1]
        self._close_file()
        if not _FILE_NAME.fullmatch(turn_id):
            if turn_id not in self._unnamed:
                self._unnamed.add(turn_id)
                log.warning("storm: session %s: turn id %r can't name a file; its raw output isn't kept",
                            self._label, turn_id)
            return None
        folder = turn_logs_dir()
        folder.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
        if folder.stat().st_mode & 0o777 != FOLDER_MODE:
            folder.chmod(FOLDER_MODE)
        fd = os.open(folder / f"{turn_id}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
        self._file = (turn_id, fd)
        return fd

    def _close_file(self) -> None:
        self._blocks.new_file()         # the next file holds every block it names
        if self._file is not None:
            fd, self._file = self._file[1], None
            with contextlib.suppress(OSError):     # unbuffered: a failure already showed at a write
                os.close(fd)


def _by_turn(batch: list[tuple[str, bytes]]) -> list[tuple[str, list[bytes]]]:
    groups: list[tuple[str, list[bytes]]] = []
    for turn_id, data in batch:
        if groups and groups[-1][0] == turn_id:
            groups[-1][1].append(data)
        else:
            groups.append((turn_id, [data]))
    return groups


def _redacted(data: bytes) -> bytes:
    # A line that isn't UTF-8 keeps its bytes as they are.
    return redact(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")


_RAW_PREFIX = b'{"xo.raw"'

#: An agent's ``raw_log.split``: a line as ``(pre, blocks, post)``, or None to write it as it is.
Split = Callable[[bytes], "tuple[bytes, list[bytes], bytes] | None"]


def agent_split(agent: str | None) -> Split | None:
    """The agent's ``raw_log.split``, when its adapter has one."""
    if not agent:
        return None
    module = try_load_capability("raw_log", agent=agent)
    return getattr(module, "split", None) if module is not None else None


def _text(data: bytes) -> str:
    return data.decode("latin-1")


class Blocks:
    """One file's encoder: the blocks it already wrote, by digest; used from the writer's thread only."""

    def __init__(self, split: Split | None = None) -> None:
        self._seen: set[str] = set()
        enabled = (os.getenv("QUIRQ_RAW_TURN_DIFF_BLOCKS", "") or "").strip().lower() not in ("off", "false", "0")
        self._split = split if enabled else None

    def new_file(self) -> None:
        self._seen = set()

    def encode(self, line: bytes) -> bytes:
        """``line`` (without its newline) as the bytes to append."""
        if line.startswith(_RAW_PREFIX):
            return _json_line({"xo.raw": "line", "bytes": _text(line)})
        if self._split is None or len(line) < SPLIT_MIN_BYTES:
            return line + b"\n"
        parts = self._split(line)
        if parts is None:
            return line + b"\n"
        pre, blocks, post = parts
        if pre + b"".join(blocks) + post != line:      # never trust a split to be lossless
            return line + b"\n"
        out: list[bytes] = []
        names: list[str] = []
        for block in blocks:
            if not block:
                continue
            digest = hashlib.sha256(block).hexdigest()
            if digest not in self._seen:
                out.append(_json_line({"xo.raw": "block", "sha256": digest, "bytes": _text(block)}))
                self._seen.add(digest)
            names.append(digest)
        out.append(_json_line({"xo.raw": "diff", "pre": _text(pre), "blocks": names, "post": _text(post)}))
        return b"".join(out)


def _json_line(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class TeedProcess:
    """A session process whose stdout lines are copied to its raw turn log as
    the runtime reads them; everything else is the process's own."""

    def __init__(self, process: Any, raw: RawTurnLog) -> None:
        self._process = process
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)

    async def lines(self) -> AsyncIterator[bytes]:
        async for line in self._process.lines():
            self._raw.line(line)
            yield line
