"""Claude Code's encoding of a working directory into its transcript folder name.

Claude Code writes each session log to
``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` where
``<encoded-cwd>`` is the absolute working directory with every character
outside ``[A-Za-z0-9]`` replaced by ``-``: ``/`` and also ``.`` and ``_``
(the leading slash makes the result start with ``-``). A thread worktree at
``<project>/.xo/worktrees/thread-x`` therefore encodes its ``/.`` as ``--``.

The encoding is **lossy** when a directory name contains a hyphen —
``xo-projects/blackhole`` encodes to ``-home-…-xo-projects-blackhole``
which a naive decode would split as ``xo/projects/blackhole`` — so callers
only ever compare in the forward direction. It is claude_code-specific and
lives under the adapter for that reason.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def encode_path(abs_path: str) -> str:
    """Forward direction, Claude Code's rule: every non-alphanumeric
    character becomes ``-`` (``/a/.b_c`` → ``-a--b-c``).

    Used for forward comparisons; we never decode an encoded string
    blindly because the reverse is lossy (see module docstring). Claude
    shortens folder names over 200 characters with a hash suffix, and
    replaces per UTF-16 unit, so a character outside the Basic Multilingual
    Plane (an emoji) is two dashes there and one here; such paths are not
    matched here.
    """
    return _NON_ALNUM.sub("-", abs_path)
