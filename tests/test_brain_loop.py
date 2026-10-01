"""The brain's background tick: off by default, re-learns only what
changed, fades, discovers."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from services.brain import config, loop

from tests._brain_support import ALPHA_README, BrainSandbox


class LoopTests(BrainSandbox):
    def test_the_loop_is_off_unless_enabled(self) -> None:
        self.assertFalse(config.loop_enabled())
        asyncio.run(asyncio.wait_for(loop.start_brain_loop(), timeout=2))   # returns at once
        with patch.dict(os.environ, {"BRAIN_ENABLED": "1", "BRAIN_TICK_S": "5"}):
            self.assertTrue(config.loop_enabled())
            self.assertEqual(config.tick_seconds(), config.MIN_TICK_S)

    def test_a_tick_relearns_only_changed_sources(self) -> None:
        root = self.project("alpha", {"README.md": ALPHA_README})
        sid = self.register("alpha", root)
        first = asyncio.run(loop.tick())
        self.assertEqual(first["learned"], [sid])
        self.assertIsNotNone(first["discovered"])
        second = asyncio.run(loop.tick())
        self.assertEqual((second["learned"], second["discovered"]), ([], None))
        (root / "README.md").write_text(ALPHA_README + "\n## Bloom filter\n\nBloom filters skip missing terms fast.\n")
        third = asyncio.run(loop.tick())
        self.assertEqual(third["learned"], [sid])
        self.assertTrue(self.query("SELECT id FROM pieces WHERE key = 'bloom filter'"))

    def test_a_missing_folder_is_skipped_not_fatal(self) -> None:
        sid = self.register("ghost", self.base / "nowhere")
        out = asyncio.run(loop.tick())
        self.assertEqual((out["learned"], out["failed"]), ([], []))
        self.assertEqual(self.query("SELECT status FROM sources WHERE id = ?", sid), [("new",)])
