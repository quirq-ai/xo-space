"""An over-long stdout line must not end the turn.

Reproduces the failure seen live: a tool result carried on one line exceeded
asyncio's 64 KiB StreamReader default, ``readline()`` raised ValueError, and
that propagated out of the adapter's generator — ending the SSE response
mid-turn with ``{"error_message": "Separator is found, but chunk is longer
than limit"}`` while the agent kept running and finished on disk.
"""
from __future__ import annotations

import asyncio
import unittest

from services.cowork_agent.engine.stream_io import LINE_LIMIT, iter_lines


def reader_with(payload: bytes, limit: int) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


async def collect(payload: bytes, limit: int) -> list[bytes]:
    return [line async for line in iter_lines(reader_with(payload, limit))]


class IterLinesTests(unittest.TestCase):
    def test_normal_lines_pass_through_unchanged(self) -> None:
        got = asyncio.run(collect(b'{"a":1}\n{"b":2}\n', 64))
        self.assertEqual(got, [b'{"a":1}\n', b'{"b":2}\n'])

    def test_an_over_long_line_does_not_end_the_stream(self) -> None:
        """The whole point: later events still arrive after a huge one."""
        payload = b'{"first":1}\n' + b"x" * 500 + b"\n" + b'{"last":1}\n'
        got = asyncio.run(collect(payload, 64))
        self.assertEqual(got[0], b'{"first":1}\n')
        self.assertEqual(got[-1], b'{"last":1}\n')

    def test_only_the_over_long_line_is_lost(self) -> None:
        payload = b'{"first":1}\n' + b"x" * 500 + b"\n" + b'{"last":1}\n'
        got = asyncio.run(collect(payload, 64))
        self.assertEqual(len(got), 2)
        self.assertFalse(any(b"xxx" in line for line in got))

    def test_several_over_long_lines_in_a_row_are_survived(self) -> None:
        big = b"x" * 500 + b"\n"
        got = asyncio.run(collect(big + big + b'{"last":1}\n', 64))
        self.assertEqual(got, [b'{"last":1}\n'])

    def test_a_trailing_line_without_a_newline_is_still_yielded(self) -> None:
        got = asyncio.run(collect(b'{"a":1}\n{"b":2}', 64))
        self.assertEqual(got[-1], b'{"b":2}')

    def test_empty_output_terminates(self) -> None:
        self.assertEqual(asyncio.run(collect(b"", 64)), [])

    def test_the_configured_limit_clears_a_realistic_tool_result(self) -> None:
        """A megabyte of command output is ordinary and must survive intact."""
        line = b'{"aggregated_output":"' + b"y" * (1024 * 1024) + b'"}\n'
        got = asyncio.run(collect(line, LINE_LIMIT))
        self.assertEqual(got, [line])

    def test_the_default_asyncio_limit_would_have_dropped_it(self) -> None:
        """Pins why the limit is raised, not just why iter_lines exists."""
        line = b'{"aggregated_output":"' + b"y" * (1024 * 1024) + b'"}\n'
        self.assertEqual(asyncio.run(collect(line, 64 * 1024)), [])


if __name__ == "__main__":
    unittest.main()
