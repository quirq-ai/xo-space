"""SSE keepalive frames must survive a buffering proxy.

Measured against the deployed workspace proxy: a turn whose tool ran 72s
survived 77s with three heartbeats when curled directly at the API, and died at
~30.3s through the proxy — four times, across two backends. Real output always
got through; only the ~30-byte heartbeats did not. The frame is padded so a
buffer that would sit on 30 bytes is pushed past its flush threshold, and the
interval is kept well under the observed 30s cut.
"""
from __future__ import annotations

import unittest

from routers.cowork_agent import chat


class HeartbeatFrameTests(unittest.TestCase):
    def test_the_frame_is_large_enough_to_flush_a_buffer(self) -> None:
        self.assertGreater(len(chat._HEARTBEAT), 2000)

    def test_two_keepalives_fall_inside_the_observed_cut(self) -> None:
        """One per window leaves no margin if a single frame is delayed."""
        self.assertLessEqual(chat._KEEPALIVE_INTERVAL * 2, 30)

    def test_the_padding_is_an_sse_comment_so_clients_ignore_it(self) -> None:
        first = chat._HEARTBEAT.split("\n", 1)[0]
        self.assertTrue(first.startswith(":"))
        self.assertEqual(first.strip(), ":")

    def test_it_still_parses_as_a_heartbeat_event(self) -> None:
        """Padding must not disturb the event a client actually reads."""
        lines = [ln for ln in chat._HEARTBEAT.split("\n") if ln and not ln.startswith(":")]
        self.assertEqual(lines, ["event: heartbeat", "data: {}"])

    def test_the_frame_ends_with_a_blank_line(self) -> None:
        """SSE dispatches an event on the blank line; without it nothing fires."""
        self.assertTrue(chat._HEARTBEAT.endswith("\n\n"))

    def test_the_comment_is_terminated_before_the_event_begins(self) -> None:
        """A comment not closed by a blank line would swallow the event."""
        self.assertIn("\n\nevent: heartbeat", chat._HEARTBEAT)


if __name__ == "__main__":
    unittest.main()
