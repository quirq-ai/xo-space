"""
Antigravity (agy) on-disk path constants.

All agy state lives under ``~/.gemini/antigravity-cli/`` (the state ROOT — inside
``~/.gemini/`` but agy-specific; it only shares the *directory* with the
unrelated Gemini CLI). Resolved from the active manifest's ``home_dir`` so a
future relocation is a one-line manifest change, with a hardcoded fallback for
importers that run before the registry is warm.

Layout (verified against agy v1.1.2):

    ROOT/
    ├── antigravity-oauth-token                     consumer OAuth creds (mode 600)
    ├── settings.json                               {trustedWorkspaces, …}
    ├── brain/<conversation-uuid>/                  per-conversation engine state
    │   └── .system_generated/logs/
    │       ├── transcript_full.jsonl               step stream (native JSON args) ← PARSE THIS
    │       └── transcript.jsonl                     step stream (args re-stringified)
    ├── conversations/<uuid>.db                     SQLite trajectory store — TOKENS live here (WAL)
    └── cache/last_conversations.json               {"<abs-launch-cwd>": "<conversation-uuid>"}
"""
from __future__ import annotations

import os
from pathlib import Path


def _agy_home() -> Path:
    """The agy state ROOT, from the active manifest's ``home_dir`` when available."""
    try:
        from services.cowork_agent.registry.agent_registry import get_agent

        return Path(get_agent("antigravity").home_dir)
    except Exception:
        return Path(os.path.expanduser("~/.gemini/antigravity-cli"))


AGY_HOME: Path = _agy_home()
SETTINGS_PATH: Path = AGY_HOME / "settings.json"
BRAIN_DIR: Path = AGY_HOME / "brain"
CONVERSATIONS_DIR: Path = AGY_HOME / "conversations"
CACHE_DIR: Path = AGY_HOME / "cache"
LAST_CONVERSATIONS: Path = CACHE_DIR / "last_conversations.json"


def transcript_path(conversation_id: str) -> Path:
    """``brain/<cid>/.system_generated/logs/transcript_full.jsonl`` for a conversation."""
    return BRAIN_DIR / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl"


def conversation_db(conversation_id: str) -> Path:
    """``conversations/<cid>.db`` — the SQLite trajectory store (tokens)."""
    return CONVERSATIONS_DIR / f"{conversation_id}.db"


__all__ = [
    "AGY_HOME", "SETTINGS_PATH", "BRAIN_DIR",
    "CONVERSATIONS_DIR", "CACHE_DIR", "LAST_CONVERSATIONS",
    "transcript_path", "conversation_db",
]
