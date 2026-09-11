"""A session as a flat chat transcript: title plus role/content bubbles.

GET /api/messages/{id} returns the full record — every assistant record of a
turn, tool calls with their input and output, reasoning blocks. A chat-style
consumer wants what the person saw: one bubble per turn, text only. This is
that projection; it reads nothing the messages route does not already read.

Rules (see tests/test_session_transcript.py):
- a message's content is its text parts joined with blank lines; reasoning
  parts are dropped, tool parts are dropped unless include_tools, in which
  case each becomes one line `[<tool>] <first line of the command/input>`;
- consecutive assistant messages merge into one bubble (a turn that ran
  tools is several records in the store), keeping the first record's id;
- bubbles left empty by the rules above are removed;
- the title is the session's, cut to TITLE_MAX characters on a word boundary.
"""
from __future__ import annotations

from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine.sessions_io import find_session_backend, load_all_sessions

TITLE_MAX = 50


class SessionNotFound(LookupError):
    pass


def truncate_title(title: str, limit: int = TITLE_MAX) -> str:
    title = " ".join((title or "").split())
    if len(title) <= limit:
        return title
    cut = title[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…"


def _tool_line(data: dict) -> str:
    inp = (data.get("state") or {}).get("input")
    detail = ""
    if isinstance(inp, dict):
        detail = str(inp.get("command") or inp.get("description") or inp.get("path") or "")
    elif inp:
        detail = str(inp)
    detail = detail.strip().splitlines()[0] if detail.strip() else ""
    return f"[{data.get('tool') or 'tool'}] {detail}".rstrip()


def _content_of(message: dict, include_tools: bool) -> str:
    pieces: list[str] = []
    for part in message.get("parts") or []:
        data = part.get("data") or {}
        kind = data.get("type")
        if kind == "text":
            text = (data.get("text") or "").strip()
            if text:
                pieces.append(text)
        elif kind == "tool" and include_tools:
            pieces.append(_tool_line(data))
    return "\n\n".join(pieces)


def build_transcript(title: str, messages: list[dict], *, include_tools: bool = False) -> dict:
    """Pure: the record's messages -> {title, messages:[{id, role, content}]}."""
    bubbles: list[dict] = []
    for message in messages:
        role = (message.get("data") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        content = _content_of(message, include_tools)
        if bubbles and role == "assistant" and bubbles[-1]["role"] == "assistant":
            if content:
                joined = "\n\n".join(p for p in (bubbles[-1]["content"], content) if p)
                bubbles[-1]["content"] = joined
            continue
        bubbles.append({"id": message.get("id"), "role": role, "content": content})
    return {
        "title": truncate_title(title),
        "messages": [b for b in bubbles if b["content"]],
    }


def load_transcript(session_id: str, *, include_tools: bool = False) -> dict:
    """The transcript of a known session; SessionNotFound otherwise. Reads
    messages through the owning adapter's sessions capability, exactly as
    GET /api/messages does."""
    session = next((s for s in load_all_sessions() if s.get("id") == session_id), None)
    if session is None:
        raise SessionNotFound(session_id)
    backend = find_session_backend(session_id)
    mod = try_load_capability("sessions", agent=backend) if backend else None
    fn = getattr(mod, "get_messages", None) if mod else None
    messages = fn(session_id) if fn else []
    return build_transcript(session.get("title") or "", messages, include_tools=include_tools)
