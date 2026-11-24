import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from services.cowork_agent.adapters import contract as c


@lru_cache(maxsize=512)
def _summary(path, mtime, size):
    return json.loads(path.read_bytes())


def summaries():
    root = Path(os.environ.get("GROK_HOME", "~/.grok")).expanduser() / "sessions"
    for path in root.glob("*/*/summary.json"):
        try:
            stat = path.stat()
            if stat.st_size > 1024 * 1024:
                continue
            data = _summary(path, stat.st_mtime_ns, stat.st_size)
        except (ValueError, OSError):
            continue
        info = data.get("info") if isinstance(data, dict) else None
        if isinstance(info, dict) and info.get("id") == path.parent.name and isinstance(info.get("cwd"), str):
            yield data, path.parent


def files():
    for data, folder in summaries():
        yield data["info"]["id"], data["info"]["cwd"], folder / "updates.jsonl"


def runs(native_id):
    sessions = {folder.name: folder for _, folder in summaries()}
    if native_id in sessions:
        for metadata in (sessions[native_id] / "subagents").glob("*/meta.json"):
            child = metadata.parent.name
            if child in sessions:
                yield child, sessions[child] / "updates.jsonl"


def decode(row, key):
    params = row.get("params")
    if row.get("method") != "session/update" or not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    stamp = row.get("timestamp")
    try:
        ts = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(stamp, (float, int)) else None
    except (OSError, OverflowError, ValueError):
        ts = None
    kind = update.get("sessionUpdate")
    roles = {"user_message_chunk": "input", "agent_message_chunk": "text", "agent_thought_chunk": "thinking"}
    if kind in roles:
        content = update.get("content") or {}
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            return c.TranscriptItem(id=key, ts=ts, type=roles[kind], text=content["text"])
    if kind == "tool_call":
        # Titles can include filenames or command lines. Only the machine name leaves the transcript.
        tool = (update.get("_meta") or {}).get("toolName") or update.get("kind") or "tool"
        args = update.get("rawInput") or {}
        target = args.get("file_path") or args.get("path") if isinstance(args, dict) else None
        kind = {"edit": "edit_file", "read": "read", "execute": "command"}.get(update.get("kind"), "other")
        return c.TranscriptItem(id=key, ts=ts, type="tool_call", call_id=update.get("toolCallId"),
                                tool=tool, title=update.get("title") or tool, kind=kind,
                                target=target if isinstance(target, str) else None)
    if kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
        return c.TranscriptItem(id=key, ts=ts, type="tool_result", call_id=update.get("toolCallId"),
                                status="ok" if update["status"] == "completed" else "error")
    return None
