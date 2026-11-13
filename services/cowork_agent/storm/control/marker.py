"""The XO process marker: one environment variable on every process the
engine starts for a session, naming the Space's store, the session and the
epoch. The engine's launcher sets it (``session._Launcher``), so no runtime
can forget it, and it names no agent.

Children inherit their parent's environment, so the marker follows a
session's work wherever it goes: a shell's background job, a process that
left the process group with ``setsid``, a daemon that forked twice. The
kernel keeps each process's starting environment in ``/proc/<pid>/environ``,
so the sweep finds them by reading it at halt time, with no bookkeeping and
no cost in normal work.

The name is ``XO_STORM_PROC``. CLIs filter what their tools inherit, and the
name passes each filter of the installed CLIs (checked 11-11): one inherits
everything by default, then drops every name containing ``KEY``, ``SECRET``
or ``TOKEN``; another can scrub a fixed list of credential and config names.
A name with none of those words passes both.

The value is ``<space>:<session>:<epoch>``. ``space`` names the store this
engine holds (a hash of its path): a test server on a copy of the store has
the same session ids, and must never find the live Space's processes, nor
they its. Boot recovery stops a process the registry names only when it is
this Space's by the same token (``belongs_to``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from utils.runtime_env import ENV_STATE_ROOT

log = logging.getLogger(__name__)

MARKER = "XO_STORM_PROC"
#: Hex digits of the store path's hash in a marker.
SPACE_HEX = 12


@dataclass(frozen=True)
class Mark:
    space: str
    session_id: str
    epoch: int

    @property
    def value(self) -> str:
        return f"{self.space}:{self.session_id}:{self.epoch}"


def space_of(store_path: Path) -> str:
    """The marker's name for the store at ``store_path``: one engine holds a
    store (the engine lock), so its path names the Space it serves."""
    return hashlib.sha256(os.path.realpath(store_path).encode()).hexdigest()[:SPACE_HEX]


def marked(env: Mapping[str, str], mark: Mark) -> dict[str, str]:
    """``env`` with the marker set, replacing any it carried already."""
    return {**env, MARKER: mark.value}


def parse(value: str | None) -> Mark | None:
    """The mark in a marker's value, or None when it isn't one."""
    space, _, rest = (value or "").partition(":")
    session_id, _, epoch = rest.partition(":")
    if not space or not session_id or not (epoch.isascii() and epoch.isdigit()):
        return None
    return Mark(space, session_id, int(epoch))


def environ_of(pid: int) -> dict[str, str] | None:
    """The starting environment of process ``pid``, or None when it can't be
    read (gone, or another user's). Blocking."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    return dict(os.fsdecode(var).partition("=")[::2] for var in raw.split(b"\0") if var)


def belongs_to(space: str, state_root: Path | None) -> Callable[[int], bool]:
    """Whether a running process is the work of this Space's own server, the
    proof ``reap_orphan`` needs before stopping a process the registry names.
    A store copied from a live Space names that Space's processes, and a
    server on the copy must never stop them. Blocking.

    * With a marker: it is when the marker names this Space.
    * Without (a build before the marker started it): it is when its
      environment's state root is ``state_root``, compared by real path.
      ``None`` (no state root to prove it by) means it never is.
    * Its environment can't be read: it isn't.
    """
    root = os.path.realpath(state_root) if state_root is not None else None

    def belongs(pid: int) -> bool:
        env = environ_of(pid)
        if env is None:
            return False
        if MARKER in env:
            mark = parse(env[MARKER])
            return mark is not None and mark.space == space
        theirs = _state_root(env)
        return root is not None and theirs is not None and os.path.realpath(theirs) == root
    return belongs


def _state_root(env: Mapping[str, str]) -> str | None:
    """The state root a process with ``env`` uses, as ``quirq_state_dir``
    resolves it there: ``QUIRQ_STATE_ROOT`` if set, else ``$HOME/.quirq``,
    with ``~`` its own home. None when that can't be told (no home, a
    relative path)."""
    root = env.get(ENV_STATE_ROOT, "").strip() or "~/.quirq"
    if root == "~" or root.startswith("~/"):
        home = env.get("HOME", "")
        if not home:
            return None
        root = home.rstrip("/") + root[1:] or "/"       # as os.path.expanduser does with its home
    return root if os.path.isabs(root) else None


def forget_own(space: str) -> Mark | None:
    """A server started from a shell of one of its own sessions (an agent
    that restarted it) inherited that session's marker. It is dropped from
    this process's environment, so the processes the server starts outside a
    session (git, its helpers) never look like that session's work; the
    sweep spares the server itself and its ancestors. A marker of another
    Space is kept: a test server an agent started is that agent's work, and
    so is everything it starts. Returns the mark dropped."""
    mark = parse(os.environ.get(MARKER))
    if mark is None or mark.space != space:
        return None
    os.environ.pop(MARKER, None)
    log.warning("storm: this server was started from session %s (epoch %d); its own processes won't carry "
                "that session's marker", mark.session_id, mark.epoch)
    return mark
