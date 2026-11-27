"""
Codex CLI on-disk rollout discovery.

Codex persists every session as a rollout JSONL under a 3-level date tree
(NOT a per-cwd encoded dir like claude ``~/.claude/projects/<enc>/``). The native
session id (UUIDv7) is embedded in the filename and equals
``session_meta.session_id`` inside the file and ``thread.started.thread_id`` on
the wire (blueprint §0). This module only *locates* rollouts; parsing lives in the
consumers (``codex/sessions.py``, ``codex/usage.py``, ``codex/visualizer_source.py``).

Layout (verified from 7 real rollouts, 2026-07-24):

    ROOT/                                      $CODEX_HOME or ~/.codex
    ├── sessions/YYYY/MM/DD/
    │   └── rollout-<ISO8601-with-dashes>-<uuid>.jsonl   ← the transcript
    ├── history.jsonl                          {session_id, ts, text} per prompt
    ├── config.toml                            per-project trust_level registry
    └── auth.json                              OAuth / API-key creds (may be absent)

Mirrors the ``paths.py`` pattern of ``adapters/antigravity/paths.py:1-62``
(manifest-with-fallback root, path helpers, ``__all__``).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional


# UUID (incl. UUIDv7) canonical hex layout 8-4-4-4-12. Guards ``find_rollout``
# against glob-metacharacter injection from an untrusted native id.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def codex_home() -> Path:
    """
    The codex state ROOT. Precedence:
      1. ``$CODEX_HOME`` — the env var the codex binary itself honours when
         writing rollouts (so the reader must agree with the writer).
      2. the active manifest's ``home_dir`` (config/agents/codex/manifest.json).
      3. ``~/.codex`` — hardcoded fallback for importers before the registry warms.

    The env read is identical to
    ``providers_status_lib.codex_oauth_connected`` (providers_status_lib.py:105-108)
    so the auth.json probe and the rollout reader never disagree on the root.
    """
    env = (os.getenv("CODEX_HOME", "") or "").strip()
    if env:
        return Path(os.path.expanduser(env))
    try:
        from services.cowork_agent.registry.agent_registry import get_agent

        return Path(os.path.expanduser(get_agent("codex").home_dir))
    except Exception:
        return Path(os.path.expanduser("~/.codex"))


def rollout_root() -> Path:
    """The ``<CODEX_HOME>/sessions`` tree holding the date-nested rollout files."""
    return codex_home() / "sessions"


def find_rollout(uuid: str) -> Optional[Path]:
    """
    The rollout file for a native session ``uuid``, or ``None``.

    Globs ``rollout-*-<uuid>.jsonl`` recursively across the ``YYYY/MM/DD`` tree.
    NEWEST WINS (by mtime): resume may append a second dated rollout carrying the
    same id (UNVERIFIED, blueprint §12.5), so we always return the freshest.
    Returns ``None`` for a malformed/empty uuid (glob-injection guard) or when
    nothing matches. Consumed by ``sessions.resolve_native_file`` /
    ``sessions.enrich_project_session``.
    """
    if not uuid:
        return None
    uuid = uuid.strip()
    if not _UUID_RE.match(uuid):
        return None

    root = rollout_root()
    if not root.is_dir():
        return None

    matches = list(root.rglob(f"rollout-*-{uuid}.jsonl"))
    if not matches:
        return None
    try:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return matches[0]


def archived_root() -> Path:
    """``<CODEX_HOME>/archived_sessions``: codex moves an archived thread's
    rollout here (flat, same file name), out of the date tree."""
    return codex_home() / "archived_sessions"


def iter_rollouts(*, archived: bool = False) -> Iterator[Path]:
    """
    Every rollout JSONL under the date tree, newest first (by mtime); with
    ``archived``, the archived threads' rollouts too (their usage still
    happened, though they're no longer listed as sessions).

    Recursive glob — codex nests by ``YYYY/MM/DD`` (NOT the 1-level walk claude
    uses). Empty iterator when the tree is absent (never raises). Consumed by
    ``usage.get_session_files`` / ``sessions.list_native_sessions`` discovery.
    """
    roots = (rollout_root(), archived_root()) if archived else (rollout_root(),)
    files = [p for root in roots if root.is_dir() for p in root.rglob("rollout-*.jsonl")]
    if not files:
        return iter(())
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return iter(files)


def read_session_meta(path: Path) -> dict[str, Any]:
    """
    The ``session_meta`` payload (line 1 of a rollout), or ``{}``.

    Every rollout line is ``{"timestamp","type","payload"}``; ``session_meta`` is
    line 1 and carries ``session_id`` + ``cwd`` (project attribution) +
    ``model_provider`` + ``git`` (blueprint §1.3). Scans the first few lines
    defensively rather than trusting a fixed index. Never raises.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "session_meta":
                    payload = rec.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def read_session_cwd(path: Path) -> Optional[str]:
    """
    The launch ``cwd`` recorded in a rollout's ``session_meta`` — codex's
    project-attribution key (there is no encoded-cwd dir). Feed the result to the
    backend-neutral ``project_id_for_cwd``
    (``services/cowork_agent/visualizer/project_index.py``) to map a rollout to an
    xo-project. ``None`` when absent.
    """
    cwd = read_session_meta(path).get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


__all__ = [
    "codex_home",
    "rollout_root",
    "find_rollout",
    "iter_rollouts",
    "read_session_meta",
    "read_session_cwd",
]
