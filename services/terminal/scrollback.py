"""A terminal's recent output, which every viewer reads at its own pace.

One reader drains the terminal into this ring whether or not anyone is
watching, so a slow or paused browser tab never blocks the program. Each
viewer holds a byte offset (a cursor) into the stream. A viewer whose cursor
has fallen off the start of the ring, or a new viewer once the ring has
wrapped, gets a *resync*: a terminal reset, the modes the program has
switched on (alternate screen, mouse reporting, bracketed paste…) and the
ring from its first whole line. A full-screen program then repaints itself
when the viewer's size arrives (``PtyProcess.redraw``).
"""

from __future__ import annotations

import re

RESET = b"\x1bc"
# DEC private modes worth restoring on a resync, in the order they are re-sent:
# cursor keys (1), alternate screen (47/1047/1049), mouse (1000/1002/1003 and
# the 1006/1015 encodings), bracketed paste (2004), cursor visibility (25).
_TRACKED = (1, 47, 1047, 1049, 1000, 1002, 1003, 1006, 1015, 2004, 25)
_DEFAULT_ON = frozenset({25})
_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]{1,32})([hl])|\x1bc")
_TAIL = 40  # longest sequence above, so one split across two reads is still seen
_ALIGN_WITHIN = 4096  # look this far for a line start to resume from


class _Modes:
    """The DEC private modes a byte stream has switched on, fed in order."""

    def __init__(self) -> None:
        self._on: dict[int, bool] = {}
        self._tail = b""

    def copy(self) -> "_Modes":
        other = _Modes()
        other._on, other._tail = dict(self._on), self._tail
        return other

    def feed(self, data: bytes) -> None:
        seen = self._tail + data
        for match in _MODE_RE.finditer(seen):
            if match.end() <= len(self._tail):
                continue  # counted with the previous bytes
            if match.group(0) == RESET:
                self._on.clear()
                continue
            on = match.group(2) == b"h"
            for part in match.group(1).split(b";"):
                if part.isdigit() and int(part) in _TRACKED:
                    self._on[int(part)] = on
        self._tail = seen[-_TAIL:]

    def prefix(self) -> bytes:
        """Escape sequences that put a freshly reset terminal into these modes."""
        out = bytearray()
        for mode in _TRACKED:
            on = self._on.get(mode, mode in _DEFAULT_ON)
            if on != (mode in _DEFAULT_ON):
                out += b"\x1b[?%d%s" % (mode, b"h" if on else b"l")
        return bytes(out)


class Scrollback:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._buf = bytearray()
        self.start = 0  # stream offset of _buf[0]
        # The modes in force at the start of the ring: what a replay from
        # there must begin with. Fed with the bytes as they fall off the ring.
        self._start_modes = _Modes()

    @property
    def end(self) -> int:
        """Stream offset just past the newest byte."""
        return self.start + len(self._buf)

    def append(self, data: bytes) -> None:
        self._buf += data
        overflow = len(self._buf) - self.capacity
        if overflow > 0:
            self._start_modes.feed(bytes(self._buf[:overflow]))
            del self._buf[:overflow]
            self.start += overflow

    def read(self, cursor: int, limit: int) -> tuple[bytes, int, bool]:
        """Up to ``limit`` bytes from ``cursor``: ``(data, next_cursor, resynced)``.

        A cursor still inside the ring reads exactly what followed it. A cursor
        before the ring (behind, or 0 for a new viewer after a wrap) gets a
        resync, as described in the module docstring: the replay begins in the
        modes that were in force where it begins.
        """
        if cursor >= self.start:
            offset = min(cursor, self.end) - self.start
            data = bytes(self._buf[offset:offset + limit])
            return data, self.start + offset + len(data), False
        newline = self._buf.find(b"\n", 0, _ALIGN_WITHIN)
        offset = newline + 1 if newline >= 0 else 0
        modes = self._start_modes.copy()
        modes.feed(bytes(self._buf[:offset]))
        data = bytes(self._buf[offset:offset + limit])
        return RESET + modes.prefix() + data, self.start + offset + len(data), True
