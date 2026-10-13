"""Decision models behind one interface: Levanto Sage and TypeSafe Jev.

A caller asks a handful of typed questions about one piece of content
(``choice``: pick one option; ``tags``: which of these apply) and gets back
normalised answers, whichever provider answered. The interface is shaped like
SystemOne (the request format Jev defined and Sage also serves); research:
``docs/architecture/research-decision-models.md``.

What is decided here, not by the provider, so both behave the same:

- **confidence** is recomputed locally (the providers define it differently):
  for a choice, the probability of the chosen option;
- **abstaining**: a choice abstains when the provider gave no pick (Sage's
  ``null``) or its probability is under the caller's threshold;
- **tags** a provider lacks (Jev) are asked as one yes/no question each, and
  ``applies`` is derived with fixed thresholds (≥ 0.8 yes, ≤ 0.2 no, between: unsure).

Keys are read only in the provider modules (``sage.py`` through
``services/levanto/client.py``; ``jev.py``). Nothing here raises for a
provider failure: a failed call is an answer with ``error`` set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

YES_AT, NO_AT = 0.8, 0.2


@dataclass
class Choice:
    id: str
    instructions: str
    options: dict[str, str]          # option id -> description


@dataclass
class Tags:
    id: str
    instructions: str
    tags: dict[str, str]             # tag id -> description


@dataclass
class Answer:
    id: str
    kind: str                        # "choice" | "tags"
    chosen: Optional[str] = None
    probability: Optional[float] = None
    probabilities: dict[str, float] = field(default_factory=dict)
    applies: dict[str, Optional[bool]] = field(default_factory=dict)   # tags: id -> True/False/None
    error: Optional[str] = None      # provider failure kind; the answer is then empty

    def abstains(self, threshold: float) -> bool:
        return self.error is not None or self.chosen is None or (self.probability or 0.0) < threshold


@dataclass
class Result:
    provider: str
    answers: dict[str, Answer]
    input_tokens: int = 0
    latency_ms: float = 0.0


def applies_from(p: Optional[float]) -> Optional[bool]:
    if not isinstance(p, (int, float)):
        return None
    return True if p >= YES_AT else False if p <= NO_AT else None


async def ask(provider: str, content: str, questions: list, *, timeout: float = 8.0) -> Result:
    """Ask every question about ``content`` with ``provider`` (``sage`` or ``jev``)."""
    if provider == "sage":
        from services.decision_models import sage as impl
    elif provider == "jev":
        from services.decision_models import jev as impl
    else:
        raise ValueError(f"unknown decision model {provider!r}")
    return await impl.ask(content, questions, timeout=timeout)


def configured(provider: str) -> bool:
    if provider == "sage":
        from services.decision_models import sage as impl
    elif provider == "jev":
        from services.decision_models import jev as impl
    else:
        return False
    return impl.configured()


__all__ = ["Answer", "Choice", "Result", "Tags", "applies_from", "ask", "configured"]
