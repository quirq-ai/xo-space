"""What a project coordinator no longer hears through wakes, read at the
top of its next real turn (w5-w7-design W5, "Wakes that go away").

A thread's ended input, its report and its result, and the items it opens
for the owner wake no coordinator now: a question wakes someone, a report
doesn't. ``SinceLastTurn`` is the engine's ``TurnNote`` that gives the
coordinator all of it at its next turn, whatever starts it: a
``<since-last-turn>`` block, built from the store, of what happened in its
chat's threads since the chat's coordinator last took a turn (any of its
incarnations, so a successor hears what its predecessor never did): each
task's new latest report, each result submitted, each item a thread opened
that is still open, and the owner's own words in a thread. Nothing new
gives no block. The bound is to the second, so something at its very edge
may be told twice, never missed; ``read_state`` has the whole picture.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from services.cowork_agent.storm.chats.envelope import escape_attr, escape_text
from services.cowork_agent.storm.engine.ports import iso

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.adapters.contract import SessionSpec
    from services.cowork_agent.storm.engine import Engine
    from services.cowork_agent.storm.engine.session import Meta

#: The roles told: a project coordinator is the one whose threads report to it.
TOLD_ROLES = ("project-coordinator",)
#: How many lines a block holds, the latest kept; the rest are counted.
LISTED = 30
INTRO = ("What this project's threads did since your last turn, from XO's records. None of it woke you; read_state "
         "has the whole picture.")
OWNER_IN_THREAD = ("The owner's messages in a thread are their own instructions for that thread: never contradict "
                   "them.")


class SinceLastTurn:
    """``ports.TurnNote``: what a coordinator's threads did since its chat's
    coordinator last took a turn. The bound is read from the store, so
    nothing is kept between turns."""

    async def note(self, engine: Engine, meta: Meta, spec: SessionSpec | None, since: str) -> str | None:
        if meta.role not in TOLD_ROLES or not meta.chat_id:
            return None
        lines = await engine.store.read(lambda conn: _lines(conn, meta.chat_id, meta.role, since))
        if not lines:
            return None
        kept = lines[-LISTED:]
        earlier = [f"{len(lines) - len(kept)} earlier, in read_state."] if len(lines) > len(kept) else []
        owner = [OWNER_IN_THREAD] if any(kind == "owner" for _at, kind, _text in kept) else []
        body = "\n".join(escape_text(line) for line in
                         (INTRO, *earlier, *(f"- {text}" for _at, _kind, text in kept), *owner))
        return (f'<since-last-turn current-time="{escape_attr(iso(engine.clock.time()))}">\n{body}\n'
                "</since-last-turn>")

    def delivered(self, meta: Meta) -> None:
        """Nothing to move: the next bound is the turn this input starts."""


def _lines(conn: sqlite3.Connection, chat_id: str, role: str, since: str) -> list[tuple[str, str, str]]:
    """``(at, kind, line)`` for each thing since the chat's last coordinator
    turn (``since``, the session's own bound, when there is none), oldest
    first."""
    last = conn.execute("SELECT max(t.started_at) FROM turns t JOIN sessions s ON s.id = t.session_id "
                        "WHERE s.chat_id = ? AND s.role = ?", (chat_id, role)).fetchone()[0]
    bound = last or since
    found: list[tuple[str, str, str]] = []
    for ts, title, report, at in conn.execute(
            "SELECT t.thread_ts, th.title, t.last_report, t.reported_at FROM tasks t JOIN threads th "
            "ON th.chat_id = t.chat_id AND th.ts = t.thread_ts WHERE t.chat_id = ? AND t.reported_at >= ?",
            (chat_id, bound)):
        found.append((at, "report", f"Thread {_named(title, ts)} reported at {at}: {report}"))
    for ts, title, summary, artifacts, at, state in conn.execute(
            "SELECT t.thread_ts, th.title, r.summary, r.artifacts, r.created_at, t.state FROM results r "
            "JOIN tasks t ON t.id = r.task_id JOIN threads th ON th.chat_id = t.chat_id AND th.ts = t.thread_ts "
            "WHERE t.chat_id = ? AND r.created_at >= ?", (chat_id, bound)):
        count = len(json.loads(artifacts))
        found.append((at, "result", f"Thread {_named(title, ts)} submitted a result at {at} ({count} artifact"
                                    f"{'' if count == 1 else 's'}); its task is {state}: {summary}"))
    for item, ts, title, kind, blocking, asked, at in conn.execute(
            "SELECT r.id, r.thread_ts, th.title, r.kind, r.blocking, r.title, r.created_at FROM review_items r "
            "JOIN threads th ON th.chat_id = r.chat_id AND th.ts = r.thread_ts WHERE r.chat_id = ? "
            "AND r.state = 'open' AND r.origin IN ('cli', 'agent') AND r.created_at >= ?", (chat_id, bound)):
        how = f"{kind}, blocking" if blocking else kind
        found.append((at, "item", f"Thread {_named(title, ts)} asked the owner ({how}): {asked}. Review item {item}: "
                                  "add context with annotate_review or tell the owner here; never decide it."))
    for ts, title, text, at in conn.execute(
            "SELECT m.thread_ts, th.title, m.text, m.created_at FROM messages m JOIN threads th "
            "ON th.chat_id = m.chat_id AND th.ts = m.thread_ts WHERE m.chat_id = ? AND m.author_kind = 'owner' "
            "AND m.created_at >= ?", (chat_id, bound)):
        found.append((at, "owner", f"The owner wrote in thread {_named(title, ts)} at {at}: {text}"))
    return sorted(found, key=lambda entry: entry[0])


def _named(title: Any, ts: str) -> str:
    return f"“{title or ts}” ({ts})"
