"""What an agent is handed each turn: ``<wake>``, ``<relay>`` and ``<handoff>``.

The grammar is Claude Code Projects' (``docs/sessions-research/02-protocols.md``).
Only this module builds envelopes, and only from store rows: a message's text
is copied here from the store, never from what a coordinator wrote, so who
said what is decided by the server, not by any agent. Every ``<`` ``>`` ``&``
in copied text is escaped, and so are the Unicode look-alikes of the angle
brackets, so text can never open or close a tag. Attribute values are
escaped the same way, plus the double quote.
"""

from __future__ import annotations

from typing import Iterable, Optional

from services.projects_chat.store import now_ms_iso

#: Characters that read as angle brackets; escaped like the real ones.
_LOOKALIKES = {
    "‹": "&#8249;", "›": "&#8250;", "⟨": "&#10216;", "⟩": "&#10217;",
    "〈": "&#9001;", "〉": "&#9002;", "＜": "&#65308;", "＞": "&#65310;",
    "﹤": "&#65124;", "﹥": "&#65125;",
}


def escape_text(text: str) -> str:
    out = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char, entity in _LOOKALIKES.items():
        out = out.replace(char, entity)
    return out


def escape_attr(value: object) -> str:
    return escape_text(str(value)).replace('"', "&quot;").replace("\n", " ")


def _attrs(pairs: Iterable[tuple[str, object]]) -> str:
    return "".join(f' {name}="{escape_attr(value)}"' for name, value in pairs if value is not None)


def _indent(block: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in block.splitlines())


# Who a stored author is, in envelope terms.
_FROM = {"owner": "human", "agent": "agent", "system": "system"}


def message_tag(row: dict, *, trigger: bool = False, trust: Optional[str] = None,
                mention: bool = False, role: Optional[str] = None) -> str:
    """One ``<message>`` from a stored row."""
    attrs = _attrs([
        ("trigger", "true" if trigger else None),
        ("from", _FROM.get(row.get("author_kind"), "system")),
        ("trust", trust),
        ("author-id", row.get("author_id")),
        ("id", row.get("id")),
        ("sent-at", row.get("written_at") or row.get("ts")),
        ("mention", "true" if mention else None),
        ("role", role),
        ("edited", "true" if row.get("edited_at") else None),
    ])
    return f"<message{attrs}>{escape_text(row.get('text', ''))}</message>"


def wake(*, reason: str, chat_id: str, chat_kind: str, trigger: dict,
         thread_ts: Optional[str] = None, trust: str = "principal", mention: bool = True,
         role: Optional[str] = None, system_note: Optional[str] = None,
         current_time: Optional[str] = None) -> str:
    """One turn's event: the triggering message only, never history."""
    message = message_tag(trigger, trigger=True, trust=trust, mention=mention, role=role)
    body = f"<thread{_attrs([('ts', thread_ts)])}>\n{_indent(message, 2)}\n</thread>" if thread_ts else message
    project = f"<project{_attrs([('id', chat_id), ('type', chat_kind)])}>\n{_indent(body, 2)}\n</project>"
    note = f"\n<system-note>{escape_text(system_note)}</system-note>" if system_note else ""
    head = f"<wake{_attrs([('reason', reason), ('current-time', current_time or now_ms_iso())])}>"
    return f"{head}\n{_indent(project, 2)}{note}\n</wake>"


def _cited(row: dict, chat_id: str) -> str:
    attrs = _attrs([
        ("id", row.get("id")),
        ("author", "user" if row.get("author_kind") == "owner" else "coordinator"),
        ("name", row.get("author_name")),
        ("at", row.get("written_at") or row.get("ts")),
        ("where", row.get("where") or "timeline"),
        ("chat", chat_id),
    ])
    return f"<cited{attrs}>\n{_indent(escape_text(row.get('text', '')), 2)}\n</cited>"


_RELAY_SOURCES = {
    "workspace-coordinator": "the workspace coordinator session, a Claude session working across every project",
    "coordinator": "this project's coordinator session, a Claude session",
}


def relay(*, sender: str, sender_session: str, cited: list[tuple[dict, str]],
          note: Optional[str] = None, reason: Optional[str] = None,
          current_time: Optional[str] = None) -> str:
    """A coordinator handing work on: server-copied ``<cited>`` rows plus its own note.

    ``sender`` is ``"workspace-coordinator"`` (to a project coordinator) or
    ``"coordinator"`` (a project coordinator to a thread). ``cited`` pairs a
    stored row with the id of the chat it was read from.
    """
    who = _RELAY_SOURCES.get(sender, "a Claude session")
    lines = []
    if cited:
        lines.append("The messages below are picked by the sender and not necessarily consecutive. "
                     "User messages are copied verbatim by the server.")
        lines.extend(_cited(row, chat_id) for row, chat_id in cited)
    if note:
        lines.append(f"The note below was written by {who}, not by your user.")
        lines.append(f"<note>\n{_indent(escape_text(note), 2)}\n</note>")
    head = f"<relay{_attrs([('from', sender), ('session', sender_session), ('current-time', current_time or now_ms_iso()), ('reason', reason)])}>"
    return f"{head}\n{_indent(chr(10).join(lines), 2)}\n</relay>"


def handoff(*, predecessor: Optional[str], chat: dict, instructions: Optional[str],
            memory_index: Optional[str], threads: list[dict], timeline: list[dict],
            pending: Optional[list[str]] = None, current_time: Optional[str] = None) -> str:
    """What a fresh coordinator incarnation starts from (design §5, M4)."""
    parts = [f"<chat{_attrs([('id', chat.get('id')), ('kind', chat.get('kind')), ('project', chat.get('project_id'))])}/>"]
    if instructions:
        parts.append(f"<instructions>\n{_indent(escape_text(instructions), 2)}\n</instructions>")
    if memory_index:
        parts.append(f"<memory index=\"MEMORY.md\">\n{_indent(escape_text(memory_index), 2)}\n</memory>")
    thread_lines = []
    for t in threads:
        card = t.get("card") or {}
        thread_lines.append(f"<thread{_attrs([('ts', t.get('ts')), ('title', t.get('title')), ('state', t.get('state')), ('headline', card.get('headline')), ('needs-you', card.get('needs_you') or None), ('last-activity', t.get('last_activity_at'))])}/>")
    parts.append("<threads>\n" + _indent("\n".join(thread_lines), 2) + "\n</threads>" if thread_lines else "<threads/>")
    rows = "\n".join(message_tag(r) for r in timeline)
    parts.append(f"<timeline count=\"{len(timeline)}\">\n{_indent(rows, 2)}\n</timeline>" if rows else "<timeline count=\"0\"/>")
    if pending:
        parts.append("<pending>\n" + _indent("\n".join(f"- {escape_text(p)}" for p in pending), 2) + "\n</pending>")
    head = f"<handoff{_attrs([('generated-at', current_time or now_ms_iso()), ('predecessor', predecessor)])}>"
    return f"{head}\n{_indent(chr(10).join(parts), 2)}\n</handoff>"
