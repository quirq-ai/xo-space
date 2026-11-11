"""What each live session costs: the memory of its process tree now, and its
last turn's spend and context size.

* **Memory** is PSS (``/proc/<pid>/smaps_rollup``) summed over the session's
  process and every descendant. PSS splits a shared page between the
  processes that map it, so a browser or a runtime the sessions share is
  counted once in total, not once per session (RSS would count it in each).
* **The tree** is found from the session's own process (the engine names it
  with ``track_process``) by parent links, in one pass over ``/proc``. The
  process is held by its pid and its start time, so a pid the kernel reused
  for another process is never taken for it. A process that left the tree
  (a daemon that forked twice) isn't counted; W4b finds processes by the
  engine's marker instead.
* **A turn's numbers** are what its runtime reported at its end
  (``record_turn``): the turn's own cost, never a running total, and the
  context's size. They describe that turn, so they don't age out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from services.advisory.facts import Reading, fact_of
from services.advisory.model import Session
from services.cowork_agent.adapters.contract import TokenUsage

PROC = Path("/proc")
PAGE_KB = 1024


@dataclass(frozen=True)
class Root:
    pid: int
    start_ticks: int | None


@dataclass
class SessionState:
    account: str | None = None
    root: Root | None = None
    memory: Reading[int] | None = None
    processes: Reading[int] | None = None
    turn_cost: Reading[float] | None = None
    context_tokens: Reading[int] | None = None
    context_window: Reading[int] | None = None

    def record_turn(self, usage: TokenUsage, at: float) -> None:
        self.turn_cost = Reading(usage.cost_usd, "reported", at) if usage.cost_usd is not None else None
        self.context_tokens = Reading(usage.context_tokens, "reported", at) \
            if usage.context_tokens is not None else None
        if usage.context_window is not None:
            self.context_window = Reading(usage.context_window, "reported", at)

    def publish(self, now_s: float) -> Session:
        return Session(account=self.account, memory_pss=fact_of(self.memory, now_s, "session"),
                       processes=fact_of(self.processes, now_s, "session"),
                       turn_cost_usd=fact_of(self.turn_cost, now_s, "turn"),
                       context_tokens=fact_of(self.context_tokens, now_s, "turn"),
                       context_window=fact_of(self.context_window, now_s, "turn"))


@dataclass(frozen=True)
class Measured:
    """One session's tree, measured: its PSS in bytes (None when no process
    of it could be read) and how many processes it has."""

    pss: int | None
    processes: int


@dataclass
class _Proc:
    ppid: int
    start_ticks: int | None
    children: list[int] = field(default_factory=list)


def measure(roots: Mapping[str, Root], proc: Path = PROC) -> dict[str, Measured]:
    """Every session's tree, measured. Blocking: call it in a thread."""
    table = _process_table(proc)
    out: dict[str, Measured] = {}
    for session_id, root in roots.items():
        found = table.get(root.pid)
        if found is None or (root.start_ticks is not None and found.start_ticks != root.start_ticks):
            out[session_id] = Measured(None, 0)         # its process ended (or the pid is another's now)
            continue
        pids = _tree(root.pid, table)
        sizes = [size for pid in pids if (size := pss_bytes(pid, proc)) is not None]
        out[session_id] = Measured(sum(sizes) if sizes else None, len(pids))
    return out


def start_ticks(pid: int, proc: Path = PROC) -> int | None:
    """When ``pid`` started, in clock ticks since boot (``stat`` field 22)."""
    fields = _stat_fields(proc / str(pid) / "stat")
    return _field(fields, 22)


def pss_bytes(pid: int, proc: Path = PROC) -> int | None:
    """``pid``'s proportional set size, or None when it can't be read (gone,
    or another user's)."""
    try:
        with open(proc / str(pid) / "smaps_rollup", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * PAGE_KB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _process_table(proc: Path) -> dict[int, _Proc]:
    table: dict[int, _Proc] = {}
    try:
        entries = [e for e in proc.iterdir() if e.name.isdigit()]
    except OSError:
        return table
    for entry in entries:
        fields = _stat_fields(entry / "stat")
        ppid = _field(fields, 4)
        if ppid is not None:
            table[int(entry.name)] = _Proc(ppid, _field(fields, 22))
    for pid, found in table.items():
        parent = table.get(found.ppid)
        if parent is not None and found.ppid != pid:
            parent.children.append(pid)
    return table


def _tree(root: int, table: Mapping[int, _Proc]) -> list[int]:
    """``root`` and every descendant."""
    seen: dict[int, None] = {}
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen[pid] = None
            stack.extend(table[pid].children if pid in table else ())
    return list(seen)


def _stat_fields(path: Path) -> list[str] | None:
    """A ``stat`` file from field 3 on. The command name (field 2) is in
    parentheses and may hold spaces or ')', so fields count from the last ')'."""
    try:
        text = path.read_text()
    except OSError:
        return None
    return text[text.rfind(")") + 2:].split()


def _field(fields: list[str] | None, number: int) -> int | None:
    """Field ``number`` (1-based, as in proc(5))."""
    try:
        return int(fields[number - 3]) if fields else None
    except (IndexError, ValueError):
        return None
