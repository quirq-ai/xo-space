"""What an agent is handed as a turn's input: ``<wake>``, ``<relay>`` and
the ``review-answered`` wake (design ch 11 §4; the PoC's
envelopes, D31–D34).

Only this module builds envelopes, and only from stored rows: a message's
text and author are copied from the store, never from what an agent wrote,
so who said what is decided by the server. Every ``<`` ``>`` ``&`` in copied
text is escaped, and so are the Unicode look-alikes of the angle brackets,
so text can never open or close a tag; attribute values also escape ``"``
and newlines. Agent words never count as the owner's consent: a relay's note
says who wrote it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

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


def message_tag(row: Mapping[str, Any], *, trigger: bool = False, trust: str | None = None, mention: bool = False,
                role: str | None = None) -> str:
    """One ``<message>`` from a stored row."""
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
    return f"<message{attrs}>{escape_text(row.get('text', ''))}</message>"


def _place(chat: Mapping[str, Any], body: str, thread_ts: str | None) -> str:
    if thread_ts:
        body = f"<thread{_attrs([('ts', thread_ts)])}>\n{_indent(body)}\n</thread>"
    return f"<project{_attrs([('id', chat['id']), ('type', chat['kind']), ('name', chat.get('project'))])}>\n" \
           f"{_indent(body)}\n</project>"


def wake(*, reason: str, chat: Mapping[str, Any], trigger: Mapping[str, Any], now: str, thread_ts: str | None = None,
         trust: str = "principal", mention: bool = True, role: str | None = None,
         system_note: str | None = None) -> str:
    """One turn's event: the triggering message only, never history."""
    message = message_tag(trigger, trigger=True, trust=trust, mention=mention, role=role)
    note = f"\n<system-note>{escape_text(system_note)}</system-note>" if system_note else ""
    head = f"<wake{_attrs([('reason', reason), ('current-time', now)])}>"
    return f"{head}\n{_indent(_place(chat, message, thread_ts))}{note}\n</wake>"


def _cited(row: Mapping[str, Any]) -> str:
    attrs = _attrs([
        ("id", row.get("id")),
        ("author", "user" if row.get("author_kind") == "owner" else _FROM.get(row.get("author_kind"), "system")),
        ("at", row.get("created_at")),
        ("where", "thread" if row.get("thread_ts") else "timeline"),
        ("chat", row.get("chat_id")),
    ])
    return f"<cited{attrs}>\n{_indent(escape_text(row.get('text', '')))}\n</cited>"


def relay(*, sender_role: str, sender_session: str | None, cited: Sequence[Mapping[str, Any]] = (),
          note: str | None = None, reason: str | None = None, now: str, hop: str | None = None,
          system_note: str | None = None) -> str:
    """An agent handing work on: server-copied ``<cited>`` rows plus its own
    note, labelled as not the user's words, and the server's own note on how
    to answer."""
    who = _RELAY_SOURCES.get(sender_role, "an agent session")
    lines = []
    if cited:
        lines.append("The messages below are picked by the sender and not necessarily consecutive. "
                     "User messages are copied verbatim by the server.")
        lines.extend(_cited(row) for row in cited)
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
