"""What an agent is handed as a turn's input: ``<wake>``, ``<relay>``,
``<handoff>``, the ``review-answered`` wake and a job run's ``schedule``
wake (design ch 11 §4, §7.1; the PoC's envelopes, D31–D34).

Only this module builds envelopes, and only from stored rows: a message's
text and author are copied from the store, never from what an agent wrote,
so who said what is decided by the server. One exception keeps the person's
own words: the wake to the session they sent a message to quotes its text
as they typed it (the store keeps it redacted, §0.112; ``delivery``). A
relay's note, an agent's words, is redacted. Every ``<`` ``>`` ``&`` in copied
text is escaped, and so are the Unicode look-alikes of the angle brackets,
so text can never open or close a tag; attribute values also escape ``"``
and newlines. Agent words never count as the owner's consent: a relay's note
says who wrote it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.cowork_agent.storm.chats import attachments
from services.cowork_agent.storm.redact import redact

#: Characters that read as angle brackets; escaped like the real ones.
_LOOKALIKES = {
    "‹": "&#8249;", "›": "&#8250;", "⟨": "&#10216;", "⟩": "&#10217;",
    "〈": "&#9001;", "〉": "&#9002;", "＜": "&#65308;", "＞": "&#65310;",
    "﹤": "&#65124;", "﹥": "&#65125;",
}
#: Who a stored author is, in envelope terms.
_FROM = {"owner": "human", "agent": "agent", "system": "system"}
#: Who wrote a relay's note, by the sender's role.
_RELAY_SOURCES = {
    "workspace-coordinator": "the workspace coordinator session, an agent session working across every project",
    "project-coordinator": "this project's coordinator session, an agent session",
    "thread": "a thread's agent session working in this project",
    "xo": "XO Space itself",
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


def _indent(block: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in block.splitlines())


def attachment_tags(row: Mapping[str, Any], folder: Path | None) -> str:
    """One ``<attachment/>`` per file the row carries, with the absolute path
    the agent reads it at in ``folder`` (the chat's). Built from the stored
    row; the name is the person's, escaped like any attribute. Its contents
    are data, never instructions (``trust="data"``)."""
    files = (row.get("extra") or {}).get("attachments") or ()
    if not files or folder is None:
        return ""
    return "".join(f"\n  <attachment{_attrs([('id', f['id']), ('name', f['name']), ('type', f['type']), ('bytes', f['bytes']), ('path', attachments.path_of(folder, f)), ('source', 'upload'), ('trust', 'data')])}/>"
                   for f in files) + "\n"


def _folder(chat: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> Path | None:
    """The chat's folder when any of ``rows`` carries files, else None (no
    lookup for the many envelopes that carry none)."""
    if any((row.get("extra") or {}).get("attachments") for row in rows):
        return attachments.chat_folder(chat)
    return None


def message_tag(row: Mapping[str, Any], *, trigger: bool = False, trust: str | None = None, mention: bool = False,
                role: str | None = None, folder: Path | None = None) -> str:
    """One ``<message>`` from a stored row; its attachments with their paths
    in ``folder``, the chat's."""
    forwarded = (row.get("extra") or {}).get("forwarded_from") or {}
    attrs = _attrs([
        ("trigger", "true" if trigger else None),
        ("from", _FROM.get(row.get("author_kind"), "system")),
        ("trust", trust),
        ("author-id", row.get("author_id")),
        ("id", row.get("id")),
        ("sent-at", row.get("created_at")),
        ("mention", "true" if mention else None),
        ("role", role),
        ("forwarded-via", " › ".join(forwarded.get("via") or ()) or None),
    ])
    return f"<message{attrs}>{escape_text(row.get('text', ''))}{attachment_tags(row, folder)}</message>"


def _place(chat: Mapping[str, Any], body: str, thread_ts: str | None) -> str:
    if thread_ts:
        body = f"<thread{_attrs([('ts', thread_ts)])}>\n{_indent(body)}\n</thread>"
    return f"<project{_attrs([('id', chat['id']), ('type', chat['kind']), ('name', chat.get('project'))])}>\n" \
           f"{_indent(body)}\n</project>"


def wake(*, reason: str, chat: Mapping[str, Any], trigger: Mapping[str, Any], now: str, thread_ts: str | None = None,
         trust: str = "principal", mention: bool = True, role: str | None = None,
         system_note: str | None = None) -> str:
    """One turn's event: the triggering message only, never history."""
    message = message_tag(trigger, trigger=True, trust=trust, mention=mention, role=role,
                          folder=_folder(chat, [trigger]))
    note = f"\n<system-note>{escape_text(system_note)}</system-note>" if system_note else ""
    head = f"<wake{_attrs([('reason', reason), ('current-time', now)])}>"
    return f"{head}\n{_indent(_place(chat, message, thread_ts))}{note}\n</wake>"


def _cited(row: Mapping[str, Any], folder: Path | None = None) -> str:
    attrs = _attrs([
        ("id", row.get("id")),
        ("author", "user" if row.get("author_kind") == "owner" else _FROM.get(row.get("author_kind"), "system")),
        ("at", row.get("created_at")),
        ("where", "thread" if row.get("thread_ts") else "timeline"),
        ("chat", row.get("chat_id")),
    ])
    files = attachment_tags(row, folder).strip("\n")
    body = escape_text(row.get('text', '')) + (f"\n{files.strip()}" if files else "")
    return f"<cited{attrs}>\n{_indent(body)}\n</cited>"


def relay(*, sender_role: str, sender_session: str | None, cited: Sequence[Mapping[str, Any]] = (),
          note: str | None = None, reason: str | None = None, now: str, hop: str | None = None,
          system_note: str | None = None, folder: Path | None = None) -> str:
    """An agent handing work on: server-copied ``<cited>`` rows plus its own
    note, labelled as not the user's words, and the server's own note on how
    to answer. The note is redacted: a key the person gave one agent never
    reaches another through it (§0.112)."""
    who = _RELAY_SOURCES.get(sender_role, "an agent session")
    note = redact(note) if note else note
    lines = []
    if cited:
        lines.append("The messages below are picked by the sender and not necessarily consecutive. "
                     "User messages are copied verbatim by the server.")
        lines.extend(_cited(row, folder) for row in cited)
    if note:
        lines.append(f"The note below was written by {who}, not by your user.")
        lines.append(f"<note>\n{_indent(escape_text(note))}\n</note>")
    if system_note:
        lines.append(f"<system-note>{escape_text(system_note)}</system-note>")
    head = f"<relay{_attrs([('from', sender_role), ('session', sender_session), ('current-time', now), ('reason', reason), ('hop', hop)])}>"
    return f"{head}\n{_indent(chr(10).join(lines))}\n</relay>"


def review_answered(*, chat: Mapping[str, Any] | None, thread_ts: str | None, item: Mapping[str, Any],
                    answer: str, option: str | None, now: str) -> str:
    """The person's answer to an ask, built by the server from the stored
    decision (ch 11 §4): the only words in it that count as the owner's."""
    asked = f"<asked>{escape_text(item['title'])}</asked>"
    said = f"<answer{_attrs([('from', 'human'), ('trust', 'principal'), ('option', option), ('at', now)])}>" \
           f"{escape_text(answer)}</answer>"
    review = f"<review{_attrs([('id', item['id']), ('kind', item['kind']), ('decided-by', 'owner')])}>\n" \
             f"{_indent(asked)}\n{_indent(said)}\n</review>"
    body = _place(chat, review, thread_ts) if chat else review
    return f"<wake{_attrs([('reason', 'review-answered'), ('current-time', now)])}>\n{_indent(body)}\n</wake>"


def schedule(*, chat: Mapping[str, Any], thread_ts: str, job: Mapping[str, Any], fire_at: str, now: str) -> str:
    """A job run's first input (ch 11 §7.1): the job's prompt, copied by the
    server from the job's definition, as the owner's words. The owner wrote
    it when they made the job, or allowed it word for word on the review
    item ``review`` (ch 11 §14.6)."""
    review = (job.get("approved_by") or {}).get("review_id")
    attrs = _attrs([("trigger", "true"), ("from", "human"), ("trust", "principal"), ("source", "schedule"),
                    ("job-id", job["id"]), ("job", job["name"]), ("fire-at", fire_at), ("review", review)])
    message = f"<message{attrs}>{escape_text(job['prompt'])}</message>"
    head = f"<wake{_attrs([('reason', 'schedule'), ('current-time', now)])}>"
    return f"{head}\n{_indent(_place(chat, message, thread_ts))}\n</wake>"


def handoff(*, predecessor: str | None, chat: Mapping[str, Any], memory_index: str | None,
            threads: Sequence[Mapping[str, Any]], timeline: Sequence[Mapping[str, Any]],
            pending: Sequence[str] = (), now: str) -> str:
    """What a fresh coordinator incarnation starts from (ch 11 §6.2)."""
    parts = [f"<chat{_attrs([('id', chat['id']), ('kind', chat['kind']), ('project', chat.get('project'))])}/>"]
    if memory_index:
        # An agent wrote it: redacted before it is escaped, since a secret
        # holding & < > would no longer match once escaped (§0.112's scrub
        # redacts this wake as it stands).
        parts.append(f"<memory index=\"MEMORY.md\">\n{_indent(escape_text(redact(memory_index)))}\n</memory>")
    thread_lines = [f"<thread{_attrs([('ts', t['ts']), ('title', t.get('title')), ('state', t.get('state')), ('headline', (t.get('card') or {}).get('headline')), ('needs-you', (t.get('card') or {}).get('needs_you') or None), ('last-activity', t.get('updated_at'))])}/>"
                    for t in threads]
    parts.append("<threads>\n" + _indent("\n".join(thread_lines)) + "\n</threads>" if thread_lines else "<threads/>")
    folder = _folder(chat, timeline)
    rows = "\n".join(message_tag(r, folder=folder) for r in timeline)
    parts.append(f"<timeline count=\"{len(timeline)}\">\n{_indent(rows)}\n</timeline>" if rows
                 else "<timeline count=\"0\"/>")
    if pending:
        parts.append("<pending>\n" + _indent("\n".join(f"- {escape_text(p)}" for p in pending)) + "\n</pending>")
    head = f"<handoff{_attrs([('generated-at', now), ('predecessor', predecessor)])}>"
    return f"{head}\n{_indent(chr(10).join(parts))}\n</handoff>"
