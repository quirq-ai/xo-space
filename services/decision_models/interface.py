"""What every decision model speaks: the questions, the answers and the
:class:`Provider` each provider package exposes as ``PROVIDER``.

A provider maps these questions to its own API and its replies back to these
answers; the rules that make both behave the same (see the package docstring)
live here, not in a provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Optional, Protocol, Union

#: A tag's probability at or above which it applies, at or below which it doesn't.
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


Question = Union[Choice, Tags]


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


def kind_of(question: Question) -> str:
    """``"choice"`` or ``"tags"``: the kind of the answer ``question`` gets."""
    return "choice" if isinstance(question, Choice) else "tags"


def failed(provider: str, questions: list[Question], error: str) -> Result:
    """Every question answered with ``error`` and nothing else."""
    return Result(provider, {q.id: Answer(q.id, kind_of(q), error=error) for q in questions})


def applies_from(p: Optional[float]) -> Optional[bool]:
    if not isinstance(p, (int, float)):
        return None
    return True if p >= YES_AT else False if p <= NO_AT else None


class Ask(Protocol):
    def __call__(self, content: str, questions: list[Question], *, model: str,
                 timeout: float) -> Awaitable[Result]: ...


@dataclass(frozen=True)
class Provider:
    """One decision model, as the router lists, connects and asks it.

    ``id`` is the public provider id (the router's settings and API name it);
    ``env_key`` is its key's name in ``router.env``. ``ask`` never raises for a
    provider failure: it answers every question with ``error`` set.
    """

    id: str
    label: str
    docs_url: str
    env_key: str
    default_model: str
    ask: Ask
