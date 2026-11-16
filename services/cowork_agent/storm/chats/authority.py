"""Telling the founder when an agent claimed their authority and XO refused
it (the plan's W8): a citation of a founder message that isn't one, or XO's
own message tags in an agent's output (``storm/authority``). Each raises one
``integrity`` item for the founder, once per turn and reason; no authority
was granted either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from services.cowork_agent.storm.review.items import ItemSpec

if TYPE_CHECKING:
    from services.cowork_agent.storm.chats import Chats
    from services.cowork_agent.storm.engine import Writes
    from services.cowork_agent.storm.store import StoredEvent

#: The event the engine appends when a turn's output opened an XO tag.
FORGED = "authority.forged"
#: Why an item was raised.
INVALID_CITATION, FORGED_ENVELOPE = "invalid_founder_citation", "forged_xo_envelope"
#: What each item says, by its reason.
_SAID = {INVALID_CITATION: "XO refused a citation of a message that isn't there or is another chat's{where}{why}. "
                           "No founder authority was granted.",
         FORGED_ENVELOPE: "XO stopped a turn whose output imitated an XO envelope (a tag only XO writes). No "
                          "founder authority was granted."}


def flag(chats: Chats, w: Writes, *, session_id: str, chat_id: str | None, thread_ts: str | None, epoch: int,
         turn_id: str | None, reason: str, tool: str | None = None, cited: str | None = None,
         why: str | None = None) -> None:
    """Open the founder's item for this turn and ``reason``, unless it is
    already there (open or decided). A refused citation names the tool, the
    id it cited and why."""
    subject = f"authority:{session_id}:{turn_id or epoch}:{reason}"
    if w.execute("SELECT 1 FROM review_items WHERE subject = ? AND state IN ('open', 'decided')",
                 (subject,)).fetchone():
        return
    chats.queue.open_item(w, ItemSpec(
        kind="integrity", origin="xo", title="Agent authority check failed", session_id=session_id, chat_id=chat_id,
        thread_ts=thread_ts, turn_id=turn_id, epoch=epoch, subject=subject,
        detail={"reason": reason, "text": _SAID[reason].format(
                    where=f" ({cited!r} in {tool})" if cited and tool else "", why=f": {why.rstrip('.')}" if why else ""),
                **{k: v for k, v in (("tool", tool), ("message_id", cited), ("why", why)) if v}}))


class Reporter:
    """An engine listener: each ``authority.forged`` event becomes the
    founder's item."""

    def __init__(self, chats: Chats) -> None:
        self.chats = chats

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.type == FORGED:
                self.chats.spawn(self._record(event), f"authority:{event.turn_id}")

    def ephemeral(self, event: Any, epoch: int) -> None:
        pass

    async def _record(self, event: StoredEvent) -> None:
        await self.chats.engine.write(lambda w: flag(
            self.chats, w, session_id=event.session_id, chat_id=event.chat_id, thread_ts=event.thread_ts,
            epoch=event.epoch, turn_id=event.turn_id, reason=FORGED_ENVELOPE))

    async def recover(self) -> None:
        """At boot: an event whose item a crash kept from being opened gets
        it now (``flag`` opens each once)."""
        def reopen(w: Writes) -> None:
            for session_id, chat_id, thread_ts, epoch, turn_id in w.execute(
                    "SELECT session_id, chat_id, thread_ts, epoch, turn_id FROM events WHERE type = ?",
                    (FORGED,)).fetchall():
                flag(self.chats, w, session_id=session_id, chat_id=chat_id, thread_ts=thread_ts, epoch=epoch,
                     turn_id=turn_id, reason=FORGED_ENVELOPE)
        await self.chats.engine.write(reopen)
