"""Capability ``raw_log``: split codex's cumulative-diff lines into per-file blocks so the raw log stores each once."""

from __future__ import annotations

import re

#: The only codex line worth splitting: its turn's cumulative diff.
_DIFF_LINE = b'{"method":"turn/diff/updated"'
#: The diff's JSON string: ``"diff":"`` then escapes or other bytes, to the closing quote.
_DIFF_STRING = re.compile(rb'"diff":"((?:[^"\\]++|\\.)*+)"', re.S)
#: A block starts at each ``diff --git `` that follows an (escaped) newline.
_BLOCK_START = re.compile(rb'(?<=\\n)diff --git ')


def split(line: bytes) -> tuple[bytes, list[bytes], bytes] | None:
    """``(pre, blocks, post)`` for a cumulative-diff line, None otherwise; lossless: pre + blocks + post is the line."""
    if not line.startswith(_DIFF_LINE):
        return None
    found = _DIFF_STRING.search(line)
    if found is None:
        return None
    diff = found.group(1)
    cuts = [0, *(m.start() for m in _BLOCK_START.finditer(diff)), len(diff)]
    blocks = [diff[start:end] for start, end in zip(cuts, cuts[1:]) if end > start]
    return line[:found.start(1)], blocks, line[found.end(1):]
