"""Find and stop every process that carries a session's marker (plan W3).

Runs only when a session is halted, never in normal work.

* **Finding:** a scan of ``/proc``. Of this user's processes (another user's
  environment can't be read, nor its process signalled), those whose
  starting environment holds the session's marker, at any epoch: what an
  earlier process of the session left behind is the session's work too.
* **Naming:** a process is named by its pid and its start time
  (``process_start_ticks``). The marker is read a second time between two
  readings of the start time, and kept only if both readings agree, so a pid
  freed and reused during the scan is never taken for the marked process.
* **Stopping:** through a pidfd, opened only while the pid still has that
  start time, so a signal can't reach a process that took the number over
  since. SIGTERM, then SIGKILL after ``GRACE_S``.

The server itself and its ancestors are never touched, even when they carry
the marker (a server an agent started from its shell).
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from services.cowork_agent.storm.control.marker import MARKER
from utils.commands import process_parent, process_start_ticks, signal_and_wait

#: How long a marked process gets after SIGTERM before SIGKILL, and after
#: SIGKILL before it counts as surviving.
GRACE_S = 1.0


@dataclass(frozen=True)
class Marked:
    """One process carrying the session's marker, as the scan found it."""

    pid: int
    start_ticks: int
    epoch: int
    name: str                            # its command name (``/proc/<pid>/comm``)

    def row(self) -> dict[str, Any]:
        return {"pid": self.pid, "epoch": self.epoch, "name": self.name}


@dataclass(frozen=True)
class Swept:
    """What one sweep found and did, and how long it took."""

    scanned: int                         # this user's processes looked at
    killed: tuple[tuple[Marked, str], ...] = ()     # each with the signal that ended it
    gone: tuple[Marked, ...] = ()        # ended (or its pid reused) before a signal was sent
    survived: tuple[Marked, ...] = ()    # still running after SIGKILL and the grace
    scan_s: float = 0.0
    kill_s: float = 0.0

    @property
    def found(self) -> int:
        return len(self.killed) + len(self.gone) + len(self.survived)

    def record(self) -> dict[str, Any]:
        """What the halt's event says about it."""
        return {"found": self.found, "scanned": self.scanned,
                "killed": [{**proc.row(), "signal": sig} for proc, sig in self.killed],
                "survived": [proc.row() for proc in self.survived], "gone": len(self.gone)}


async def sweep(space: str, session_id: str, *, grace_s: float = GRACE_S) -> Swept:
    """Stop every process of this user that carries ``session_id``'s marker
    for this Space."""
    started = time.perf_counter()
    found, scanned = await asyncio.to_thread(scan, space, session_id)
    scanned_at = time.perf_counter()
    killed, gone, survived = await stop(found, grace_s=grace_s)
    return Swept(scanned=scanned, killed=tuple(killed), gone=tuple(gone), survived=tuple(survived),
                 scan_s=scanned_at - started, kill_s=time.perf_counter() - scanned_at)


def scan(space: str, session_id: str) -> tuple[list[Marked], int]:
    """The processes of this user carrying the session's marker, and how
    many of this user's processes were looked at. Blocking."""
    prefix = f"{MARKER}={space}:{session_id}:".encode()
    spared = _self_and_ancestors()
    uid = os.getuid()
    try:
        entries = [e for e in os.scandir("/proc") if e.name.isdigit()]
    except OSError:
        return [], 0
    found, scanned = [], 0
    for entry in entries:
        pid = int(entry.name)
        try:
            if pid in spared or entry.stat(follow_symlinks=False).st_uid != uid:
                continue
        except OSError:
            continue                    # gone already
        scanned += 1
        if _epoch(pid, prefix) is None:
            continue
        ticks = process_start_ticks(pid)
        epoch = _epoch(pid, prefix)     # read again while the pid is known to have `ticks`
        if ticks is None or epoch is None or process_start_ticks(pid) != ticks:
            continue
        found.append(Marked(pid, ticks, epoch, _name(pid)))
    return found, scanned


async def stop(found: Sequence[Marked], *, grace_s: float = GRACE_S
               ) -> tuple[list[tuple[Marked, str]], list[Marked], list[Marked]]:
    """SIGTERM through a pidfd to each process still running as found, then
    SIGKILL to those left after ``grace_s``. Returns (killed, each with the
    signal that ended it; gone before a signal; survived)."""
    opened = await asyncio.to_thread(_open, found)
    gone = [proc for proc in found if proc not in opened.values()]
    try:
        alive = await signal_and_wait(list(opened), signal.SIGTERM, grace_s)
        left = await signal_and_wait(alive, signal.SIGKILL, grace_s)
    finally:
        for fd in opened:
            os.close(fd)
    killed = [(opened[fd], "SIGTERM") for fd in opened if fd not in alive]
    killed += [(opened[fd], "SIGKILL") for fd in alive if fd not in left]
    return killed, gone, [opened[fd] for fd in left]


def _open(found: Sequence[Marked]) -> dict[int, Marked]:
    """A pidfd for each process whose pid still has its recorded start time
    once the fd names it. Blocking."""
    opened: dict[int, Marked] = {}
    for proc in found:
        try:
            fd = os.pidfd_open(proc.pid)
        except OSError:
            continue                    # gone already
        if process_start_ticks(proc.pid) == proc.start_ticks:
            opened[fd] = proc
        else:
            os.close(fd)                # the pid was reused: not ours
    return opened


def _epoch(pid: int, prefix: bytes) -> int | None:
    """The epoch in the process's marker for this session, or None when its
    starting environment has none (or can't be read)."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    if prefix not in environ:
        return None
    for var in environ.split(b"\0"):
        if var.startswith(prefix):
            epoch = var[len(prefix):]
            return int(epoch) if epoch.isdigit() else None
    return None


def _name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _self_and_ancestors() -> set[int]:
    pids, pid = set(), os.getpid()
    while pid and pid not in pids:
        pids.add(pid)
        pid = process_parent(pid) or 0
    return pids
