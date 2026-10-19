"""Reading claude's own files (storm ch 02 §7): its transcripts stay where
claude writes them; nothing is copied.

::

    ~/.claude/projects/<encoded cwd>/<session>.jsonl                         a session
    ~/.claude/projects/<encoded cwd>/<session>/subagents/agent-<id>.jsonl    a sub-agent's run
    ~/.claude/projects/<encoded cwd>/<session>/subagents/agent-<id>.meta.json  its sidecar

A page is the transcript's conversation records (``user`` and ``assistant``)
in file order; the cursor is the byte offset to continue from, so each page
reads only its own part of the file and pages stay stable while claude
appends. Blocking file I/O: callers run these in a
thread.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from services.cowork_agent.adapters.claude_code._project_encoding import encode_path
from services.cowork_agent.adapters.claude_code.runtime.tools import one_line
from services.cowork_agent.adapters.contract import RunInfo, RuntimeFailure, SessionInfo, TranscriptPage

CONVERSATION = frozenset({"user", "assistant"})
TITLE_MAX = 80
#: Records read from the top of a transcript to find its cwd and title.
HEAD_LINES = 50


def projects_root(claude_home: Path) -> Path:
    return claude_home / "projects"


def list_sessions(claude_home: Path, *, cwd: str | None, limit: int) -> list[SessionInfo]:
    root = projects_root(claude_home)
    folders = [root / encode_path(cwd)] if cwd else _dirs(root)
    files = [(mtime, f) for folder in folders for f in _files(folder, "*.jsonl")
             if (mtime := _mtime(f)) is not None]
    files.sort(key=lambda pair: pair[0], reverse=True)
    return [_session_info(path, mtime) for mtime, path in files[:max(limit, 0)]]


def read_session(claude_home: Path, native_id: str, *, after: str | None, limit: int) -> TranscriptPage:
    path = session_file(claude_home, native_id)
    return _page(path, after=after, limit=limit, sidechains=False)


def list_runs(claude_home: Path, native_id: str) -> list[RunInfo]:
    folder = session_file(claude_home, native_id).with_suffix("") / "subagents"
    runs = []
    for transcript in sorted(_files(folder, "agent-*.jsonl")):
        run_id = transcript.stem[len("agent-"):]
        meta = _json_file(transcript.with_suffix(".meta.json"))
        depth = meta.get("spawnDepth")
        parent = meta.get("parentAgentId")
        call = meta.get("toolUseId")
        runs.append(RunInfo(
            run_id=run_id, parent_run_id=parent if isinstance(parent, str) and parent else None,
            spawn_call_id=call if isinstance(call, str) and call else None, kind="subagent",
            agent_type=meta.get("agentType") if isinstance(meta.get("agentType"), str) else None,
            title=one_line(str(meta.get("description") or meta.get("agentType") or run_id), TITLE_MAX),
            background=meta.get("requestShape") == "background",
            depth=depth if isinstance(depth, int) and depth > 0 else 1))
    return runs


def read_run(claude_home: Path, native_id: str, run_id: str, *, after: str | None, limit: int) -> TranscriptPage:
    if not run_id or "/" in run_id or run_id.startswith("."):
        raise RuntimeFailure("session_not_found", f"no sub-agent run {run_id!r}")
    path = session_file(claude_home, native_id).with_suffix("") / "subagents" / f"agent-{run_id}.jsonl"
    if not path.is_file():
        raise RuntimeFailure("session_not_found", f"session {native_id} has no sub-agent run {run_id}")
    return _page(path, after=after, limit=limit, sidechains=True)


def remove_empty_project_folder(claude_home: Path, cwd: str) -> None:
    """``--no-session-persistence`` still creates claude's project folder for
    the cwd, holding only empty folders (``memory/`` on 2.1.301). Remove the
    empty folders, deepest first; a file anywhere keeps its folders. Blocking."""
    folder = projects_root(claude_home) / encode_path(os.path.realpath(cwd))
    for path, _dirs, _files in sorted(os.walk(folder), key=lambda entry: len(entry[0]), reverse=True):
        with contextlib.suppress(OSError):
            os.rmdir(path)


def session_file(claude_home: Path, native_id: str) -> Path:
    """The transcript of ``native_id`` in whichever project folder holds it."""
    if not native_id or "/" in native_id or native_id.startswith("."):
        raise RuntimeFailure("session_not_found", f"no claude session {native_id!r}")
    for folder in _dirs(projects_root(claude_home)):
        candidate = folder / f"{native_id}.jsonl"
        if candidate.is_file():
            return candidate
    raise RuntimeFailure("session_not_found", f"claude has no transcript for session {native_id}")


def _page(path: Path, *, after: str | None, limit: int, sidechains: bool) -> TranscriptPage:
    limit = max(limit, 1)
    start = _cursor(path, after)
    items: list[dict[str, Any]] = []
    last = start
    for end, record in _records(path, start):
        if len(items) >= limit:
            return TranscriptPage(items=tuple(items), next=str(last))
        last = end
        if record.get("type") not in CONVERSATION or record.get("isMeta"):
            continue
        if record.get("isSidechain") and not sidechains:
            continue
        items.append({"id": record.get("uuid"), "type": record["type"], "timestamp": record.get("timestamp"),
                      "message": record.get("message"), "agent_id": record.get("agentId")})
    return TranscriptPage(items=tuple(items), next=None)


def _cursor(path: Path, after: str | None) -> int:
    """The byte offset a cursor names. It must be one a page of this
    transcript gave: a whole number, inside the file, at the start of a line."""
    if not after:
        return 0
    stale = RuntimeFailure("protocol", f"{after!r} is not a cursor of transcript {path.stem}; read it again "
                                       "from the start (no cursor)")
    if not (after.isascii() and after.isdigit()):
        raise stale                     # only what a page gave: plain digits, no sign or space
    offset = int(after)
    try:
        with path.open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            if offset > size:
                raise stale
            if offset:
                f.seek(offset - 1)
                if f.read(1) != b"\n":
                    raise stale
    except OSError as exc:
        raise RuntimeFailure("session_not_found", f"can't read {path.name}: {exc.strerror}") from exc
    return offset


def _records(path: Path, start: int) -> Iterator[tuple[int, dict[str, Any]]]:
    """(byte offset just past the record's line, record) from byte ``start``
    on; unreadable lines are skipped, and a last line claude is still writing
    (no newline yet) is left for the next read."""
    try:
        with path.open("rb") as f:
            f.seek(start)
            offset = start
            for line in iter(f.readline, b""):
                if not line.endswith(b"\n"):
                    return
                offset += len(line)
                try:
                    # Invalid UTF-8 is read with replacement characters: reported, never dropped.
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield offset, record
    except OSError as exc:
        raise RuntimeFailure("session_not_found", f"can't read {path.name}: {exc.strerror}") from exc


def _session_info(path: Path, mtime: float) -> SessionInfo:
    cwd = title = None
    for count, (_offset, record) in enumerate(_records(path, 0), 1):
        if count > HEAD_LINES or (cwd and title):
            break
        if cwd is None and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        if title is None:
            title = _title(record)
    updated = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SessionInfo(native_id=path.stem, cwd=cwd, title=title, updated_at=updated)


def _title(record: dict[str, Any]) -> str | None:
    if record.get("type") in ("summary", "custom-title"):
        text = record.get("summary") or record.get("customTitle")
        return one_line(text, TITLE_MAX) if isinstance(text, str) and text.strip() else None
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    content = (record.get("message") or {}).get("content") if isinstance(record.get("message"), dict) else None
    if isinstance(content, list):
        content = next((b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"), None)
    return one_line(content, TITLE_MAX) if isinstance(content, str) and content.strip() else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None                         # removed while listing


def _json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dirs(root: Path) -> list[Path]:
    try:
        return [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []


def _files(folder: Path, pattern: str) -> list[Path]:
    try:
        return [p for p in folder.glob(pattern) if p.is_file()]
    except OSError:
        return []
