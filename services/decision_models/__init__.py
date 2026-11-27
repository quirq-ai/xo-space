"""Decision models behind one interface: Levanto Sage and TypeSafe Jev.

A caller asks a handful of typed questions about one piece of content
(``choice``: pick one option; ``tags``: which of these apply) and gets back
normalised answers, whichever provider answered. The interface
(``interface.py``) is shaped like SystemOne, the request format Jev defined
and Sage also serves. The providers' own APIs: https://docs.levanto.ai and
https://docs.typesafe.ai/api.md.

What is decided here, not by the provider, so both behave the same:

- **confidence** is recomputed locally (the providers define it differently):
  for a choice, the probability of the chosen option;
- **abstaining**: a choice abstains when the provider gave no pick (Sage's
  ``null``) or its probability is under the caller's threshold;
- **tags** a provider lacks (Jev) are asked as one yes/no question each, and
  ``applies`` is derived with fixed thresholds (≥ 0.8 yes, ≤ 0.2 no, between: unsure).

Each provider is a package beside this one, ``levanto/`` (provider ``sage``)
and ``jev/`` (provider ``jev``): its ``__init__`` exposes ``PROVIDER``, a
:class:`Provider`, and its ``client.py`` is the only module that knows its
host. Both clients make the one call in ``transport.py``. A new provider is a
new package listed in ``PROVIDERS``.

The keys are the router's (``keys.py``: ``router.env``, §0.81), read per
call. Nothing here raises for a provider failure: a failed call is an answer
with ``error`` set. Used by storm's router (``storm/chats/router.py``).
"""

from __future__ import annotations

from services.decision_models import jev, levanto
from services.decision_models.interface import Answer, Choice, Provider, Question, Result, Tags

#: Provider id -> provider, in the order the router lists them.
PROVIDERS: dict[str, Provider] = {p.id: p for p in (levanto.PROVIDER, jev.PROVIDER)}

#: How long a routing decision may take before the agent routes instead.
TIMEOUT_S = 8.0


def provider(provider_id: str) -> Provider:
    """The provider called ``provider_id``; ``ValueError`` for an unknown one."""
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise ValueError(f"unknown decision model {provider_id!r}") from None


async def ask(provider_id: str, content: str, questions: list[Question], *, model: str,
              timeout: float = TIMEOUT_S) -> Result:
    """Ask every question about ``content`` with the provider ``provider_id``."""
    return await provider(provider_id).ask(content, questions, model=model, timeout=timeout)


__all__ = ["PROVIDERS", "TIMEOUT_S", "Answer", "Choice", "Provider", "Question", "Result", "Tags", "ask",
           "provider"]
