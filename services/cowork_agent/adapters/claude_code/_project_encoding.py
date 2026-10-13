"""Claude Code's encoded-cwd jsonl directory ↔ xo-project id helpers.

Claude Code writes each session log to
``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` where
``<encoded-cwd>`` is the absolute working directory with every character
outside ``[A-Za-z0-9]`` replaced by ``-`` (so ``/`` and also ``.``, ``_``
become ``-``; the leading slash makes the result start with ``-``). A
worktree at ``<project>/.claude/worktrees/x`` therefore encodes its
``/.`` as ``--``.

The encoding is **lossy** when a directory name contains a hyphen —
``xo-projects/blackhole`` encodes to ``-home-…-xo-projects-blackhole``
which a naive decode would split as ``xo/projects/blackhole``.

These helpers avoid the lossy reverse by matching the encoded prefix
against actual project directories under ``xo_projects_root()`` (the
authoritative source). They are claude_code-specific — other backends
have their own per-message project routing — and live under the
adapter for that reason. The backend-neutral
``services.cowork_agent.visualizer.project_index.project_id_for_cwd``
is the only piece of the original ``project_index`` module that's
still used by more than one source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import xo_projects_root


_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def encode_path(abs_path: str) -> str:
    """Forward direction, Claude Code's rule: every non-alphanumeric
    character becomes ``-`` (``/a/.b_c`` → ``-a--b-c``).

    Used for forward comparisons; we never decode an encoded string
    blindly because the reverse is lossy (see module docstring). Claude
    shortens names over 200 characters with a hash suffix; such paths are
    not matched here.
    """
    return _NON_ALNUM.sub("-", abs_path)


_encode_path = encode_path


def project_id_for_encoded_cwd(encoded: str) -> Optional[str]:
    """Best-effort: encoded dir name → project id, by **longest match
    against existing project directories**.

    Strategy:

    1. Verify the encoding starts with the encoded form of
       ``xo_projects_root()`` followed by ``-`` (otherwise the session
       isn't inside the workspace).
    2. Strip that prefix; the remainder is ``<project_id>`` optionally
       followed by ``-<subdir-encoded>``.
    3. Match the remainder against each existing top-level project
       directory, preferring the **longest** match. Longest-match
       handles the case where projects ``foo`` and ``foo-bar`` both
       exist and the session is in ``foo-bar``.

    Returns ``None`` if no project matches (unknown session, or
    project deleted between encoding and lookup).
    """
    if not encoded or not encoded.startswith("-"):
        return None
    root = xo_projects_root()
    root_encoded = _encode_path(str(root))  # e.g. ``-home-coder-xo-projects``
    prefix = root_encoded + "-"
    if not encoded.startswith(prefix):
        return None
    remainder = encoded[len(prefix):]
    if not remainder:
        return None

    # Sort actual children by name length, longest first, to handle
    # the ``foo`` vs ``foo-bar`` ambiguity correctly.
    try:
        candidates = sorted(
            (c.name for c in root.iterdir()
             if c.is_dir() and not c.name.startswith(".")),
            key=len,
            reverse=True,
        )
    except OSError:
        return None

    for name in candidates:
        token = encode_path(name)
        if remainder == token or remainder.startswith(token + "-"):
            return name
    return None
