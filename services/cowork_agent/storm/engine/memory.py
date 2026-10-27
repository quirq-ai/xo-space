"""The memory guard: may a new CLI process start now? (ch 07 §5.2)

Read before every new process and every ``READ_INTERVAL_S``. A warm session
needs no new memory and is never gated. It trips when:

* headroom (cgroup v2 ``memory.max − (memory.current − inactive_file)``, or
  ``/proc/meminfo``'s ``MemAvailable`` when there is no cgroup limit) is below
  the reserve (the larger of 1 GiB or 10 % of the limit) plus the new
  process's expected size, learned from the peaks seen per agent;
* ``/proc/pressure/memory`` ``some avg10`` is above 10 %;
* open file descriptors or the cgroup's processes are above 80 % of their
  limits.

The values are fixed and internal (owner: nothing to tune). A signal that
can't be read (no procfs, no cgroup) never trips the guard.
"""

from __future__ import annotations

import asyncio
import os
import resource
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RESERVE_MIN_BYTES = 1 << 30
RESERVE_FRACTION = 0.10
PRESSURE_AVG10_MAX = 10.0
FD_PID_MAX_FRACTION = 0.80
#: Until a CLI's own peak is seen: R-C §2 measured live claude sessions at
#: 335–475 MB.
DEFAULT_PROCESS_BYTES = 512 << 20
READ_INTERVAL_S = 5.0

_CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class MemoryReading:
    limit_bytes: int | None = None
    used_bytes: int | None = None
    pressure_avg10: float | None = None
    fds: int | None = None
    fd_limit: int | None = None
    pids: int | None = None
    pid_limit: int | None = None

    @property
    def headroom_bytes(self) -> int | None:
        if self.limit_bytes is None or self.used_bytes is None:
            return None
        return self.limit_bytes - self.used_bytes

    @property
    def reserve_bytes(self) -> int:
        return max(RESERVE_MIN_BYTES, int(RESERVE_FRACTION * (self.limit_bytes or 0)))

    def as_dict(self) -> dict:
        return {"limit_bytes": self.limit_bytes, "used_bytes": self.used_bytes,
                "headroom_bytes": self.headroom_bytes, "reserve_bytes": self.reserve_bytes,
                "pressure_some_avg10": self.pressure_avg10, "fds": self.fds, "fd_limit": self.fd_limit,
                "pids": self.pids, "pid_limit": self.pid_limit}


def read_memory() -> MemoryReading:
    """One reading of every signal. Blocking (file reads): run it in a thread."""
    cgroup = _own_cgroup()
    limit = used = None
    if cgroup is not None:
        limit = _int_file(cgroup / "memory.max")
        current = _int_file(cgroup / "memory.current")
        inactive = _stat_field(cgroup / "memory.stat", "inactive_file") or 0
        used = current - inactive if current is not None else None
    if limit is None:
        meminfo = _meminfo()
        if "MemTotal" in meminfo and "MemAvailable" in meminfo:
            limit, used = meminfo["MemTotal"], meminfo["MemTotal"] - meminfo["MemAvailable"]
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        fds = None
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return MemoryReading(
        limit_bytes=limit, used_bytes=used, pressure_avg10=_pressure(), fds=fds,
        fd_limit=soft if soft != resource.RLIM_INFINITY else None,
        pids=_int_file(cgroup / "pids.current") if cgroup else None,
        pid_limit=_int_file(cgroup / "pids.max") if cgroup else None)


def peak_rss_bytes(pid: int) -> int | None:
    """A process's peak resident memory (``VmHWM``), or None once it's gone."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class MemoryGuard:
    """The last reading and what the guard has learned of each agent's
    process size. ``reader`` is the blocking reading function (a test passes
    its own)."""

    def __init__(self, reader: Callable[[], MemoryReading] = read_memory) -> None:
        self._reader = reader
        self._expected: dict[str, int] = {}
        self.last: MemoryReading | None = None

    async def read(self) -> MemoryReading:
        self.last = await asyncio.to_thread(self._reader)
        return self.last

    def expected_bytes(self, agent: str) -> int:
        return self._expected.get(agent, DEFAULT_PROCESS_BYTES)

    def learn(self, agent: str, peak_bytes: int) -> None:
        """A process of ``agent`` peaked at ``peak_bytes``: expect at least
        that much from the next one."""
        if peak_bytes > self._expected.get(agent, 0):
            self._expected[agent] = peak_bytes

    def tripped(self, reading: MemoryReading, agent: str) -> str | None:
        """Why a new ``agent`` process may not start now, or None."""
        headroom = reading.headroom_bytes
        if headroom is not None and headroom < reading.reserve_bytes + self.expected_bytes(agent):
            return f"memory headroom {headroom >> 20} MiB is under the reserve"
        if reading.pressure_avg10 is not None and reading.pressure_avg10 > PRESSURE_AVG10_MAX:
            return f"memory pressure {reading.pressure_avg10:.1f} %"
        for used, limit, what in ((reading.fds, reading.fd_limit, "file descriptors"),
                                  (reading.pids, reading.pid_limit, "processes")):
            if used is not None and limit and used > FD_PID_MAX_FRACTION * limit:
                return f"{what} at {used} of {limit}"
        return None

    def expectations(self) -> dict[str, int]:
        return dict(self._expected)


def _own_cgroup() -> Path | None:
    """This process's cgroup v2 folder, or None without cgroup v2."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                folder = _CGROUP_ROOT / line[3:].strip().lstrip("/")
                return folder if (folder / "memory.current").exists() else None
    except OSError:
        pass
    return None


def _int_file(path: Path) -> int | None:
    """An integer file; None for ``max`` (no limit) or anything unreadable."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _stat_field(path: Path, name: str) -> int | None:
    try:
        for line in path.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == name and value.strip().isdigit():
                return int(value)
    except OSError:
        pass
    return None


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key] = int(parts[0]) * 1024
    except OSError:
        pass
    return out


def _pressure() -> float | None:
    try:
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            if line.startswith("some "):
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    if key == "avg10":
                        return float(value)
    except (OSError, ValueError):
        pass
    return None
