"""Token prices behind the ``cost`` an AUDR usage record claims."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.cowork_agent.visualizer import model_pricing


def _turn(model, **tokens):
    fields = dict(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
    fields.update(tokens)
    return model_pricing.price_turn(model, **fields)


class PriceTurnTests(unittest.TestCase):
    def test_each_counter_is_priced_at_its_own_rate(self) -> None:
        # claude-opus-5-5: $4 in, $20 out, $0.20 cache read, $5 cache write (1.25x).
        cost = _turn("claude-opus-5-5", input_tokens=2, output_tokens=74,
                     cache_read_tokens=21_869, cache_write_tokens=18_725)
        self.assertAlmostEqual(cost.input, 0.000008)
        self.assertAlmostEqual(cost.output, 0.00148)
        self.assertAlmostEqual(cost.cache_read, 0.0043738)
        self.assertAlmostEqual(cost.cache_write, 0.093625)
        self.assertAlmostEqual(cost.total, 0.0994868)

    def test_a_model_argus_prices_comes_from_argus(self) -> None:
        # Argus's table: claude-sonnet-4-6 at $3 / $15.
        cost = _turn("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(cost.total, 18.0)

    def test_argus_wins_over_the_supplement(self) -> None:
        argus = {"claude-opus-5-5": model_pricing.ModelPrice(model_pricing.Rates(1, 1, 1, 1))}
        with patch.object(model_pricing, "_argus_prices", return_value=argus):
            self.assertAlmostEqual(_turn("claude-opus-5-5", input_tokens=1_000_000).total, 1.0)

    def test_a_dated_snapshot_falls_back_to_its_alias(self) -> None:
        self.assertEqual(
            model_pricing.price_for("claude-sonnet-4-20250514"),
            model_pricing.price_for("claude-sonnet-4"),
        )

    def test_an_unknown_model_is_unpriced_not_free(self) -> None:
        self.assertIsNone(_turn("mystery-model", input_tokens=10))
        self.assertIsNone(_turn(None, input_tokens=10))

    def test_haiku_5_5_bills_a_long_prompt_at_the_higher_tier(self) -> None:
        short = _turn("claude-haiku-5-5", input_tokens=100_000, output_tokens=1_000_000)
        long = _turn("claude-haiku-5-5", input_tokens=100_001, output_tokens=1_000_000)
        self.assertAlmostEqual(short.output, 0.50)
        self.assertAlmostEqual(long.output, 2.50)

    def test_the_haiku_tier_counts_cached_prompt_tokens(self) -> None:
        cost = _turn("claude-haiku-5-5", input_tokens=10, cache_read_tokens=150_000,
                     output_tokens=1_000_000)
        self.assertAlmostEqual(cost.output, 2.50)


if __name__ == "__main__":
    unittest.main()
