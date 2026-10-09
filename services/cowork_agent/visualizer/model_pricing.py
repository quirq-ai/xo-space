"""Per-model token prices, for the cost a usage record claims.

Two sources, in order:

1. **Argus's pricing table** (``argus-code``, already a dependency): the same
   table the Sessions tab's ``cost_usd`` comes from, so the two agree. Loaded
   once, newest table first, so an ``argus pricing refresh`` takes effect on
   the next restart.
2. **:data:`_SUPPLEMENT`** — models Argus's bundled table does not price yet.
   Used only for a model Argus lacks, so a newer Argus table always wins.

A model neither knows is **unpriced**: :func:`price_turn` returns ``None`` and
the caller omits its cost rather than claiming $0, which would read as
"measured free".

Prices are USD per million tokens. The token fields are the disjoint ones
:class:`~services.cowork_agent.visualizer.ingest.events.UsageObserved`
carries (``input`` excludes cache reads/writes for every runtime). Cache
writes are priced at the 5-minute rate: the events don't split 5m from 1h
writes, and this matches Argus's own fallback (a lower bound when a runtime
writes 1h entries).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

_PER_MTOK = 1_000_000
_DATE_SUFFIX = re.compile(r"-\d{8}$")


@dataclass(frozen=True, slots=True)
class Rates:
    """USD per million tokens for one model (one context tier)."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True, slots=True)
class ModelPrice:
    base: Rates
    #: ``(prompt-token threshold, rates)``: a turn whose prompt (input + cache
    #: read + cache write) exceeds the threshold is priced at these instead.
    long_context: Optional[tuple[int, Rates]] = None


def _anthropic(inp: float, out: float, cache_read: Optional[float] = None) -> Rates:
    """Anthropic list rates; unpublished cache rates use the documented
    multipliers (5-minute write 1.25x input, read 0.1x input)."""
    return Rates(
        input=inp,
        output=out,
        cache_read=cache_read if cache_read is not None else inp * 0.1,
        cache_write=inp * 1.25,
    )


#: Anthropic first-party list prices (Claude API reference, 2026-10-06) for
#: models Argus's bundled table predates. Remove an entry once Argus ships it.
_SUPPLEMENT: dict[str, ModelPrice] = {
    "claude-fable-5-1":  ModelPrice(_anthropic(10.00, 50.00, cache_read=0.25)),
    "claude-mythos-5-1": ModelPrice(_anthropic(10.00, 50.00, cache_read=0.25)),
    "claude-opus-5-5":   ModelPrice(_anthropic(4.00, 20.00, cache_read=0.20)),
    "claude-opus-5":     ModelPrice(_anthropic(5.00, 25.00)),
    "claude-sonnet-5-5": ModelPrice(_anthropic(2.00, 10.00, cache_read=0.20)),
    "claude-sonnet-5":   ModelPrice(_anthropic(2.00, 10.00, cache_read=0.20)),
    # Haiku 5.5 is tiered: prompts over 100K tokens bill at $0.50 / $2.50.
    "claude-haiku-5-5":  ModelPrice(
        _anthropic(0.10, 0.50),
        long_context=(100_000, _anthropic(0.50, 2.50)),
    ),
}


@dataclass(frozen=True, slots=True)
class TurnCost:
    """One turn's cost in USD, broken down the way AUDR's ``cost.llm`` is."""

    input: float
    output: float
    cache_read: float
    cache_write: float

    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_read + self.cache_write


@lru_cache(maxsize=1)
def _argus_prices() -> dict[str, ModelPrice]:
    try:
        from argus.pricing.load import load_pricing_table
    except ImportError:
        return {}
    try:
        table = load_pricing_table()
    except Exception:  # noqa: BLE001 - a broken table must not stop usage records
        logger.warning("model pricing: Argus pricing table unreadable; using the supplement only")
        return {}
    prices: dict[str, ModelPrice] = {}
    for name, p in table.models.items():
        cache_write = p.cache_write_5m if p.cache_write_5m is not None else p.input
        prices[name] = ModelPrice(Rates(p.input, p.output, p.cache_read, cache_write))
    return prices


def price_for(model: Optional[str]) -> Optional[ModelPrice]:
    """The model's price, or ``None`` when no source knows it.

    A dated snapshot id (``claude-sonnet-4-20250514``) falls back to its
    undated alias.
    """
    if not model:
        return None
    argus = _argus_prices()
    for name in (model, _DATE_SUFFIX.sub("", model)):
        price = argus.get(name) or _SUPPLEMENT.get(name)
        if price is not None:
            return price
    return None


def price_turn(
    model: Optional[str],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> Optional[TurnCost]:
    """The turn's cost, or ``None`` when the model is unpriced."""
    price = price_for(model)
    if price is None:
        return None
    rates = price.base
    if price.long_context is not None:
        threshold, long_rates = price.long_context
        if input_tokens + cache_read_tokens + cache_write_tokens > threshold:
            rates = long_rates
    return TurnCost(
        input=input_tokens * rates.input / _PER_MTOK,
        output=output_tokens * rates.output / _PER_MTOK,
        cache_read=cache_read_tokens * rates.cache_read / _PER_MTOK,
        cache_write=cache_write_tokens * rates.cache_write / _PER_MTOK,
    )
