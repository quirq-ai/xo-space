"""agy's own files read as the contract's transcript (storm ch 04 §9, §10;
§0.110), and the live follower of a sub-agent's steps.

agy keeps each conversation, sub-agents' included, under
``~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/``: one JSON
line per step in ``transcript_full.jsonl`` (``step_index``, ``source``,
``type``, ``status``, ``created_at``, ``content``, ``tool_calls``,
``thinking``, ``error``). A ``PLANNER_RESPONSE`` is the model's turn (its
words, its thinking, its tool calls); each tool call's result is the
``GENERIC`` step after it, so the n-th call of the response at step k is
step k+n, the index agy's stream and its hook give the same call. Its
``conversation_summaries.db`` lists conversations and their parents.

Every item is in the contract's terms, never agy's own records.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlparse

from services.cowork_agent.adapters.antigravity.runtime.env import agy_home, brain_dir
from services.cowork_agent.adapters.antigravity.runtime.tools import describe, one_line
from services.cowork_agent.adapters.antigravity.runtime.wire import DENIED
from services.cowork_agent.adapters.contract import RunInfo, RuntimeFailure, SessionInfo, TranscriptItem, \
    TranscriptPage

_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_HEADER = re.compile(r"^(?:Created At: \S+\n)?(?:Completed At: \S+\n)?")
_MESSAGE = re.compile(r"\[Message\][^\n]*?sender=(\S+)[^\n]*?content=(.*?)(?:</SYSTEM_MESSAGE>|$)", re.DOTALL)
_CREATED = re.compile(r"Created the following subagent:\s*(\{.*?\n\})", re.DOTALL)
RESULT_MAX = 64 * 1024


def transcript_file(conversation: str, home: Path | None = None) -> Path:
    logs = (home or brain_dir()) / conversation / ".system_generated" / "logs"
    full = logs / "transcript_full.jsonl"
    return full if full.exists() or not (logs / "transcript.jsonl").exists() else logs / "transcript.jsonl"


def steps(conversation: str, home: Path | None = None) -> list[dict[str, Any]]:
    """Every step of a conversation; raises ``session_not_found`` when agy
    keeps none."""
    if not _ID.fullmatch(conversation or ""):
        raise RuntimeFailure("session_not_found", f"not an agy conversation id: {conversation!r}")
    path = transcript_file(conversation, home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeFailure("session_not_found", f"agy keeps no transcript of {conversation}") from exc
    except OSError as exc:
        raise RuntimeFailure("protocol", f"agy's transcript of {conversation} can't be read: {exc.strerror}") from exc
    return parse_lines(text.splitlines())


def parse_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        try:
            step = json.loads(line)
        except ValueError:
            continue
        if isinstance(step, dict) and isinstance(step.get("step_index"), int):
            out.append(step)
    return out


def _ts(step: Mapping[str, Any]) -> str | None:
    stamp = step.get("created_at")
    if isinstance(stamp, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp):
        return stamp
    return None


def _args(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return {k: _unquote(v) for k, v in raw.items()}
    return {}


def _unquote(value: object) -> object:
    """``transcript.jsonl`` keeps each argument re-stringified (``'"ls"'``)."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def result_text(step: Mapping[str, Any]) -> str:
    content = step.get("content") if isinstance(step.get("content"), str) else ""
    return _HEADER.sub("", content, count=1)


def message_text(content: str) -> tuple[str | None, str]:
    """A system message's sender (another agent) and its words."""
    found = _MESSAGE.search(content)
    if found:
        return found.group(1), found.group(2).strip()
    request = _USER_REQUEST.search(content)
    return None, (request.group(1) if request else content).strip()


def items(conversation: str, all_steps: list[dict[str, Any]], *, cwd: str = "/") -> list[TranscriptItem]:
    out: list[TranscriptItem] = []
    calls: dict[int, str] = {}                  # result step index -> its call id
    for step in all_steps:
        index, kind, ts = step["step_index"], step.get("type"), _ts(step)
        content = step.get("content") if isinstance(step.get("content"), str) else ""
        if kind == "USER_INPUT":
            out.append(TranscriptItem(id=f"{index}", type="input", ts=ts, text=message_text(content)[1]))
        elif kind == "SYSTEM_MESSAGE":
            sender, words = message_text(content)
            if sender is not None:
                out.append(TranscriptItem(id=f"{index}", type="input", ts=ts, text=words))
            else:
                out.append(TranscriptItem(id=f"{index}", type="other", ts=ts, text="system message"))
        elif kind == "EPHEMERAL_MESSAGE":
            out.append(TranscriptItem(id=f"{index}", type="other", ts=ts, text="ephemeral message"))
        elif kind == "PLANNER_RESPONSE":
            thinking = step.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                out.append(TranscriptItem(id=f"{index}:t", type="thinking", ts=ts, text=thinking.strip(),
                                          steps=tuple(one_line(l) for l in thinking.strip().splitlines()
                                                      if l.strip())[:20]))
            if content.strip():
                out.append(TranscriptItem(id=f"{index}:c", type="text", ts=ts, text=content.strip()))
            for n, call in enumerate(step.get("tool_calls") or (), start=1):
                if not isinstance(call, Mapping):
                    continue
                tool = str(call.get("name") or "tool")
                described = describe(tool, _args(call.get("args")), cwd)
                call_id = f"{conversation}:{index + n}"
                calls[index + n] = call_id
                out.append(TranscriptItem(id=f"{index}:{n}", type="tool_call", ts=ts, call_id=call_id, tool=tool,
                                          kind=described.kind, title=described.title, target=described.target))
        elif kind == "GENERIC" and index in calls:
            text = result_text(step)
            error = step.get("error") if isinstance(step.get("error"), str) else None
            status = "ok"
            if step.get("status") == "ERROR":
                message = _unquote(error) if error else text
                status = "denied" if DENIED.match(str(message)) else "error"
                text = str(message)
            elif step.get("status") == "RUNNING":
                continue                          # still running: its result comes later
            size = len(text.encode())
            cut = text.encode()[:RESULT_MAX].decode(errors="ignore") if size > RESULT_MAX else text
            out.append(TranscriptItem(id=f"{index}", type="tool_result", ts=ts, call_id=calls[index], status=status,
                                      text=cut, bytes=size if size > RESULT_MAX else None,
                                      truncated=size > RESULT_MAX))
        elif kind == "GENERIC" and step.get("status") == "ERROR" and not calls:
            out.append(TranscriptItem(id=f"{index}", type="error", ts=ts,
                                      text=str(_unquote(step.get("error")) or result_text(step) or "error")))
        elif kind not in ("GENERIC", "CHECKPOINT"):
            out.append(TranscriptItem(id=f"{index}", type="other", ts=ts, text=str(kind or "step").lower()))
    return out


def page(all_items: list[TranscriptItem], *, after: str | None, limit: int) -> TranscriptPage:
    start = 0
    if after is not None:
        ids = [item.id for item in all_items]
        if after not in ids:
            raise RuntimeFailure("protocol", "not a place in this transcript")
        start = ids.index(after) + 1
    limit = max(int(limit), 1)
    chunk = all_items[start:start + limit]
    more = start + limit < len(all_items)
    return TranscriptPage(items=tuple(chunk), next=chunk[-1].id if more and chunk else None)


def read(conversation: str, *, after: str | None = None, limit: int = 50, home: Path | None = None) -> TranscriptPage:
    return page(items(conversation, steps(conversation, home)), after=after, limit=limit)


# ── sub-agents ───────────────────────────────────────────────────────────


def runs_in(conversation: str, all_steps: list[dict[str, Any]]) -> list[RunInfo]:
    """The sub-agents a conversation started, from its own steps: each
    ``invoke_subagent`` result names its child's conversation."""
    out: list[RunInfo] = []
    pending: dict[int, Mapping[str, Any]] = {}
    for step in all_steps:
        index = step["step_index"]
        if step.get("type") == "PLANNER_RESPONSE":
            for n, call in enumerate(step.get("tool_calls") or (), start=1):
                if isinstance(call, Mapping) and call.get("name") == "invoke_subagent":
                    pending[index + n] = _args(call.get("args"))
        elif step.get("type") == "GENERIC" and index in pending:
            args = pending.pop(index)
            for blob in _CREATED.findall(result_text(step)):
                try:
                    created = json.loads(blob)
                except ValueError:
                    continue
                run_id = created.get("conversationId") if isinstance(created, dict) else None
                if not isinstance(run_id, str):
                    continue
                role = args.get("Role") or args.get("TypeName") or "sub-agent"
                out.append(RunInfo(run_id=run_id, parent_run_id=None, spawn_call_id=f"{conversation}:{index}",
                                   kind="subagent", agent_type=args.get("TypeName") if isinstance(
                                       args.get("TypeName"), str) else None,
                                   title=one_line(str(role), 80), background=True, depth=1))
    return out


def list_runs(conversation: str, home: Path | None = None) -> list[RunInfo]:
    return runs_in(conversation, steps(conversation, home))


def read_run(conversation: str, run_id: str, *, after: str | None = None, limit: int = 50,
             home: Path | None = None) -> TranscriptPage:
    if run_id not in {r.run_id for r in list_runs(conversation, home)}:
        raise RuntimeFailure("session_not_found", f"agy keeps no sub-agent {run_id} of {conversation}")
    return read(run_id, after=after, limit=limit, home=home)


def list_sessions(*, cwd: str | None = None, limit: int = 50, home: Path | None = None) -> list[SessionInfo]:
    """Root conversations, newest first, from agy's own index."""
    db = (home or agy_home()) / "conversation_summaries.db"
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute("SELECT conversation_id, title, last_modified_time, workspace_uris FROM "
                           "conversation_summaries WHERE nesting_depth = 0 ORDER BY last_modified_time DESC").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for cid, title, modified, uris in rows:
        folders = _folders(uris)
        if cwd is not None and str(Path(cwd).resolve()) not in folders:
            continue
        out.append(SessionInfo(native_id=cid, cwd=folders[0] if folders else None, title=title or None,
                               updated_at=_iso(modified)))
        if len(out) >= limit:
            break
    return out


def _folders(uris: object) -> list[str]:
    try:
        parsed = json.loads(uris) if isinstance(uris, str) else []
    except ValueError:
        return []
    return [unquote(urlparse(u).path) for u in parsed if isinstance(u, str) and u.startswith("file://")]


def _iso(stamp: object) -> str | None:
    if not isinstance(stamp, str) or len(stamp) < 19:
        return None
    return stamp[:10] + "T" + stamp[11:19] + "Z"


# ── the live follower ────────────────────────────────────────────────────


class Tail:
    """A transcript read as it grows: each ``read()`` gives the steps written
    since the last one (a half-written last line waits)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._rest = b""

    def skip_to_end(self) -> "Tail":
        """From now on: what is already written is left unread. Blocking."""
        try:
            self._offset = self.path.stat().st_size
        except OSError:
            self._offset = 0
        return self

    def read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                data = f.read()
        except OSError:
            return []
        self._offset += len(data)
        data = self._rest + data
        lines = data.split(b"\n")
        self._rest = lines.pop()
        return parse_lines(line.decode(errors="replace") for line in lines if line.strip())
