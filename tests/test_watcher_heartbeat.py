"""T22 — the watcher's heartbeat is the only *observed* liveness signal.

Everything ``GET /api/quirq`` reported before this was configuration:
``enabled`` says what the watcher was asked to do, never whether the loop
is running, so a crashed watcher kept rendering "Live". These tests pin
the three halves of the fix — the path, the once-per-tick write, and the
staleness verdict the catalog derives from it — plus the property that
matters most operationally: the heartbeat can never take a tick down.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import quirq_catalog
from services.cowork_agent.visualizer import state, watcher


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _settings() -> dict:
    return {
        "agent_name": "test",
        "watcher_enabled": True,
        "watcher_interval_seconds": 1,
        "watcher_source_mode": "active",
    }


class HeartbeatPathTests(unittest.TestCase):
    def test_path_is_under_the_state_root_watcher_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".quirq"
            with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(root)}):
                self.assertEqual(
                    state.watcher_heartbeat_path(),
                    root / "watcher" / "heartbeat.json",
                )

    def test_reading_the_path_never_creates_a_directory(self) -> None:
        """The module contract (state.py docstring): the dir is created by
        the callers that write into it, so a read-only deployment does not
        mkdir just because something asked where the file would live."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "never-created"
            with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(root)}):
                path = state.watcher_heartbeat_path()
            self.assertFalse(root.exists())
            self.assertFalse(path.parent.exists())
            self.assertFalse(path.exists())


class HeartbeatWriteTests(unittest.TestCase):
    """A real ``tick()``, against temp roots, with no runtime sources."""

    def _tick_env(self, tmp: str) -> dict[str, str]:
        return {
            "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
            "XO_PROJECTS_ROOT": str(Path(tmp) / "projects"),
        }

    def _watcher_instance(self) -> watcher.Watcher:
        # No adapter source: the heartbeat is written by the tick itself,
        # not by anything a source contributes.
        with patch.object(watcher, "try_load_capability", return_value=None):
            return watcher.Watcher()

    def test_one_tick_writes_all_three_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, self._tick_env(tmp), clear=False):
                instance = self._watcher_instance()
                instance.tick()
                path = state.watcher_heartbeat_path()
                self.assertTrue(path.is_file())
                beat = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(
                sorted(beat), ["duration_ms", "last_tick_at", "tick_count"]
            )
            self.assertEqual(beat["tick_count"], 1)
            self.assertIsInstance(beat["duration_ms"], int)
            self.assertGreaterEqual(beat["duration_ms"], 0)
            # Same stamp format the sinks write, and parseable as UTC.
            self.assertRegex(
                beat["last_tick_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
            )
            self.assertIsNotNone(quirq_catalog._parse_iso(beat["last_tick_at"]))

    def test_tick_count_increments_across_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, self._tick_env(tmp), clear=False):
                instance = self._watcher_instance()
                instance.tick()
                first = json.loads(
                    state.watcher_heartbeat_path().read_text(encoding="utf-8")
                )
                instance.tick()
                second = json.loads(
                    state.watcher_heartbeat_path().read_text(encoding="utf-8")
                )
        self.assertEqual(first["tick_count"], 1)
        self.assertEqual(second["tick_count"], 2)

    def test_a_failing_heartbeat_write_never_kills_the_tick(self) -> None:
        """The heartbeat observes the tick; it must never be able to break
        it. A raising writer is logged and swallowed."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, self._tick_env(tmp), clear=False):
                instance = self._watcher_instance()
                with patch.object(
                    watcher,
                    "write_json_atomic",
                    side_effect=OSError("read-only filesystem"),
                ):
                    # assertLogs also keeps the expected traceback out of
                    # the suite's output.
                    with self.assertLogs(watcher.logger, level="ERROR") as logged:
                        instance.tick()  # must not propagate
                self.assertIn("heartbeat write failed", "\n".join(logged.output))
                self.assertFalse(state.watcher_heartbeat_path().exists())

                # And the very next tick recovers.
                instance.tick()
                beat = json.loads(
                    state.watcher_heartbeat_path().read_text(encoding="utf-8")
                )
        # Both ticks ran, so the counter reflects two executed ticks.
        self.assertEqual(beat["tick_count"], 2)


class CatalogLivenessTests(unittest.TestCase):
    """``_watcher(root)`` takes the state root as a parameter, so these
    drive it with a temp root directly rather than through the env."""

    def _report(self, root: Path) -> dict:
        with (
            patch.object(
                quirq_catalog, "configured_settings", return_value=_settings()
            ),
            patch.object(
                quirq_catalog, "effective_settings", return_value=_settings()
            ),
        ):
            return quirq_catalog._watcher(root)

    def _write_beat(self, root: Path, *, age_seconds: float) -> None:
        path = root / "watcher" / "heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        path.write_text(
            json.dumps(
                {
                    "last_tick_at": _iso(stamp),
                    "tick_count": 42,
                    "duration_ms": 17,
                }
            ),
            encoding="utf-8",
        )

    def test_missing_heartbeat_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._report(Path(tmp))
        self.assertFalse(report["alive"])
        self.assertFalse(report["heartbeat_present"])
        self.assertIsNone(report["last_tick_at"])
        self.assertIsNone(report["heartbeat_age_seconds"])

    def test_unreadable_heartbeat_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watcher" / "heartbeat.json"
            path.parent.mkdir(parents=True)
            path.write_text("{ not json", encoding="utf-8")
            report = self._report(root)
        self.assertFalse(report["alive"])
        self.assertIsNone(report["last_tick_at"])

    def test_stale_heartbeat_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_beat(root, age_seconds=600)
            report = self._report(root)
        self.assertFalse(report["alive"])
        self.assertGreater(
            report["heartbeat_age_seconds"], report["heartbeat_stale_after_seconds"]
        )
        # Enabled configuration is unchanged — that is the whole point:
        # config says "on", observation says "not ticking".
        self.assertTrue(report["enabled"])

    def test_fresh_heartbeat_is_alive_and_carries_the_observed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_beat(root, age_seconds=0)
            report = self._report(root)
        self.assertTrue(report["alive"])
        self.assertTrue(report["heartbeat_present"])
        self.assertEqual(report["tick_count"], 42)
        self.assertEqual(report["last_tick_duration_ms"], 17)
        self.assertGreaterEqual(report["heartbeat_age_seconds"], 0.0)

    def test_existing_config_keys_are_preserved(self) -> None:
        """Other consumers (and tests/test_quirq_catalog.py) read these."""
        with tempfile.TemporaryDirectory() as tmp:
            report = self._report(Path(tmp))
        for key in (
            "enabled",
            "interval_seconds",
            "source_mode",
            "configured_enabled",
            "tracked_files",
            "offsets_present",
        ):
            self.assertIn(key, report)

    def test_staleness_threshold_scales_with_the_configured_interval(self) -> None:
        # Floor applies at fast intervals; multiple missed ticks at slow ones.
        self.assertEqual(quirq_catalog._stale_after_seconds(0.25), 5.0)
        self.assertEqual(quirq_catalog._stale_after_seconds(1), 5.0)
        self.assertEqual(quirq_catalog._stale_after_seconds(60), 300.0)
        # Nonsense configuration must not make everything look alive.
        self.assertEqual(quirq_catalog._stale_after_seconds(None), 5.0)
        self.assertEqual(quirq_catalog._stale_after_seconds(0), 5.0)


class BadgeSourceTests(unittest.TestCase):
    """The frontend regression this task exists to close."""

    def test_quirq_badge_is_driven_by_alive_not_only_by_enabled(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "space_ui"
            / "js"
            / "views"
            / "quirq.js"
        ).read_text(encoding="utf-8")
        self.assertIn("watcher.alive", source)
        self.assertNotIn("badge.textContent=watcher.enabled?'Live':'Paused'", source)
        css = (
            Path(__file__).resolve().parents[1] / "space_ui" / "css" / "quirq.css"
        ).read_text(encoding="utf-8")
        # Third state must be visually distinct, not a silent no-op class.
        self.assertIn("b.is-stalled{", css)


if __name__ == "__main__":
    unittest.main()
