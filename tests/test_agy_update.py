"""Keeping agy current: the version floor (an older agy still runs, on print
mode, and is said to need an update) and ``agy update`` run on request,
against a fake agy.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.antigravity import update as agy_update
from services.cowork_agent.adapters.antigravity.runtime import core

SCRIPT = """#!/bin/sh
state="$(dirname "$0")/.version"
case "$1" in
  --version) cat "$state" ;;
  update)
    echo "Checking for updates..."
    sleep {pause}
    echo "progress" >&2
    if [ "{fail}" = "1" ]; then echo "network unreachable" >&2; exit 3; fi
    echo "Updated to 1.5.20"
    printf '1.5.20\\n' > "$state" ;;
esac
"""


class FloorTests(unittest.TestCase):
    def test_an_agy_older_than_the_floor_is_said_to_need_an_update(self) -> None:
        self.assertIn("older than 1.5.15", core.update_note("1.4.10"))
        self.assertIsNone(core.update_note("1.5.15"))
        self.assertIsNone(core.update_note("1.6.0"))
        self.assertIsNone(core.update_note(None))         # unknown: nothing to say


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def fake(self, *, fail: bool = False, pause: float = 0) -> dict[str, str]:
        exe = self.bin / "agy"
        exe.write_text(SCRIPT.replace("{fail}", "1" if fail else "0").replace("{pause}", str(pause)))
        exe.chmod(0o755)
        (self.bin / ".version").write_text("1.4.10\n")
        return {"AGY_CLI_PATH": str(exe), "HOME": self._tmp.name}

    async def collect(self, **kw) -> list[dict]:
        return [event async for event in agy_update.update(**kw)]

    async def test_an_update_streams_its_lines_and_ends_with_the_new_version(self) -> None:
        with mock.patch.dict(os.environ, self.fake()):
            events = await self.collect()
        self.assertEqual(events[0], {"type": "installing", "package": "agy", "version": "1.4.10"})
        self.assertEqual(sorted(e["line"] for e in events if e["type"] == "install_log"),
                         ["Checking for updates...", "Updated to 1.5.20", "progress"])
        self.assertEqual(events[-1], {"type": "done", "returncode": 0, "version_before": "1.4.10",
                                      "version": "1.5.20", "update": None})

    async def test_a_failed_update_says_so(self) -> None:
        with mock.patch.dict(os.environ, self.fake(fail=True)):
            with self.assertRaises(agy_update.UpdateFailed) as caught:
                await self.collect()
        self.assertEqual(caught.exception.code, "update_failed")

    async def test_a_quiet_update_sends_heartbeats(self) -> None:
        with mock.patch.dict(os.environ, self.fake(pause=0.6)):
            events = await self.collect(heartbeat_s=0.2)
        self.assertIn({"type": "heartbeat"}, events)
        self.assertEqual(events[-1]["type"], "done")

    async def test_one_update_at_a_time(self) -> None:
        with mock.patch.dict(os.environ, self.fake(pause=0.5)):
            first = asyncio.create_task(self.collect())
            await asyncio.sleep(0.2)
            with self.assertRaises(agy_update.UpdateFailed) as caught:
                await self.collect()
            await first
        self.assertEqual((caught.exception.code, caught.exception.status), ("update_running", 409))

    async def test_no_agy_to_update(self) -> None:
        with mock.patch.dict(os.environ, {"AGY_CLI_PATH": str(self.bin / "missing"), "HOME": self._tmp.name}):
            with self.assertRaises(agy_update.UpdateFailed) as caught:
                await self.collect()
        self.assertEqual(caught.exception.code, "cli_missing")


if __name__ == "__main__":
    unittest.main()
