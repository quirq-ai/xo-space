"""A fact: one value, with where it came from and when it was seen.

The advisory never shows a guess as a fact. Each value says how it was
learnt (``source``) and when (``observed_at``), and a value that is too old
for its kind is unknown: its value is None and ``known`` is false, while
``observed_at`` still says when it was last seen. A fact ages out when a
snapshot is published (every few seconds) and again when it is served
(``Fact.at``), so a stopped sampler can never leave a stale value showing.

* ``measured``: read from the kernel or the vendor just now;
* ``reported``: sent by a runtime during a turn;
* ``derived``: computed from other facts;
* ``unknown``: never seen, stale, or no longer true (a window that reset).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar

from services.timestamps import TS_FORMAT

Source = Literal["measured", "reported", "derived", "unknown"]
T = TypeVar("T")

#: How long each kind of fact stays true without a new reading, in seconds.
#: Usage is read at least every 5 minutes while a read is possible, so three
#: missed reads make it unknown; what an account can do beyond its plan
#: changes rarely; the machine is sampled every 2 seconds and a session's
#: memory every 15. A finished turn's numbers describe that turn: they
#: never age out.
MAX_AGE_S = {"usage": 15 * 60.0, "paid": 60 * 60.0, "machine": 10.0, "session": 60.0, "turn": math.inf}


def iso_at(seconds: float) -> str:
    """Epoch ``seconds`` as UTC, ending in Z."""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(TS_FORMAT)


@dataclass(frozen=True)
class Fact(Generic[T]):
    value: T | None = None
    source: Source = "unknown"
    observed_at: str | None = None
    stale_after: float | None = None    # epoch seconds after which it is unknown (None: never)

    @property
    def known(self) -> bool:
        """Known as of the snapshot that holds it (``at`` says as of now)."""
        return self.source != "unknown" and self.value is not None

    def at(self, now_s: float) -> "Fact[T]":
        """This fact as it stands at ``now_s``: unknown once too old."""
        if self.known and self.stale_after is not None and now_s > self.stale_after:
            return Fact(None, "unknown", self.observed_at)
        return self

    def as_dict(self, now_s: float) -> dict[str, Any]:
        fact = self.at(now_s)
        return {"value": fact.value, "source": fact.source, "observed_at": fact.observed_at,
                "age_s": _age(fact.observed_at, now_s), "known": fact.known}


UNKNOWN: Fact[Any] = Fact()


@dataclass(frozen=True)
class Reading(Generic[T]):
    """What the advisory keeps of one value: the last one seen, how, and when
    (epoch seconds). It becomes a ``Fact`` when a snapshot is published."""

    value: T
    source: Source
    at: float

    def fact(self, now_s: float, kind: str) -> Fact[T]:
        """The published fact: unknown once older than its kind allows."""
        max_age = MAX_AGE_S[kind]
        if now_s - self.at > max_age:
            return Fact(None, "unknown", iso_at(self.at))
        return Fact(self.value, self.source, iso_at(self.at), self.at + max_age if math.isfinite(max_age) else None)


def fact_of(reading: Reading[T] | None, now_s: float, kind: str) -> Fact[T]:
    return reading.fact(now_s, kind) if reading is not None else UNKNOWN


def _age(observed_at: str | None, now_s: float) -> float | None:
    if observed_at is None:
        return None
    seen = datetime.strptime(observed_at, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    return round(max(now_s - seen, 0.0), 1)
