"""
Claude Code sessions capability.

Message reading and session-directory updates for claude_code sessions.
Ownership is determined by the project ``sessionslist.json`` backend tag (read
generically in ``find_session_backend``), so ``owns_session`` returns False
here — there is no separate native-scan fallback for claude_code.

The listing-side hooks (``enrich_project_session`` / ``resolve_native_file`` /
``list_native_sessions`` / ``USES_PROJECT_SESSIONS``) are what the generic
``sessions_io.load_all_sessions`` / ``find_session_file`` call instead of
branching on ``backend == "claude_code"``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.helpers import derive_title_native_claude, parse_jsonl
from services.cowork_agent.engine.messages import convert_native_claude_messages
from services.cowork_agent.engine.sessions_io import find_session_file
from services.cowork_agent.engine import sessions_io as _session_index

# claude_code publishes its session metadata into the per-project session
# index, so the generic project-tied scan applies to it.
USES_PROJECT_SESSIONS = True

# The ``backend`` tag claude_code writes on every sessionslist row it publishes;
# used to tell our rows apart from the other project-tied backends' in a shared
# index.
_BACKEND = "claude_code"


def _native_file(native_session_id: str, directory: str) -> Path | None:
    """Path to a Claude Code native JSONL, or None if absent.

    Claude Code names its project folders by replacing every ``/`` with ``-``
    (``/home/coder/xo-projects/blackhole`` →
    ``-home-coder-xo-projects-blackhole``); the session log lives at
    ``~/.claude/projects/<encoded-dir>/<nativeSessionId>.jsonl``. This is the
    lossless forward encoding (the lossy reverse lives in ``_project_encoding``).
    """
    if not native_session_id or not directory:
        return None
    encoded = directory.replace("/", "-")
    path = Path.home() / ".claude" / "projects" / encoded / f"{native_session_id}.jsonl"
    return path if path.exists() else None


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Return ``(time_created, title, effective_agent)`` for a project-tied
    claude_code session by reading its native JSONL. Either override may be
    None (caller keeps its defaults)."""
    time_created = None
    title = None
    native_path = _native_file(meta.get("nativeSessionId", ""), meta.get("directory", ""))
    if native_path:
        try:
            records = parse_jsonl(native_path)
            if records:
                ts = records[0].get("timestamp")
                if ts:
                    time_created = ts
            title = derive_title_native_claude(records)
        except Exception:
            pass
    return time_created, title, default_agent


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Locate the native message file for a project-tied claude_code session."""
    return _native_file(meta.get("nativeSessionId", ""), meta.get("directory", ""))


def list_native_sessions() -> list[dict]:
    """claude_code has no non-project native session store."""
    return []


def owns_session(session_id: str) -> bool:
    """claude_code sessions are project-tee'd and detected via the sessionslist tag."""
    return False


def get_messages(session_id: str) -> list:
    """Return converted messages for a claude_code session (empty if no file)."""
    path = find_session_file(session_id)
    if not path:
        return []
    return convert_native_claude_messages(session_id, parse_jsonl(path))


def _persist_session_directory(session_id: str, directory: str) -> bool:
    """Update the workspace directory for a claude_code session (project rows only)."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for project_id, _project_dir, index in _session_index.iter_project_session_indexes():
        for key, meta in index.items():
            if meta.get("sessionId") != session_id:
                continue
            # Same-index rows from the other project-tied backends are not ours:
            # the PATCH route loops adapters and takes the first non-None, so
            # without this we would service (and rewrite) another backend's row.
            # Untagged legacy rows stay claimable — a missing/empty ``backend``
            # predates the tag, so first-adapter-wins still applies there.
            backend = meta.get("backend")
            if isinstance(backend, str) and backend and backend != _BACKEND:
                continue
            # One row, one shard file, one atomic replace. The whole-document
            # rewrite this used to do is what made two concurrent writers lose
            # a row (syncplan T19, inherited from T4).
            row = dict(meta)
            history = list(row.get("directoryHistory") or [])
            history.append({"directory": directory, "selectedAt": now_ms})
            row["directoryHistory"] = history[-200:]
            row["directory"] = directory
            row["updatedAt"] = now_ms
            if _session_index.write_session_row(project_id, key, row):
                return True
    return False


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Set the workspace directory for a claude_code session; None if not ours."""
    if _persist_session_directory(session_id, directory):
        return {"ok": True, "session_id": session_id, "directory": directory}
    return None
