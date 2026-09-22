"""Grok Bot sessions capability — list/read sand-data transcript JSONL.

Host journal files live at ``agent-transcripts/<agentId>/<agentId>.jsonl``
in the legacy envelope ``{role, message: {content}}``. Missing or unreadable
sand-data degrades to an empty list (chat still works through the gateway).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.cowork_agent.adapters.grokbot.paths import (
    is_safe_folder_id,
    is_subagent_id,
    is_valid_sand_agent_id,
    resolve_sand_root,
    transcript_path,
    transcripts_dir,
)
from services.cowork_agent.helpers import iso_now, short_id, strip_workspace_preamble

USES_PROJECT_SESSIONS = False
_BACKEND = "grokbot"
_NON_MESSAGE_TYPES = frozenset({"metadata", "turn_ended"})


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Grok Bot does not tee into xo-projects; identity enrichment only."""
    return None, None, default_agent


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Locate ``agent-transcripts/<id>/<id>.jsonl`` when the id is a host seat."""
    native = ""
    if isinstance(meta, dict):
        native = str(meta.get("nativeSessionId") or "")
    candidate = native or session_id
    if not candidate or not is_valid_sand_agent_id(candidate):
        return None
    try:
        path = transcript_path(candidate)
    except ValueError:
        return None
    return path if path.is_file() else None


def list_native_sessions() -> list[dict]:
    """Session rows from on-disk transcript JSONL. Empty when sand-data is absent."""
    root = transcripts_dir()
    if not root.is_dir():
        return []
    rows: list[dict] = []
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    for agent_id in names:
        if not is_safe_folder_id(agent_id) or is_subagent_id(agent_id):
            continue
        path = root / agent_id / f"{agent_id}.jsonl"
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        records = _read_transcript_lines(path)
        time_updated = _iso_from_mtime(stat.st_mtime) or iso_now()
        time_created = _first_timestamp(records) or time_updated
        rows.append({
            "id": agent_id,
            "project_id": None,
            "parent_id": None,
            "slug": None,
            "agent": agent_id,
            "directory": str(resolve_sand_root()),
            "title": _title_from_records(records),
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
    rows.sort(key=lambda row: row["time_updated"], reverse=True)
    return rows


def owns_session(session_id: str) -> bool:
    if not session_id or not is_valid_sand_agent_id(session_id):
        return False
    try:
        return transcript_path(session_id).is_file()
    except ValueError:
        return False


def get_messages(session_id: str) -> list:
    path = resolve_native_file({}, session_id)
    if not path:
        return []
    return _convert_messages(session_id, _read_transcript_lines(path))


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Recorded no-op: host seats have no Space workspace-directory field."""
    if not owns_session(session_id):
        return None
    return {
        "ok": True,
        "session_id": session_id,
        "directory": directory,
        "backend": _BACKEND,
        "applied": False,
    }


def _iso_from_mtime(mtime: float) -> str | None:
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _first_timestamp(records: list[dict[str, Any]]) -> str | None:
    for record in records:
        for key in ("timestamp", "createdAt", "created_at"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value
            message = record.get("message")
            if isinstance(message, dict):
                nested = message.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested
    return None


def _is_non_message(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in _NON_MESSAGE_TYPES


def _read_transcript_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or _is_non_message(parsed):
            continue
        role = parsed.get("role")
        message = parsed.get("message")
        if not isinstance(role, str) or not isinstance(message, dict):
            continue
        if "content" not in message:
            continue
        records.append(parsed)
    return records


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _title_from_records(records: list[dict[str, Any]]) -> str:
    for record in records:
        if record.get("role") != "user":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        for block in _text_blocks(message.get("content")):
            if block.get("type") != "text":
                continue
            text = strip_workspace_preamble(str(block.get("text") or "")).strip()
            if not text:
                continue
            return text[:80] + ("..." if len(text) > 80 else "")
    return "Untitled Session"


def _convert_messages(session_id: str, records: list[dict[str, Any]]) -> list[dict]:
    messages: list[dict] = []
    last_user: str | None = None
    pending_tools: list[dict] = []
    for record in records:
        role = record.get("role") or ""
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        mid = str(record.get("id") or short_id())
        timestamp = (
            record.get("timestamp")
            or message.get("timestamp")
            or iso_now()
        )
        blocks = _text_blocks(message.get("content"))
        if role == "user":
            parts = []
            texts: list[str] = []
            for block in blocks:
                if block.get("type") != "text":
                    continue
                text = strip_workspace_preamble(str(block.get("text") or ""))
                if not text:
                    continue
                texts.append(text)
                parts.append({
                    "id": f"{mid}_p{len(parts)}",
                    "message_id": mid,
                    "session_id": session_id,
                    "time_created": timestamp,
                    "data": {"type": "text", "text": text},
                })
            joined = "".join(texts)
            if joined and joined == last_user:
                continue
            if joined:
                last_user = joined
            if not parts:
                continue
            messages.append({
                "id": mid,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"role": "user"},
                "parts": parts,
            })
        elif role == "assistant":
            parts = []
            for block in blocks:
                btype = block.get("type")
                if btype == "text":
                    text = str(block.get("text") or "")
                    if not text:
                        continue
                    parts.append({
                        "id": f"{mid}_p{len(parts)}",
                        "message_id": mid,
                        "session_id": session_id,
                        "time_created": timestamp,
                        "data": {"type": "text", "text": text},
                    })
                elif btype == "tool_use":
                    name = str(block.get("name") or "tool")
                    part = {
                        "id": f"{mid}_p{len(parts)}",
                        "message_id": mid,
                        "session_id": session_id,
                        "time_created": timestamp,
                        "data": {
                            "type": "tool",
                            "tool": name,
                            "call_id": str(block.get("id") or ""),
                            "state": {
                                "status": "completed",
                                "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                                "output": None,
                                "metadata": None,
                                "title": name,
                                "time_start": timestamp,
                                "time_end": timestamp,
                                "time_compacted": None,
                            },
                        },
                    }
                    parts.append(part)
                    pending_tools.append(part)
                elif btype == "tool_result":
                    output = block.get("result")
                    if output is None:
                        output = block.get("content")
                    if pending_tools:
                        pending_tools.pop(0)["data"]["state"]["output"] = output
            if not parts:
                continue
            messages.append({
                "id": mid,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {
                    "role": "assistant",
                    "model_id": message.get("model"),
                    "provider_id": None,
                    "cost": None,
                    "tokens": None,
                    "finish": None,
                    "error": None,
                },
                "parts": parts,
            })
    return messages
