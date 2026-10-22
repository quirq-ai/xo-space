"""One process's sub-agents in memory (design ch 14 §2.1).

``RunProgress`` is ephemeral: its numbers never reach the store while a run
works (only its ``background`` does, the session's pump writing it once).
They are kept here, per run, and written to the run's row when the run ends;
until then the routes read them from here. The per-session aggregate the
stream carries (``running``, ``waiting``, ``tokens``) is computed from it.

Owned by the session's pump (``LiveSession``); everything here runs on the
event loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.cowork_agent.adapters import contract as c


@dataclass
class Progress:
    """A run's numbers so far, as its runtime last reported them."""

    tokens: int = 0
    tool_calls: int = 0
    last_tool: str | None = None
    at: str | None = None                   # its last activity, as the store writes times


@dataclass
class LiveRuns:
    live: set[str] = field(default_factory=set)               # runs working now that the store has rows for
    progress: dict[str, Progress] = field(default_factory=dict)

    def progressed(self, event: c.RunProgress, now: str) -> bool:
        """Keep what ``event`` says; whether the run is one the store shows."""
        self.progress[event.run] = Progress(event.tokens, event.tool_calls, event.last_tool, now)
        return event.run in self.live

    def ended(self, run: str) -> Progress | None:
        """The run ended: its numbers, for its row."""
        self.live.discard(run)
        return self.progress.pop(run, None)

    def clear(self) -> None:
        self.live.clear()
        self.progress.clear()

    def counts(self, waiting: set[str]) -> tuple[int, int, int]:
        """(running, waiting, tokens) of the stored runs working now;
        ``waiting``: the runs whose call is held for the person."""
        held = self.live & waiting
        tokens = sum(p.tokens for run, p in self.progress.items() if run in self.live)
        return len(self.live - held), len(held), tokens
