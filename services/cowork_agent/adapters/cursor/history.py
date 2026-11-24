from datetime import datetime, timezone

from services.cowork_agent.adapters import contract as c
from . import session_telemetry as telemetry


def files():
    for native, folder, path in telemetry._discover_transcripts(telemetry._cursor_home()):
        if "subagents" not in path.relative_to(telemetry._cursor_home() / "projects" / folder).parts:
            yield native, telemetry._decode_project_path(folder), path


def runs(native_id):
    for native, _, path in files():
        if native == native_id and path.parent.name == native:
            for child in (path.parent / "subagents").rglob("*.jsonl"):
                yield child.stem, child


def decode(row, key):
    role = row.get("role")
    if role not in ("user", "assistant"):
        return None
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else message
    stamp = row.get("timestamp") or row.get("createdAt")
    try:
        ts = datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        ts = None
    blocks = content if isinstance(content, list) else [content]
    items = []
    for index, block in enumerate(blocks):
        ident = f"{key}.{index}"
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name") or "tool"
            args = block.get("input") or {}
            target = args.get("file_path") or args.get("path") if isinstance(args, dict) else None
            kind = {"Write": "create_file", "Edit": "edit_file", "Read": "read"}.get(name, "other")
            items.append(c.TranscriptItem(id=ident, type="tool_call", ts=ts, tool=name, title=name,
                                          kind=kind, target=target if isinstance(target, str) else None,
                                          call_id=block.get("id") or ident))
        elif isinstance(block, dict) and block.get("type") == "tool_result":
            items.append(c.TranscriptItem(id=ident, type="tool_result", ts=ts, call_id=block.get("tool_use_id"),
                                          status="error" if block.get("is_error") else "ok",
                                          text=telemetry._content_text(block.get("content"))))
        else:
            text = telemetry._content_text([block]) if isinstance(block, dict) else block
            if isinstance(text, str) and text:
                items.append(c.TranscriptItem(id=ident, type="input" if role == "user" else "text", text=text, ts=ts))
    return items
