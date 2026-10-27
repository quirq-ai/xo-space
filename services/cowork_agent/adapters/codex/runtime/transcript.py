"""Reading codex's own files (storm ch 03 §5.5, §10): its transcripts stay
where codex writes them; nothing is copied.

::

    $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl

Every thread has its own rollout, a sub-agent's included. A sub-agent's
first record (``session_meta``) names the session it belongs to
(``session_id``, the root thread), its parent thread and its depth.

A page is the thread's steps in file order, as the contract's neutral
``TranscriptItem`` (§0.110): codex records each finished item
(``event_msg`` ``item_completed``, its v2 item in snake case) and each
message another agent sent it (``response_item`` ``agent_message``). A tool
item is two steps: its call and its result. The cursor is the number of
records read, so pages stay stable while codex appends, and a record's steps
are never split between pages (a page can end one step past ``limit``).
Blocking file I/O: callers run these in a thread.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from services.cowork_agent.adapters.codex.runtime.tools import (
    describe_file,
    describe_item,
    file_call_id,
    file_changes,
    one_line,
)
from services.cowork_agent.adapters.contract import (
    RunInfo,
    RuntimeFailure,
    SessionInfo,
    TranscriptItem,
    TranscriptPage,
)

TITLE_MAX = 80
#: A tool result's text is cut to this many characters here (core cuts again
#: to its own limit and fills ``bytes``/``truncated``).
RESULT_MAX = 64_000
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CAMEL = re.compile(r"_([a-z])")
RESULT_STATUS = {"completed": "ok", "failed": "error", "declined": "denied", "cancelled": "cancelled",
                 "interrupted": "cancelled"}


def sessions_root(codex_home: Path) -> Path:
    return codex_home / "sessions"


def rollout(codex_home: Path, thread_id: str) -> Path | None:
    """The newest rollout of a thread, or None."""
    if not _UUID.match(thread_id or ""):
        return None
    root = sessions_root(codex_home)
    if not root.is_dir():
        return None
    found = sorted(root.rglob(f"rollout-*-{thread_id}.jsonl"), key=_mtime, reverse=True)
    return found[0] if found else None


def meta(path: Path) -> dict[str, Any]:
    """A rollout's ``session_meta`` payload (its first record), or ``{}``."""
    try:
        with path.open(encoding="utf-8") as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("type") == "session_meta":
                    payload = record.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def list_sessions(codex_home: Path, *, cwd: str | None, limit: int) -> list[SessionInfo]:
    root = sessions_root(codex_home)
    if not root.is_dir():
        return []
    out: list[SessionInfo] = []
    for path in sorted(root.rglob("rollout-*.jsonl"), key=_mtime, reverse=True):
        info = meta(path)
        if info.get("parent_thread_id") or _spawn(info):
            continue                        # a sub-agent's, not a session
        if cwd is not None and info.get("cwd") != cwd:
            continue
        thread = info.get("id") or info.get("session_id")
        if not isinstance(thread, str):
            continue
        out.append(SessionInfo(native_id=thread, cwd=info.get("cwd") if isinstance(info.get("cwd"), str) else None,
                               title=_title(path), updated_at=_iso(_mtime(path))))
        if len(out) >= limit:
            break
    return out


def read_session(codex_home: Path, native_id: str, *, after: str | None, limit: int) -> TranscriptPage:
    path = rollout(codex_home, native_id)
    if path is None:
        raise RuntimeFailure("session_not_found", f"codex keeps no transcript of thread {native_id}")
    return _page(path, after=after, limit=limit)


def list_runs(codex_home: Path, native_id: str) -> list[RunInfo]:
    """Every sub-agent thread of a session, from their rollouts' first
    records (children are written after the root, so older files are
    skipped)."""
    root_path = rollout(codex_home, native_id)
    if root_path is None:
        return []
    since = _mtime_created(root_path)
    runs: list[RunInfo] = []
    for path in sorted(sessions_root(codex_home).rglob("rollout-*.jsonl"), key=_mtime):
        if path == root_path or _mtime(path) < since:
            continue
        info = meta(path)
        if info.get("session_id") != native_id or info.get("id") == native_id:
            continue
        runs.append(_run_info(info))
    return runs


def read_run(codex_home: Path, native_id: str, run_id: str, *, after: str | None, limit: int) -> TranscriptPage:
    path = rollout(codex_home, run_id)
    if path is None or meta(path).get("session_id") != native_id or run_id == native_id:
        raise RuntimeFailure("session_not_found", f"codex keeps no transcript of sub-agent {run_id} of {native_id}")
    return _page(path, after=after, limit=limit)


def _spawn(info: dict[str, Any]) -> dict[str, Any]:
    source = info.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    return spawn if isinstance(spawn, dict) else {}


def _run_info(info: dict[str, Any]) -> RunInfo:
    spawn = _spawn(info)
    parent = info.get("parent_thread_id") or spawn.get("parent_thread_id")
    root = info.get("session_id")
    path = info.get("agent_path") or spawn.get("agent_path") or ""
    name = path.rsplit("/", 1)[-1].replace("_", " ") if isinstance(path, str) and path else ""
    depth = spawn.get("depth")
    return RunInfo(run_id=str(info.get("id")), parent_run_id=parent if isinstance(parent, str) and parent != root
                   else None, spawn_call_id=None, kind="subagent",
                   agent_type=spawn.get("agent_role") if isinstance(spawn.get("agent_role"), str) else None,
                   title=one_line(name or str(info.get("agent_nickname") or "a sub-agent"), TITLE_MAX),
                   background=False, depth=depth if isinstance(depth, int) and depth > 0 else 1)


# ── a page ────────────────────────────────────────────────────────────────


def _page(path: Path, *, after: str | None, limit: int) -> TranscriptPage:
    limit = max(limit, 1)
    start = _cursor(path, after)
    items: list[TranscriptItem] = []
    following: str | None = None
    for index, record in _records(path, start):
        if len(items) >= limit:
            following = str(index)
            break
        items.extend(_steps(record, index))
    return TranscriptPage(items=tuple(items), next=following)


def _cursor(path: Path, after: str | None) -> int:
    if after is None:
        return 0
    if not after.isdigit():
        raise RuntimeFailure("protocol", f"{after!r} is not a place in this transcript")
    position = int(after)
    count = sum(1 for _ in _lines(path))
    if position > count:
        raise RuntimeFailure("protocol", f"{after!r} is not a place in this transcript")
    return position


def _lines(path: Path) -> Iterator[str]:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.endswith("\n"):     # a last line still being written isn't whole
                    yield line
    except OSError as exc:
        raise RuntimeFailure("session_not_found", f"codex's transcript can't be read: {exc.strerror}") from exc


def _records(path: Path, start: int) -> Iterator[tuple[int, dict[str, Any]]]:
    for index, line in enumerate(_lines(path)):
        if index < start:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield index, record


def _steps(record: dict[str, Any], index: int) -> list[TranscriptItem]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    ts = _ts(record.get("timestamp"))
    kind = record.get("type")
    if kind == "response_item" and payload.get("type") == "agent_message":
        text = _texts(payload.get("content"))
        return [TranscriptItem(id=f"r{index}", type="input", ts=ts, text=text)] if text else []
    if kind != "event_msg" or payload.get("type") != "item_completed":
        return []
    item = _camel(payload.get("item"))
    if not isinstance(item, dict):
        return []
    item_type = item.get("type")
    item_id = str(item.get("id") or f"r{index}")
    if item_type == "userMessage":
        text = _texts(item.get("content"))
        return [TranscriptItem(id=item_id, type="input", ts=ts, text=text)] if text else []
    if item_type == "agentMessage":
        text = _texts(item.get("content")) or (item.get("text") if isinstance(item.get("text"), str) else "")
        return [TranscriptItem(id=item_id, type="text", ts=ts, text=text)] if text else []
    if item_type == "reasoning":
        summary = item.get("summary") or item.get("summaryText") or []
        text = "\n\n".join(s if isinstance(s, str) else str(s.get("text") or "") for s in summary
                           if isinstance(s, (str, dict))) if isinstance(summary, list) else ""
        steps = tuple(one_line(p, TITLE_MAX) for p in text.split("\n\n") if p.strip())
        return [TranscriptItem(id=item_id, type="thinking", ts=ts, text=text, steps=steps)]
    if item_type in ("subAgentActivity", "plan", "hookPrompt", "functionCallOutput"):
        return []
    status = RESULT_STATUS.get(item.get("status"), "ok")
    if item_type == "fileChange" and file_changes(item):
        return _file_items(item_id, ts, item, status)
    call = describe_item(item)
    if call is None:
        return [TranscriptItem(id=item_id, type="other", ts=ts, text=str(item_type or "a step"))]
    if item_type == "commandExecution" and status == "ok" and isinstance(item.get("exitCode"), int) \
            and item["exitCode"] != 0:
        status = "error"
    return [TranscriptItem(id=f"{item_id}:call", type="tool_call", ts=ts, call_id=item_id, tool=call.tool,
                           kind=call.kind, title=call.title, target=call.target),
            TranscriptItem(id=f"{item_id}:result", type="tool_result", ts=ts, call_id=item_id, status=status,
                           text=_result_text(item)[:RESULT_MAX])]


def _file_items(item_id: str, ts: Any, item: dict[str, Any], status: str) -> list[TranscriptItem]:
    """One call and result per file of a patch, with the live rows' ids."""
    out = []
    for index, change in enumerate(file_changes(item)):
        call, call_id = describe_file(change), file_call_id(item_id, index)
        out += [TranscriptItem(id=f"{call_id}:call", type="tool_call", ts=ts, call_id=call_id, tool=call.tool,
                               kind=call.kind, title=call.title, target=call.target),
                TranscriptItem(id=f"{call_id}:result", type="tool_result", ts=ts, call_id=call_id, status=status,
                               text=call.target or "")]
    return out


def _result_text(item: dict[str, Any]) -> str:
    kind = item.get("type")
    if kind == "commandExecution":
        output = item.get("aggregatedOutput")
        return output if isinstance(output, str) else ""
    if kind == "mcpToolCall":
        result = item.get("result")
        if isinstance(result, dict):
            return _texts(result.get("content"))
        error = item.get("error")
        return str(error.get("message") if isinstance(error, dict) else error or "")
    if kind == "fileChange":
        changes = item.get("changes")
        if isinstance(changes, dict):
            changes = [{"path": k, **(v if isinstance(v, dict) else {})} for k, v in changes.items()]
        return "\n".join(str(c.get("path")) for c in changes or [] if isinstance(c, dict))
    return ""


def _texts(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict) and c.get("text"))


def _camel(value: object) -> object:
    """A rollout's item (snake case, ``PascalCase`` types) in the v2 shape
    the live wire uses, so one classifier reads both."""
    if isinstance(value, dict):
        out = {_CAMEL.sub(lambda m: m.group(1).upper(), k): _camel(v) for k, v in value.items()}
        if isinstance(out.get("type"), str) and out["type"][:1].isupper():
            out["type"] = out["type"][0].lower() + out["type"][1:]
        return out
    if isinstance(value, list):
        return [_camel(v) for v in value]
    return value


def _title(path: Path) -> str | None:
    for _index, record in _records(path, 0):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = _camel(payload.get("item"))
            if isinstance(item, dict) and item.get("type") == "userMessage":
                text = _texts(item.get("content"))
                if text:
                    return one_line(text, TITLE_MAX)
    return None


def _ts(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _mtime_created(path: Path) -> float:
    """About when a rollout was started: its name holds the time
    (``rollout-2026-10-22T22-14-29-…``), in codex's local zone, so a day's
    margin."""
    match = re.match(r"rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)", path.name)
    if not match:
        return 0.0
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc).timestamp() - 86_400
    except ValueError:
        return 0.0


def _iso(seconds: float) -> str | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
