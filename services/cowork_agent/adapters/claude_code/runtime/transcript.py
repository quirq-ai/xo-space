"""Reading claude's own files (storm ch 02 §7): its transcripts stay where
claude writes them; nothing is copied.

::

    ~/.claude/projects/<encoded cwd>/<session>.jsonl                         a session
    ~/.claude/projects/<encoded cwd>/<session>/subagents/agent-<id>.jsonl    a sub-agent's run
    ~/.claude/projects/<encoded cwd>/<session>/subagents/agent-<id>.meta.json  its sidecar

A page is the steps of the transcript's conversation records (``user`` and
``assistant``, and the messages a running sub-agent was sent) in file order,
as the contract's neutral ``TranscriptItem`` (§0.110): each content block of
a record is one step. A forked run starts with the parent's history it
inherited; its own steps start at its directive. The cursor is the byte
offset to continue from, so each page reads only its own part of the file
and pages stay stable while claude appends; a record's steps are never split
between pages, so a page can end a few steps past ``limit``. Blocking file
I/O: callers run these in a thread.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from services.cowork_agent.adapters.claude_code._project_encoding import encode_path
from services.cowork_agent.adapters.claude_code.runtime.tools import describe_call, one_line
from services.cowork_agent.adapters.claude_code.runtime.wire import plan_items, thinking_steps
from services.cowork_agent.adapters.contract import (
    RunInfo,
    RuntimeFailure,
    SessionInfo,
    TranscriptItem,
    TranscriptPage,
)
from services.timestamps import iso, parse_ts

CONVERSATION = frozenset({"user", "assistant"})
TITLE_MAX = 80
#: Records read from the top of a transcript to find its cwd and title.
HEAD_LINES = 50
#: ``toolDenialKind`` of a call claude stopped rather than refused; any other
#: value means the call was denied (by the person, a rule, auto mode).
DENIAL_INTERRUPTED = "interrupted"
#: Messages sent to a running sub-agent, which claude records as meta: from
#: its coordinator (the run's parent) or a peer. claude's other meta records
#: (reminders, a background task's notification) are its own plumbing.
MESSAGE_ORIGINS = frozenset({"coordinator", "peer"})
#: A forked run's own first step: claude's instructions to the fork, then the
#: parent's directive. Everything before it is the parent's history.
FORK_OPEN, FORK_CLOSE = "<fork-boilerplate>", "</fork-boilerplate>"
#: claude's envelope for an output too large for its context: the whole output
#: is saved to a file in claude's home and the record keeps its first 2 KB.
PERSISTED = re.compile(r"<persisted-output>\nOutput too large \((\d+(?:\.\d+)?)KB\)\. Full output saved to: [^\n]*"
                       r"\n\nPreview \([^)\n]*\):\n(.*)\n</persisted-output>", re.DOTALL)
KIB = 1024
#: ``toolUseResult.type`` of a Write that replaced a file that was there.
WRITE_REPLACED = "update"
#: Records read past a full page for the results of its Write calls.
RESULT_LOOKAHEAD = 64


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
    forked = _json_file(path.with_suffix(".meta.json")).get("isFork") is True
    return _page(path, after=after, limit=limit, sidechains=True, own_from=_fork_start(path) if forked else 0)


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


def _page(path: Path, *, after: str | None, limit: int, sidechains: bool, own_from: int = 0) -> TranscriptPage:
    """``own_from``: the byte offset the transcript's own steps start at; a
    cursor before it isn't a place in this transcript."""
    limit = max(limit, 1)
    start = _cursor(path, after)
    if start < own_from:
        if after:
            raise _not_a_cursor(path, after)
        start = own_from
    items: list[TranscriptItem] = []
    writes: dict[str, int] = {}             # a Write call on this page -> its item's index
    last, following, past = start, None, 0
    for end, record in _records(path, start):
        if following is None and len(items) >= limit:
            following = str(last)
        if following is not None:           # past the page: only the results of its Writes
            if not writes or past >= RESULT_LOOKAHEAD:
                break
            past += 1
        else:
            last = end
            if _shown(record, sidechains):
                for item in _steps(record, end):
                    if item.type == "tool_call" and item.tool == "Write":
                        writes[item.call_id] = len(items)
                    items.append(item)
        _settle_writes(record, writes, items)
    return TranscriptPage(items=tuple(items), next=following)


def _shown(record: dict[str, Any], sidechains: bool) -> bool:
    if record.get("type") not in CONVERSATION or (record.get("isSidechain") and not sidechains):
        return False
    return not record.get("isMeta") or _origin(record).get("kind") in MESSAGE_ORIGINS


def _settle_writes(record: dict[str, Any], writes: dict[str, int], items: list[TranscriptItem]) -> None:
    """A Write's call can't say whether its file was there; its result can:
    one that replaced a file is ``edit_file``."""
    if not writes or record.get("type") != "user":
        return
    answers = [b for b in _blocks(record) if isinstance(b, dict) and b.get("type") == "tool_result"
               and b.get("tool_use_id") in writes]
    for block in answers:
        index = writes.pop(block["tool_use_id"])
        if len(answers) == 1 and _kept(record).get("type") == WRITE_REPLACED:
            items[index] = dataclasses.replace(items[index], kind="edit_file")


def _fork_start(path: Path) -> int:
    """Where a forked run's own steps start: the record that gives the fork
    its directive. Before it is the parent's history the fork inherited,
    not the run's own steps (ch 17 §2.3). 0 when claude recorded no such
    record: then every step is shown, none dropped."""
    begin = 0
    for end, record in _records(path, 0):
        if record.get("type") == "user" and not record.get("isMeta") and _fork_directive(_blocks(record)):
            return begin
        begin = end
    return 0


def _fork_directive(blocks: list[Any]) -> tuple[int, str] | None:
    """(the block's place, the parent's directive) when a block hands a fork
    its directive after claude's instructions to it."""
    for n, block in enumerate(blocks):
        text = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else None
        if isinstance(text, str) and text.lstrip().startswith(FORK_OPEN) and FORK_CLOSE in text:
            return n, text.split(FORK_CLOSE, 1)[1].strip()
    return None


def _steps(record: dict[str, Any], end: int) -> list[TranscriptItem]:
    """One record's steps, a content block each. The first keeps the
    record's ``uuid`` as its id (the record's end offset if it has none);
    a later one adds its place: ``<uuid>:<n>``. A fork's first record is
    only the parent's directive: the call result in it is the parent's."""
    blocks = _blocks(record)
    uid = record.get("uuid") if isinstance(record.get("uuid"), str) and record.get("uuid") else f"@{end}"
    ts = _ts(record.get("timestamp"))
    directive = _fork_directive(blocks) if record["type"] == "user" else None
    if directive:
        n, text = directive
        return [TranscriptItem(id=f"{uid}:{n}" if n else uid, type="input", ts=ts, text=text)]
    return [_step(record, block, f"{uid}:{n}" if n else uid, ts) for n, block in enumerate(blocks)]


def _blocks(record: dict[str, Any]) -> list[Any]:
    """A record's content blocks; a peer's message is its own words, without
    claude's wrapping around them."""
    body = _origin(record).get("body") if record.get("isMeta") else None
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = body if isinstance(body, str) else message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _step(record: dict[str, Any], block: object, item_id: str, ts: str | None) -> TranscriptItem:
    btype = block.get("type") if isinstance(block, dict) else None
    if btype == "text":
        kind = "input" if record["type"] == "user" else "error" if record.get("isApiErrorMessage") else "text"
        return TranscriptItem(id=item_id, type=kind, ts=ts, text=_text(block.get("text")))
    if btype in ("thinking", "redacted_thinking"):        # redacted: claude keeps no text
        thought = _text(block.get("thinking"))
        return TranscriptItem(id=item_id, type="thinking", ts=ts, text=thought, steps=thinking_steps(thought),
                              duration_ms=_count(record.get("thinkingDurationMs")))
    if btype == "tool_use":
        tool = _text(block.get("name"))
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else None
        call = describe_call(tool, args, cwd=cwd, live=False)
        return TranscriptItem(id=item_id, type="tool_call", ts=ts, call_id=_text(block.get("id")) or item_id,
                              tool=tool, kind=call.kind, title=call.title, target=call.target,
                              plan=plan_items(tool, args) or ())
    if btype == "tool_result":
        return _result(record, block, item_id, ts)
    return TranscriptItem(id=item_id, type="other", ts=ts, text=str(btype or "unreadable block"))


def _result(record: dict[str, Any], block: dict[str, Any], item_id: str, ts: str | None) -> TranscriptItem:
    """A call's result. An output too large for claude's context is kept
    elsewhere and recorded in claude's envelope: the text is the preview
    inside it (and any note claude added after it), marked cut, with the
    whole size from claude's own count, or, when claude kept none, from the
    envelope's header, which gives it to a tenth of a KB (approximate)."""
    output = _output(block.get("content"))
    denial = record.get("toolDenialKind")
    status = ("cancelled" if denial == DENIAL_INTERRUPTED else "denied" if denial
              else "error" if block.get("is_error") else "ok")
    whole = _count(_kept(record).get("persistedOutputSize"))
    envelope = PERSISTED.match(output)
    if envelope:
        output = envelope.group(2) + output[envelope.end():]
        whole = whole if whole is not None else round(float(envelope.group(1)) * KIB)
    cut = whole is not None and whole > len(output.encode())
    return TranscriptItem(id=item_id, type="tool_result", ts=ts, call_id=_text(block.get("tool_use_id")) or item_id,
                          status=status, text=output, bytes=whole if cut else None, truncated=cut)


def _output(content: object) -> str:
    """A tool's output as text: a list's text parts joined by lines, each
    other part named in brackets (``[image]``) so the output shows it."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    return "\n".join(_text(part.get("text")) if isinstance(part, dict) and part.get("type") == "text"
                     else f"[{part.get('type') if isinstance(part, dict) else 'unreadable part'}]"
                     for part in content)


def _origin(record: dict[str, Any]) -> dict[str, Any]:
    return record["origin"] if isinstance(record.get("origin"), dict) else {}


def _kept(record: dict[str, Any]) -> dict[str, Any]:
    """claude's own account of a call's result (``toolUseResult``)."""
    return record["toolUseResult"] if isinstance(record.get("toolUseResult"), dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _ts(value: object) -> str | None:
    moment = parse_ts(value)
    return iso(moment) if moment else None


def _cursor(path: Path, after: str | None) -> int:
    """The byte offset a cursor names. It must be one a page of this
    transcript gave: a whole number, inside the file, at the start of a line."""
    if not after:
        return 0
    stale = _not_a_cursor(path, after)
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


def _not_a_cursor(path: Path, after: str) -> RuntimeFailure:
    return RuntimeFailure("protocol", f"{after!r} is not a place in transcript {path.stem}; read it again from "
                                      "the start (no cursor)")


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
    updated = iso(datetime.fromtimestamp(mtime, timezone.utc))
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
