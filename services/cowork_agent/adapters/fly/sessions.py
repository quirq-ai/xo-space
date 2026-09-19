"""Host-owned, revision-pinned sessions. No chat state is written to a project."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.adapters.fly import paths
from services.cowork_agent.engine import sessions_io
from services.cowork_agent import project_layout
from services.errors import ServiceError
from services.storage.atomic_write import write_json_atomic

USES_PROJECT_SESSIONS = False
_ID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TERMINAL = frozenset({"completed", "cancelled", "failed", "interrupted"})


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def checked_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ServiceError("invalid_session", "Expected a canonical session or run UUID.")
    return value


def workspace_for(project_id: str) -> Path:
    if not isinstance(project_id, str) or not _PROJECT.fullmatch(project_id):
        raise ServiceError("project_required", "Select an existing project using agent_id; a fly ID belongs in model.")
    root = project_layout.xo_projects_root().resolve()
    path = root / project_id
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
        raise ServiceError("project_unavailable", "The selected project must be an existing directory inside XO_PROJECTS_ROOT.", 404)
    return path.resolve()


def _directory(name: str) -> Path:
    root = paths.runtime_root()
    if root.is_symlink():
        raise ServiceError("unsafe_state", "The fly runtime state directory must not be a symlink.", 500)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root / name
    if directory.is_symlink():
        raise ServiceError("unsafe_state", "The fly state subdirectory must not be a symlink.", 500)
    directory.mkdir(exist_ok=True, mode=0o700)
    return directory


def _path(kind: str, record_id: str) -> Path:
    return _directory(kind) / f"{checked_id(record_id)}.json"


def _read(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ServiceError("unsafe_state", "Cannot safely open the fly state record.", 500) from exc
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32_000_000:
            raise ServiceError("invalid_state", "The fly state record exceeds supported limits.", 500)
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ServiceError("invalid_state", "The saved fly session has an incompatible schema.", 500)
    return value


def _write(path: Path, value: dict) -> None:
    if path.is_symlink() or path.with_suffix(".json.tmp").is_symlink():
        raise ServiceError("unsafe_state", "Refusing a symlink in the fly state store.", 500)
    write_json_atomic(path, value)
    path.chmod(0o600)


def load(session_id: str) -> dict | None:
    return _read(_path("sessions", session_id))


def save(session: dict) -> None:
    session["updated_at"] = now()
    _write(_path("sessions", session["id"]), session)
    sessions_io.write_session_row(session["project_id"], f"fly:{session['project_id']}:{session['id']}", {
        "sessionId": session["id"], "nativeSessionId": session["id"], "backend": "fly",
        "directory": session["workspace"], "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        "title": session.get("title", "Report Scout"), "model": session["model"],
        "artifactDigest": session["artifact_digest"],
    })


def create(session_id: str, project_id: str, workspace: Path, selected: dict) -> dict:
    if load(session_id) is not None:
        raise ServiceError("session_exists", "The session already exists; resume it or use a new session ID.", 409)
    workspace_info = workspace.stat()
    session = {
        "schema": 1, "id": session_id, "project_id": project_id,
        "pid": project_layout.runtime_key(project_id), "workspace": str(workspace),
        "workspace_identity": [workspace_info.st_dev, workspace_info.st_ino],
        "model": selected["model"], "fly_id": selected["id"], "fly_name": selected["name"],
        "artifact_digest": selected["digest"], "revision": selected["revision"],
        "created_at": now(), "updated_at": now(), "title": "Report Scout",
        "messages": [], "runs": [],
    }
    save(session)
    return session


@contextmanager
def turn_lease(session_id: str):
    """A fail-closed cross-process lock: never execute two turns of one session."""
    target = _directory("locks") / f"{checked_id(session_id)}.lock"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ServiceError("unsafe_state", "The fly session lock is not a regular file.", 500)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServiceError("session_busy", "This fly session already has a running turn.", 409) from exc
        yield
    finally:
        os.close(fd)


def append_message(session: dict, role: str, text: str, *, run_id: str | None = None) -> None:
    session["messages"].append({"id": str(uuid.uuid4()), "role": role, "text": text,
                                "created_at": now(), "run_id": run_id})


def _terminal_record(session: dict, run: dict) -> dict | None:
    """A durable completion belongs to exactly one pinned session and turn."""
    record = get_run(run["id"])
    if record is None:
        return None
    binding = {"id": run["id"], "session_id": session["id"], "pid": session["pid"],
               "project_id": session["project_id"], "artifact_digest": session["artifact_digest"],
               "started_at": run["started_at"]}
    if (any(record.get(key) != value for key, value in binding.items())
            or not isinstance(record.get("status"), str) or record["status"] not in _TERMINAL
            or not isinstance(record.get("message"), str)
            or not isinstance(record.get("completed_at"), str)
            or not (record.get("result") is None or isinstance(record.get("result"), dict))):
        raise ServiceError("invalid_state", "The saved run does not match its pinned session. Preserve the record and restore valid state.", 500)
    try:
        completed = datetime.fromisoformat(record["completed_at"].replace("Z", "+00:00"))
        if completed.tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise ServiceError("invalid_state", "The saved run has an invalid completion timestamp.", 500) from exc
    return record


def _reconcile_completion(session: dict, run: dict, record: dict) -> None:
    """Publish an already durable completion, without duplicating its bubble."""
    replies = [row for row in session["messages"]
               if row.get("run_id") == run["id"] and row.get("role") == "assistant"]
    if replies and (len(replies) != 1 or replies[0].get("text") != record["message"]):
        raise ServiceError("invalid_state", "The saved transcript disagrees with its completed run.", 500)
    run.update(status=record["status"], completed_at=record["completed_at"])
    if not replies:
        append_message(session, "assistant", record["message"], run_id=run["id"])
        session["messages"][-1]["created_at"] = record["completed_at"]
    save(session)


def finish(session: dict, run_id: str, status: str, message: str, result: dict | None = None) -> dict:
    run = next(row for row in session["runs"] if row["id"] == run_id)
    record = _terminal_record(session, run)
    if record is None:
        if status not in _TERMINAL:
            raise ServiceError("invalid_state", "A completed run requires a terminal status.", 500)
        record = {"schema": 1, "id": run_id, "session_id": session["id"], "pid": session["pid"],
                  "project_id": session["project_id"], "artifact_digest": session["artifact_digest"],
                  "status": status, "started_at": run["started_at"], "completed_at": now(),
                  "result": result, "message": message}
        # Commit the full record before publishing its transcript/index. A
        # failure of the second write must never replace a completed report.
        _write(_path("runs", run_id), record)
    _reconcile_completion(session, run, record)
    return record


def recover(session: dict) -> None:
    """Called only while holding the turn lease; stale work is never replayed."""
    for run in session["runs"]:
        if run["status"] == "running":
            record = _terminal_record(session, run)
            if record is not None:
                _reconcile_completion(session, run, record)
            else:
                finish(session, run["id"], "interrupted", "The previous fly run was interrupted before completion. No automatic retry occurred; send /run to start a new inspection.")


def get_run(run_id: str) -> dict | None:
    return _read(_path("runs", run_id))


def owns_session(session_id: str) -> bool:
    if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
        return False
    try:
        return load(session_id) is not None
    except (ServiceError, OSError, ValueError):
        return False


def get_messages(session_id: str) -> list[dict]:
    session = load(session_id)
    if not session:
        return []
    # A process restart leaves no lease. Reconcile once without re-executing files.
    try:
        with turn_lease(session_id):
            session = load(session_id)
            recover(session)
    except ServiceError as exc:
        if exc.code != "session_busy":
            raise
    return [{
        "id": row["id"], "session_id": session_id, "time_created": row["created_at"],
        "data": {"role": row["role"], "model_id": session["model"], "provider_id": "fly",
                 "cost": 0, "tokens": None, "finish": "stop" if row["role"] == "assistant" else None, "error": None},
        "parts": [{"id": row["id"] + "_text", "message_id": row["id"], "session_id": session_id,
                   "time_created": row["created_at"], "data": {"type": "text", "text": row["text"]}}],
    } for row in session["messages"]]


def enrich_project_session(meta: dict, key: str, default_agent: str):
    session = load(meta.get("sessionId", "")) if owns_session(meta.get("sessionId", "")) else None
    return (session["created_at"], session["title"], session["project_id"]) if session else (None, None, default_agent)


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    return _path("sessions", session_id) if owns_session(session_id) else None


def list_native_sessions() -> list[dict]:
    rows = []
    for path in sorted(_directory("sessions").glob("*.json"))[:1000]:
        try:
            session = load(path.stem)
        except (ServiceError, OSError, ValueError):
            continue
        if not session:
            continue
        rows.append({"id": session["id"], "project_id": None, "parent_id": None,
                     "slug": None, "agent": session["project_id"], "directory": session["workspace"],
                     "title": session["title"], "version": 1, "summary_additions": 0,
                     "summary_deletions": 0, "summary_files": 0, "summary_diffs": [],
                     "is_pinned": False, "permission": {}, "time_created": session["created_at"],
                     "time_updated": session["updated_at"], "time_compacting": None, "time_archived": None})
    return rows


def set_session_directory(session_id: str, directory: str) -> dict | None:
    if not owns_session(session_id):
        return None
    # The generic session route has no ServiceError mapping for this hook.
    from fastapi import HTTPException
    raise HTTPException(409, "Fly sessions are pinned to their project. Start a new session to change workspace.")
