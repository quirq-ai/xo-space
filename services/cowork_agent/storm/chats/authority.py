"""Owner-visible evidence of refused citations and imitated XO envelopes."""

from services.cowork_agent.storm.review.items import ItemSpec


def flag(chats, w, *, session_id, chat_id, thread_ts, epoch, turn_id, reason):
    subject = f"authority:{session_id}:{turn_id or epoch}:{reason}"
    if w.execute("SELECT 1 FROM review_items WHERE subject = ? AND state IN ('open', 'decided')",
                 (subject,)).fetchone():
        return
    chats.queue.open_item(w, ItemSpec(
        kind="integrity", origin="xo", title="Agent authority check failed",
        session_id=session_id, chat_id=chat_id, thread_ts=thread_ts, turn_id=turn_id,
        epoch=epoch, subject=subject,
        detail={"reason": reason, "text": "XO refused an invalid founder citation or stopped output that "
                "imitated an XO envelope. No founder authority was granted."}))


class Reporter:
    def __init__(self, chats):
        self.chats = chats

    def committed(self, events):
        for event in events:
            if event.type == "authority.forged":
                self.chats.spawn(self.record(event), f"authority:{event.turn_id}")

    def ephemeral(self, event, epoch):
        pass

    async def record(self, event):
        await self.chats.engine.write(lambda w: flag(
            self.chats, w, session_id=event.session_id, chat_id=event.chat_id,
            thread_ts=event.thread_ts, epoch=event.epoch, turn_id=event.turn_id,
            reason="forged_xo_envelope"))

    async def recover(self):
        """A crash between evidence and its inbox item must not hide a forgery."""
        def reopen(w):
            for session, chat, thread, epoch, turn in w.execute(
                    "SELECT session_id, chat_id, thread_ts, epoch, turn_id FROM events "
                    "WHERE type = 'authority.forged'").fetchall():
                flag(self.chats, w, session_id=session, chat_id=chat, thread_ts=thread,
                     epoch=epoch, turn_id=turn, reason="forged_xo_envelope")
        await self.chats.engine.write(reopen)
