"""Each turn's raw CLI output, kept for debugging (storm ch 08 §4, §6; ch 16 §3;
§0.106).

``<state>/engine/turns/<turn id>.jsonl`` (``layout.turn_logs_dir``, folder
0700, files 0600): every line the session's CLI wrote on stdout, redacted,
appended as it comes. Nothing in the server reads the files back, and they
are kept (the founder's decision of 2026-11-19: they are the only verbatim
record of full tool outputs). They replace the PoC's overwritten
``last-turn.jsonl``.

**Each diff block once (``DiffBlocks``).** Codex sends its turn's whole
cumulative diff again after every change (``turn/diff/updated``): one turn
of 11-10 wrote 5,281 of them, 1,670 MB, each mostly the one before. Such a
line is written as ``{"xo.raw": "diff", "pre", "blocks", "post"}``: the bytes
before and after the diff's JSON string as they are, and the string cut
into its per-file blocks (before each ``\ndiff --git ``), each named by its
SHA-256; a block's bytes are written once per file, in a
``{"xo.raw": "block", "sha256", "bytes"}`` line before the first line that
names it. Bytes travel as Latin-1 text, so any byte survives. Nothing is
dropped or changed: ``scripts/raw_turn_expand.py`` gives back every line
exactly as the CLI wrote it (redacted). A CLI line that itself starts like
``{"xo.raw"`` is wrapped as ``{"xo.raw": "line", "bytes"}``, so no line is
ever mistaken for one of these. QUIRQ_RAW_TURN_DIFF_BLOCKS=off writes the
diffs as they come.

**How the lines get here without naming a CLI.** Every session process is
started by the engine's launcher (``session._Launcher``), which hands the
runtime a ``TeedProcess``: the process, whose ``lines()`` copies each stdout
line to the session's ``RawTurnLog`` as the runtime reads it. (The contract's
``debug_raw`` would attach the CLI's payload only to the lines that become
events, and change the session's spec.) Which turn a line belongs to is the
engine's own knowledge, as it stands when the runtime *reads* the line:

* from a turn's start to the next turn's start, lines go to that turn's file,
  so what the CLI says between turns (a late report, a sub-agent's end) stays
  with the turn before it;
* lines before the process's first turn (the CLI's start, its MCP servers'
  status) and lines read while a new input is on its way to an idle CLI
  (``hold``) are held, bounded, and written at the top of the turn that
  follows, or of the last turn if the process ends first; a process that
  never ran a turn writes no file.

**The files are filed by the read, not by the event, so a boundary can be
off.** A turn starts for the engine when its pump takes the runtime's
``TurnStarted``, and a runtime may read the CLI's output ahead of that (it
may buffer its events). A turn the engine asks of an idle CLI is, as a
rule, filed right: its input ``hold``s the lines until the turn starts. A
turn the CLI starts on its own (a worker's end wakes it) has its first lines,
those read before the pump takes its start, at the end of the turn before;
with the runtime far enough ahead, a turn's file holds only its last lines
and the turn before holds the rest. Exact filing would need each event to
name the lines it came from, which the runtime contract doesn't carry, and
only a runtime could say; the files are for debugging, so read a turn's file
with the ones before and after it (by mtime).

**Off the event loop, never in the turn's way.** The tee only queues a
line (no copy, no I/O); one task writes batches from a worker thread,
redacting there. Memory is bounded by bytes: up to ``QUEUE_BYTES`` waiting,
plus the batch being written (at most as much again) and its redacted copy,
so about twice ``QUEUE_BYTES`` plus copies, and ``HELD_BYTES`` held; a single
longer line is still kept when nothing else waits. A line that finds the
queue full is dropped and counted. A failed write (a full disk) is logged once
per process and never fails the turn. The worker still shares the
interpreter: redacting a very large line (a CLI may write up to 64 MiB in
one) holds the loop back while it runs, about 11 ms per MB on real turn logs
(90 MB/s on 5.7 MB of a CLI's captured turn logs; slower on secret-dense text).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import hashlib
import json
import re
from collections import deque
from typing import Any, AsyncIterator

from services.cowork_agent.storm.redact import redact
from services.storage.layout import turn_logs_dir

log = logging.getLogger(__name__)

#: Lines waiting for the writer, in bytes; past this a line is dropped.
QUEUE_BYTES = 8 * 1024 * 1024
#: Lines held for a turn that hasn't started yet, in bytes.
HELD_BYTES = 1024 * 1024
#: A ``turn/diff/updated`` line shorter than this is written as it is.
DIFF_BLOCKS_MIN_BYTES = 4096
FOLDER_MODE = 0o700
FILE_MODE = 0o600
#: A turn id that can name a file in the folder as it is.
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class RawTurnLog:
    """One session process's raw output, split into its turns' files. The
    loop calls ``begin``, ``hold`` and ``line``; ``close`` writes what is
    left."""

    def __init__(self, label: str) -> None:
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
        self._blocks = DiffBlocks()

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


_DIFF_LINE = b'{"method":"turn/diff/updated"'
_RAW_PREFIX = b'{"xo.raw"'
#: The diff's JSON string: ``"diff":"`` then escapes or other bytes, to the closing quote.
_DIFF_STRING = re.compile(rb'"diff":"((?:[^"\\]++|\\.)*+)"', re.S)
#: A block starts at each ``diff --git `` that follows an (escaped) newline.
_BLOCK_START = re.compile(rb'(?<=\\n)diff --git ')


def _text(data: bytes) -> str:
    return data.decode("latin-1")


class DiffBlocks:
    """One file's encoder (the module's docstring): the blocks it already
    wrote, by digest. Used from the writer's thread only."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.enabled = (os.getenv("QUIRQ_RAW_TURN_DIFF_BLOCKS", "") or "").strip().lower() not in ("off", "false", "0")

    def new_file(self) -> None:
        self._seen = set()

    def encode(self, line: bytes) -> bytes:
        """``line`` (without its newline) as the bytes to append."""
        if line.startswith(_RAW_PREFIX):
            return _json_line({"xo.raw": "line", "bytes": _text(line)})
        if not (self.enabled and len(line) >= DIFF_BLOCKS_MIN_BYTES and line.startswith(_DIFF_LINE)):
            return line + b"\n"
        found = _DIFF_STRING.search(line)
        if found is None:
            return line + b"\n"
        diff = found.group(1)
        cuts = [0, *(m.start() for m in _BLOCK_START.finditer(diff)), len(diff)]
        out: list[bytes] = []
        names: list[str] = []
        for start, end in zip(cuts, cuts[1:]):
            block = diff[start:end]
            if not block:
                continue
            digest = hashlib.sha256(block).hexdigest()
            if digest not in self._seen:
                out.append(_json_line({"xo.raw": "block", "sha256": digest, "bytes": _text(block)}))
                self._seen.add(digest)
            names.append(digest)
        out.append(_json_line({"xo.raw": "diff", "pre": _text(line[:found.start(1)]), "blocks": names,
                               "post": _text(line[found.end(1):])}))
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
