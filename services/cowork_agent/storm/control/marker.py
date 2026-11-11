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
they its.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    if not space or not session_id or not epoch.isdigit():
        return None
    return Mark(space, session_id, int(epoch))


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
