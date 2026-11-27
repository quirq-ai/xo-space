"""How a search hit is shaped, and the child process that runs a person's regex (stdlib only: run with ``python -I``)."""

# Python's re can backtrack for minutes and holds the GIL meanwhile, so a
# user's regex never runs in Space's process: the finder starts this file as
# a child, reads what it prints until its deadline, then kills it.

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
from typing import Any, Iterator

TEXT_MAX = 400          # how much of a matching line is sent, around its first match
TEXT_LEAD = 120         # how much of the line before the first match a cut line keeps


def shape(raw: bytes, byte_col: int | None, pattern: re.Pattern[str] | None) -> dict[str, Any]:
    """``{text, col, ranges}`` for one matching line: cut around its first match, ranges inside the cut."""
    raw = raw[:-1] if raw.endswith(b"\r") else raw
    text = raw.decode("utf-8", errors="replace")
    ranges = [] if pattern is None else [[m.start(), m.end()] for m in pattern.finditer(text) if m.end() > m.start()]
    if byte_col is not None and byte_col >= 1:
        first = len(raw[: byte_col - 1].decode("utf-8", errors="replace"))
    else:
        first = ranges[0][0] if ranges else 0
    if len(text) > TEXT_MAX:
        start = max(0, min(first - TEXT_LEAD, len(text) - TEXT_MAX))
        end = start + TEXT_MAX
        ranges = [[max(a, start) - start, min(b, end) - start] for a, b in ranges if b > start and a < end]
        text = text[start:end]
    return {"col": first + 1, "text": text, "ranges": ranges}


def read_text_file(path: str, cap: int) -> bytes | None:
    """A regular, non-binary file's bytes (never through a link at its name); None for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            return None
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if b"\x00" in data[:8192] else data


def _hits(job: dict[str, Any], pattern: re.Pattern[str]) -> Iterator[dict[str, Any]]:
    if job["mode"] == "lines":                         # git found the lines: shape each, in order
        for raw, col in job["lines"]:
            yield shape(base64.b64decode(raw), col, pattern)
        return
    for inner in job["files"]:                         # outside git: find the lines too
        data = read_text_file(os.path.join(job["root"], inner), job["file_max"])
        for number, raw in enumerate(data.split(b"\n") if data is not None else (), start=1):
            if pattern.search(raw.decode("utf-8", errors="replace")) is not None:
                yield {"path": inner, "line": number, **shape(raw, None, pattern)}


def main() -> int:
    job = json.loads(sys.stdin.buffer.read())
    pattern = re.compile(job["pattern"], job["flags"])
    for hit in _hits(job, pattern):
        sys.stdout.write(json.dumps(hit) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
