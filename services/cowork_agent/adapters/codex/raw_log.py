"""Capability ``raw_log``: how storm's raw turn log may store codex's lines
smaller without losing a byte (``storm/engine/raw_turns.py``).

Codex's app-server sends a turn's whole cumulative diff again after every
change (``turn/diff/updated``): one turn of 11-10 wrote 5,281 of them,
1,670 MB, each mostly the one before. ``split`` cuts such a line into the
bytes before the diff's JSON string, the string's per-file blocks (each
starts at a ``diff --git `` after an escaped newline) and the bytes after
it. The log writes each block once per file and names it by digest
elsewhere; ``pre + b"".join(blocks) + post`` is always the line itself.
"""

from __future__ import annotations

import re

#: The only codex line worth splitting: its turn's cumulative diff.
_DIFF_LINE = b'{"method":"turn/diff/updated"'
#: The diff's JSON string: ``"diff":"`` then escapes or other bytes, to the closing quote.
_DIFF_STRING = re.compile(rb'"diff":"((?:[^"\\]++|\\.)*+)"', re.S)
#: A block starts at each ``diff --git `` that follows an (escaped) newline.
_BLOCK_START = re.compile(rb'(?<=\\n)diff --git ')


def split(line: bytes) -> tuple[bytes, list[bytes], bytes] | None:
    """``(pre, blocks, post)`` for a cumulative-diff line; None for any other
    line, which the log writes as it is."""
    if not line.startswith(_DIFF_LINE):
        return None
    found = _DIFF_STRING.search(line)
    if found is None:
        return None
    diff = found.group(1)
    cuts = [0, *(m.start() for m in _BLOCK_START.finditer(diff)), len(diff)]
    blocks = [diff[start:end] for start, end in zip(cuts, cuts[1:]) if end > start]
    return line[:found.start(1)], blocks, line[found.end(1):]
