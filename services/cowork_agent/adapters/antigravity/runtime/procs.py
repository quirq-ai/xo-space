"""What agy leaves running when it ends (storm ch 04 §7).

agy runs a command in a terminal of its own, so a command a sub-agent
started outlives agy's process (checked live: a sub-agent's ``sleep 45``
finished 40 s after its parent's interrupt). Before the runtime signals
agy, it notes agy's descendants; once agy has ended, those still running
are stopped. A process is named by its pid and its start time, so a pid
reused meanwhile is never touched. Linux only (``/proc``); elsewhere
nothing is noted.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def _stat(pid: int) -> tuple[int, int] | None:
    """(parent pid, start ticks), or None when it can't be read."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        if fields[0] == "Z":
            return None
        return int(fields[1]), int(fields[19])
    except (IndexError, ValueError):
        return None


def descendants(root: int) -> list[tuple[int, int]]:
    """Every live descendant of ``root``: (pid, start ticks). Blocking."""
    try:
        pids = [int(e.name) for e in os.scandir("/proc") if e.name.isdigit()]
    except OSError:
        return []
    parent: dict[int, tuple[int, int]] = {}
    for pid in pids:
        found = _stat(pid)
        if found is not None:
            parent[pid] = found
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in parent.items():
        children.setdefault(ppid, []).append(pid)
    out, stack = [], list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        out.append((pid, parent[pid][1]))
        stack.extend(children.get(pid, ()))
    return out


def _alive(pid: int, ticks: int) -> bool:
    found = _stat(pid)
    return found is not None and found[1] == ticks


def stop(noted: list[tuple[int, int]], grace_s: float = 2.0) -> int:
    """SIGTERM, then SIGKILL after ``grace_s``, each noted process still
    running as the same process. Returns how many survived it.
    Blocking."""
    left = [(pid, ticks) for pid, ticks in noted if _alive(pid, ticks)]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, _ in left:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            left = [(pid, ticks) for pid, ticks in left if _alive(pid, ticks)]
            if not left:
                break
            time.sleep(0.05)
    return len([1 for pid, ticks in noted if _alive(pid, ticks)])
