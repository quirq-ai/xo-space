"""
OpenClaw sessions capability.

Ownership detection, message reading, and session-directory updates for
openclaw-backed sessions. Resolved generically by the session routes via
``load_capability('sessions', agent=<backend>)`` so no core router names a
backend.

Implements the full sessions contract (see the other adapters' ``sessions``
modules): ``owns_session`` / ``get_messages`` / ``set_session_directory`` plus
the listing-side hooks ``USES_PROJECT_SESSIONS`` / ``enrich_project_session`` /
``resolve_native_file`` / ``list_native_sessions`` that ``sessions_io`` calls
instead of branching on ``backend == "openclaw"``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.helpers import derive_title, iso_now, ms_to_iso, parse_jsonl
from services.cowork_agent.engine.messages import convert_messages
from services.cowork_agent.engine.sessions_io import find_session_file
from services.cowork_agent.engine import sessions_io as _session_index
from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR

# openclaw tees project sessions into xo-projects AND keeps native sessions
# under ~/.openclaw/agents/<a>/sessions/, so both scans apply.
USES_PROJECT_SESSIONS = True


def _agent_from_key(key: str, default_agent: str) -> str:
    """OpenClaw session keys look like ``openclaw:<agent>:...`` — the agent id
    is the second segment when present, else the scanning dir's name."""
    parts = key.split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else default_agent


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Return ``(time_created, title, effective_agent)`` for a project-tied
    openclaw session. Messages live under ~/.openclaw/agents/<a>/sessions/;
    the effective agent comes from the session key."""
    oc_agent = _agent_from_key(key, default_agent)
    session_id = meta.get("sessionId", "")
    time_created = None
    title = None
    if session_id and AGENTS_DIR.exists():
        oc_file = AGENTS_DIR / oc_agent / "sessions" / f"{session_id}.jsonl"
        if oc_file.exists():
            try:
                records = parse_jsonl(oc_file)
                if records:
                    ts = records[0].get("timestamp")
                    if ts:
                        time_created = ts
                title = derive_title(records)
            except Exception:
                pass
    return time_created, title, oc_agent


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Locate the native message file for an openclaw session by scanning the
    agents dir (the project tee doesn't record which agent owns it)."""
    if AGENTS_DIR.exists():
        for oc_dir in AGENTS_DIR.iterdir():
            if not oc_dir.is_dir():
                continue
            p = oc_dir / "sessions" / f"{session_id}.jsonl"
            if p.exists():
                return p
    return None


def list_native_sessions() -> list[dict]:
    """Full session rows for openclaw native sessions (no project picked at
    chat time), read from each ~/.openclaw/agents/<a>/sessions/sessions.json.
    Caller de-duplicates by id."""
    rows: list[dict] = []
    if not AGENTS_DIR.exists():
        return rows
    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue
        sessions_dir = agent_dir / "sessions"
        sessions_index = sessions_dir / "sessions.json"
        if not sessions_index.exists():
            continue
        try:
            with open(sessions_index, encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            continue
        for key, meta in index_data.items():
            if not isinstance(meta, dict):
                continue
            session_id = meta.get("sessionId", "")
            if not session_id:
                continue

            session_file = sessions_dir / f"{session_id}.jsonl"
            updated_at = meta.get("updatedAt")
            time_updated = ms_to_iso(updated_at) if updated_at else iso_now()
            time_created = time_updated
            title = "Untitled Session"
            if session_file.exists():
                try:
                    records = parse_jsonl(session_file)
                    if records:
                        ts = records[0].get("timestamp")
                        if ts:
                            time_created = ts
                    title = derive_title(records)
                except Exception:
                    pass

            rows.append({
                "id": session_id,
                "project_id": None,
                "parent_id": None,
                "slug": None,
                "agent": _agent_from_key(key, agent_dir.name),
                "directory": meta.get("directory") or "",
                "title": title,
                "version": 1,
                "summary_additions": 0,
                "summary_deletions": 0,
                "summary_files": 0,
                "summary_diffs": [],
                "is_pinned": False,
                "permission": {},
                "time_created": time_created,
                "time_updated": time_updated,
                "time_compacting": None,
                "time_archived": None,
            })
    return rows


def owns_session(session_id: str) -> bool:
    """True if this session is an openclaw native session (~/.openclaw/agents/<a>/sessions/<id>.jsonl)."""
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if agent_dir.is_dir() and (agent_dir / "sessions" / f"{session_id}.jsonl").exists():
                return True
    return False


def get_messages(session_id: str) -> list:
    """Return converted messages for an openclaw session (empty if no file)."""
    path = find_session_file(session_id)
    if not path:
        return []
    return convert_messages(session_id, parse_jsonl(path))


def find_session_key(session_id: str) -> str | None:
    """Look up the openclaw session key for a given session ID.

    Checks the native store under ~/.openclaw/agents/<a>/sessions/ first, then
    the per-project session index for tee'd openclaw sessions.
    """
    # Native store
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            index_path = agent_dir / "sessions" / "sessions.json"
            if not index_path.exists():
                continue
            try:
                with open(index_path, encoding="utf-8") as f:
                    index_data = json.load(f)
            except Exception:
                continue
            for key, meta in index_data.items():
                if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                    return key

    # Project-tied (tee'd) sessions
    for _project_id, _project_dir, index in _session_index.iter_project_session_indexes():
        for key, meta in index.items():
            if meta.get("sessionId") == session_id:
                return key

    return None


def _persist_session_directory(session_id: str, directory: str) -> bool:
    """Persist the selected workspace directory onto the matching native
    sessions.json entry under ~/.openclaw/agents/<a>/sessions/."""
    if not AGENTS_DIR.exists():
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        index_path = agent_dir / "sessions" / "sessions.json"
        if not index_path.exists():
            continue
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            continue

        changed = False
        for meta in index_data.values():
            if not isinstance(meta, dict) or meta.get("sessionId") != session_id:
                continue
            history = meta.get("directoryHistory")
            if not isinstance(history, list):
                history = []
            history.append({"directory": directory, "selectedAt": now_ms})
            meta["directoryHistory"] = history[-200:]
            meta["directory"] = directory
            meta["updatedAt"] = now_ms
            changed = True
            break

        if changed:
            index_path.write_text(json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True

    return False


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Set the workspace directory for an openclaw session; None if not ours."""
    if _persist_session_directory(session_id, directory):
        return {"ok": True, "session_id": session_id, "directory": directory}
    return None
