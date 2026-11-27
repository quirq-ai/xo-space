"""Each line the server writes to its log starts with the UTC time it was
written, whoever wrote it: ``print``, logging, uvicorn, a traceback.

The log is the server's stdout and stderr (``/tmp/xo-space.log``, or the
installer's), so the stamp goes on there, once, at start. Bytes written to
fd 1 or 2 without Python (C code, a child process that inherits them) are
not stamped.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from typing import TextIO


class StampedLines:
    """``stream``, with each line begun by ``2026-11-27T14:08:15.123Z ``.
    Everything else (``fileno``, ``isatty``, ``flush``, ``encoding``) is the
    stream's own."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._at_line_start = True
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z "
            ends = text.endswith("\n")
            body = text[:-1] if ends else text
            self._stream.write((stamp if self._at_line_start else "") + body.replace("\n", "\n" + stamp)
                               + ("\n" if ends else ""))
            self._at_line_start = ends
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def stamp_lines() -> None:
    """Stamp every line of ``sys.stdout`` and ``sys.stderr`` from now on."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, StampedLines):
            setattr(sys, name, StampedLines(stream))
