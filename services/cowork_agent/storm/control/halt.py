"""What ``Engine.halt`` did to one session, and the event that records it.

A halt has three phases, each timed so the "under 1 s" promise can be
measured where it is kept or missed: the interrupt (the running turn and its
live sub-agents end), the release (the session's process is closed, so no
idle CLI is left to start a turn by itself) and the sweep (every process
still carrying the session's marker is stopped).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.cowork_agent.storm.control.sweep import Swept

#: The event a halt appends, once, after all three phases.
EVENT = "session.halted"


@dataclass(frozen=True)
class Halted:
    session_id: str
    reason: str                          # why, in the caller's words (``stopped``, ``paid_threshold``, …)
    by: str                              # who asked: the founder, or the session or coordinator that did
    turn_id: str | None                  # the turn the interrupt ended
    interrupted: bool                    # the interrupt stopped a turn or sub-agents
    released: bool                       # a live process was closed
    swept: Swept
    interrupt_s: float
    release_s: float
    total_s: float

    @property
    def sweep_s(self) -> float:
        return self.swept.scan_s + self.swept.kill_s

    def record(self, status: str) -> dict[str, Any]:
        """The event's data; ``status`` is the session's as the event is written."""
        return {"status": status, "reason": self.reason, "by": self.by, "turn": self.turn_id,
                "interrupted": self.interrupted, "released": self.released, **self.swept.record(),
                "ms": {"interrupt": _ms(self.interrupt_s), "release": _ms(self.release_s),
                       "scan": _ms(self.swept.scan_s), "kill": _ms(self.swept.kill_s), "total": _ms(self.total_s)}}


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)
