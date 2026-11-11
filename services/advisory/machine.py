"""The container's CPU and memory, read from its cgroup (v2).

What counts is what the kernel acts on inside this container, not the
host's view: the CPU quota (``cpu.max``, 2 CPUs here, on a 16-core host),
and the memory the out-of-memory killer weighs, the working set
(``memory.current`` minus the reclaimable ``inactive_file``), against
``memory.max``. Pressure (``*.pressure``) says whether work is waiting, not
just how busy the machine is; ``memory.events`` counts the near-misses.

One sample reads about eight small files (well under a millisecond). A
file that can't be read leaves its facts unknown; on a machine without
cgroup v2 memory falls back to ``/proc/meminfo``, and the rest is unknown.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from services.advisory.facts import Fact, Reading, fact_of
from services.advisory.model import Machine

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC = Path("/proc")


@dataclass(frozen=True)
class Sample:
    """One reading of the cgroup files, at ``at`` (epoch seconds). None where
    a file couldn't be read."""

    at: float
    cpu_limit: float | None = None
    cpu_usage_usec: int | None = None
    cpu_pressure: float | None = None
    memory_limit: int | None = None
    memory_used: int | None = None
    memory_inactive_file: int | None = None
    memory_pressure: float | None = None
    memory_pressure_full: float | None = None
    io_pressure: float | None = None
    oom_kills: int | None = None
    memory_limit_hits: int | None = None


def cgroup_dir(root: Path = CGROUP_ROOT, proc: Path = PROC) -> Path:
    """This process's cgroup folder (``0::<path>`` in ``/proc/self/cgroup``);
    the root when the path can't be read or isn't mounted here."""
    try:
        for line in (proc / "self" / "cgroup").read_text().splitlines():
            if line.startswith("0::"):
                candidate = root / line[3:].lstrip("/")
                if (candidate / "memory.current").exists():
                    return candidate
    except OSError:
        pass
    return root


def read_sample(at: float, folder: Path, proc: Path = PROC) -> Sample:
    """One sample of ``folder``'s cgroup. Blocking: call it in a thread."""
    memory_stat = parse_flat(_read(folder / "memory.stat"))
    events = parse_flat(_read(folder / "memory.events"))
    memory_limit = parse_max(_read(folder / "memory.max"))
    memory_used = parse_int(_read(folder / "memory.current"))
    inactive = memory_stat.get("inactive_file")
    if memory_used is None:
        # No cgroup v2 memory controller here: the kernel's own count of the machine's memory.
        meminfo = parse_meminfo(_read(proc / "meminfo"))
        if "MemTotal" in meminfo and "MemAvailable" in meminfo:
            memory_limit, memory_used, inactive = meminfo["MemTotal"], meminfo["MemTotal"] - meminfo["MemAvailable"], 0
    cpu = parse_pressure(_read(folder / "cpu.pressure"))
    memory = parse_pressure(_read(folder / "memory.pressure"))
    io = parse_pressure(_read(folder / "io.pressure"))
    return Sample(at=at, cpu_limit=parse_cpu_max(_read(folder / "cpu.max")) or _cpu_count(),
                  cpu_usage_usec=parse_flat(_read(folder / "cpu.stat")).get("usage_usec"),
                  cpu_pressure=cpu.get("some"), memory_limit=memory_limit or _meminfo_total(proc),
                  memory_used=memory_used, memory_inactive_file=inactive, memory_pressure=memory.get("some"),
                  memory_pressure_full=memory.get("full"), io_pressure=io.get("some"),
                  oom_kills=events.get("oom_kill"), memory_limit_hits=events.get("max"))


def machine(current: Sample | None, previous: Sample | None, now_s: float) -> Machine:
    """The published facts from the latest sample, and the one before it
    (CPU in use is the difference of two)."""
    if current is None:
        return Machine()

    def measured(value) -> Fact:
        return fact_of(Reading(value, "measured", current.at) if value is not None else None, now_s, "machine")

    def derived(value) -> Fact:
        return fact_of(Reading(value, "derived", current.at) if value is not None else None, now_s, "machine")

    working_set = None
    if current.memory_used is not None and current.memory_inactive_file is not None:
        working_set = max(current.memory_used - current.memory_inactive_file, 0)
    available = max(current.memory_limit - working_set, 0) \
        if current.memory_limit is not None and working_set is not None else None
    return Machine(cpu_limit=measured(current.cpu_limit), cpu_used=derived(cpu_used(previous, current)),
                   cpu_pressure=measured(current.cpu_pressure), memory_limit=measured(current.memory_limit),
                   memory_used=measured(current.memory_used), memory_working_set=derived(working_set),
                   memory_available=derived(available), memory_pressure=measured(current.memory_pressure),
                   memory_pressure_full=measured(current.memory_pressure_full),
                   io_pressure=measured(current.io_pressure), oom_kills=measured(current.oom_kills),
                   memory_limit_hits=measured(current.memory_limit_hits))


def cpu_used(previous: Sample | None, current: Sample) -> float | None:
    """CPUs in use between two samples: the cgroup's CPU time over the wall
    time, to two decimals."""
    if previous is None or previous.cpu_usage_usec is None or current.cpu_usage_usec is None:
        return None
    elapsed = current.at - previous.at
    used = current.cpu_usage_usec - previous.cpu_usage_usec
    if elapsed <= 0 or used < 0:
        return None
    return round(used / 1e6 / elapsed, 2)


# ── parsing (pure) ─────────────────────────────────────────────────────────


def parse_int(text: str | None) -> int | None:
    try:
        return int(text.strip()) if text is not None else None
    except ValueError:
        return None


def parse_max(text: str | None) -> int | None:
    """``memory.max``: bytes, or None for ``max`` (no limit)."""
    return None if text is None or text.strip() == "max" else parse_int(text)


def parse_cpu_max(text: str | None) -> float | None:
    """``cpu.max`` (``<quota> <period>``) as CPUs; None for ``max`` (no quota)."""
    parts = (text or "").split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return round(quota / period, 2) if period > 0 else None


def parse_flat(text: str | None) -> dict[str, int]:
    """A flat-keyed cgroup file (``memory.stat``, ``memory.events``,
    ``cpu.stat``): ``<key> <integer>`` per line."""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition(" ")
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out


def parse_pressure(text: str | None) -> dict[str, float]:
    """A pressure file's ``avg10`` per line (``some``, ``full``): the share of
    the last 10 s that tasks waited, in %."""
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        kind, *fields = line.split() or [""]
        values: Mapping[str, str] = dict(f.split("=", 1) for f in fields if "=" in f)
        try:
            out[kind] = float(values["avg10"])
        except (KeyError, ValueError):
            continue
    return out


def parse_meminfo(text: str | None) -> dict[str, int]:
    """``/proc/meminfo``, in bytes."""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) * (1024 if parts[1:] == ["kB"] else 1)
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _cpu_count() -> float | None:
    count = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return float(count) if count else None


def _meminfo_total(proc: Path) -> int | None:
    return parse_meminfo(_read(proc / "meminfo")).get("MemTotal")
