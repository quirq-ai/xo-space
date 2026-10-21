"""The conversation's part of a review item (design ch 15 §5.2, ch 11 §10,
§16): the review queue's ``Conversation``. Each method runs inside the
queue's transaction, on the store's thread: plain SQL, no awaiting.

* **opened**: an item a thread's agent raised (a held permission, its
  question, an ``ask_owner``) puts a blocking item's thread in
  ``needs_approval``, and writes an ``ask`` hop to the project coordinator
  with a ``next`` wake, so it is told at once (each ask its own wake,
  §0.53). It may add context or tell the person; it never decides (I4).
  Items XO raises itself (usage, a suggestion) wake no one, except an
  integrity report on a thread (ch 16 §2.4): XO tells its coordinator the
  same way, with an ``ask`` hop from ``xo``; nothing blocks.
* **closed**: decided, expired or withdrawn; the thread leaves
  ``needs_approval`` once nothing else blocks it.
* **paused**: the person confirmed pausing a thread after repeated denials;
  its coordinator gets a relay so it can start a fresh one (ch 15 §9).
* **answered**: the ``review-answered`` wake the asking session gets, built
  from the stored decision (ch 11 §4).
* **decided** (after the commit): a project the person allowed is created
  (``projects.create``, ch 11 §9).
"""

from __future__ import annotations

from typing import Any, Mapping

from services.cowork_agent.storm.chats import envelope, rows
from services.cowork_agent.storm.chats import live_coordinator
from services.cowork_agent.storm.engine import Wake, Writes

#: Who raised an item: what an agent asked, as opposed to what XO itself raised.
AGENT_ORIGINS = ("cli", "agent")
#: What XO raises itself that a thread's coordinator is told of all the same.
TOLD_XO_KINDS = ("integrity",)
#: Thread states an open blocking item doesn't override: the person's own.
KEPT_STATES = ("resolved", "paused")


#: With the owner's own messages in a thread, cited to its coordinator.
OWNER_IN_THREAD = ("The owner wrote the messages above in the thread itself, to its agent: they are the owner's own "
                   "instructions for that thread, and what the thread does follows them. Never contradict them.")


class Conversation:
    def __init__(self) -> None:
        self.chats: Any = None                  # set when the conversation starts (``chats.boot``)

    async def decided(self, item: Mapping[str, Any]) -> None:
        """After a decision commits: a project the person allowed is made (ch 11 §9)."""
        if item["kind"] == "create_project" and (item["decision"] or {}).get("decision") == "allow" \
                and self.chats is not None:
            from services.cowork_agent.storm.chats import projects
            await projects.create(self.chats, item)

    def opened(self, w: Writes, item: Mapping[str, Any]) -> None:
        thread = _thread_of(w, item)
        if thread is not None and item["origin"] not in AGENT_ORIGINS and item["kind"] in TOLD_XO_KINDS:
            self._told_by_xo(w, item, thread)
        if thread is None or item["origin"] not in AGENT_ORIGINS:
            return
        if item["blocking"] and thread["state"] not in KEPT_STATES:
            rows.update_thread(w, item["chat_id"], thread["ts"], state="needs_approval")
        chat = rows.get_chat(w, item["chat_id"])
        coordinator = live_coordinator(w, item["chat_id"])
        wake_id = f"ask:{item['id']}"
        asked = _owner_asked(w, item["session_id"])
        rows.add_hop(w, chat_id=item["chat_id"], thread_ts=thread["ts"], kind="ask", from_role="thread",
                     from_session=item["session_id"], to_role="project-coordinator", to_session=coordinator,
                     via=["thread"], cause=item["id"], message_ids=[m["id"] for m in asked],
                     wake_id=wake_id if coordinator else None)
        if coordinator is None:
            return                  # no coordinator yet: the item still reaches the person
        told = {"id": item["id"], "author_kind": "system", "author_id": "xo", "created_at": w.now,
                "text": f"Thread “{thread['title']}” asked the owner ({item['kind']}"
                        f"{', blocking' if item['blocking'] else ''}): {item['title']} — review item {item['id']}. "
                        "Add context with annotate_review or tell the owner here; never decide it."}
        text = envelope.wake(reason="ask", chat=chat, trigger=told, now=w.now, thread_ts=thread["ts"], trust="peer",
                             mention=False)
        if asked:                   # what the owner asked the thread directly, which this ask follows (live run)
            text += "\n\n" + envelope.relay(sender_role="thread", sender_session=item["session_id"], cited=asked,
                                             reason="ask", now=w.now, system_note=OWNER_IN_THREAD)
        w.wake(coordinator, Wake(id=wake_id, priority="next", reason="ask", text=text))

    @staticmethod
    def _told_by_xo(w: Writes, item: Mapping[str, Any], thread: Mapping[str, Any]) -> None:
        coordinator = live_coordinator(w, item["chat_id"])
        wake_id = f"ask:{item['id']}"
        hop = rows.add_hop(w, chat_id=item["chat_id"], thread_ts=thread["ts"], kind="ask", from_role="xo",
                           from_session=None, to_role="project-coordinator", to_session=coordinator, cause=item["id"],
                           wake_id=wake_id if coordinator else None)
        if coordinator is None:
            return                  # no coordinator yet: the item still reaches the person
        note = (f"XO's integrity check raised review item {item['id']} on thread “{thread['title']}” "
                f"({thread['ts']}): {item['title']}. It is a report for the owner, who marks it seen; the thread's "
                "turn ran as usual. Tell the owner here if it matters; never decide it.")
        w.wake(coordinator, Wake(id=wake_id, priority="next", reason="ask", text=envelope.relay(
            sender_role="xo", sender_session=None, note=note, reason="ask", now=w.now, hop=hop["id"])))

    def closed(self, w: Writes, item: Mapping[str, Any]) -> None:
        thread = _thread_of(w, item)
        if thread is None or thread["state"] != "needs_approval":
            return
        blocking = w.execute("SELECT 1 FROM review_items WHERE chat_id = ? AND thread_ts = ? AND state = 'open' "
                             "AND blocking = 1 AND id <> ? LIMIT 1", (item["chat_id"], thread["ts"], item["id"])).fetchone()
        if blocking is None:
            rows.update_thread(w, item["chat_id"], thread["ts"], state="working")

    def paused(self, w: Writes, item: Mapping[str, Any]) -> None:
        thread = _thread_of(w, item)
        coordinator = live_coordinator(w, item["chat_id"]) if thread else None
        if thread is None or coordinator is None:
            return
        hop_id = rows.new_id("hop")
        note = (f"The owner paused thread “{thread['title']}” ({thread['ts']}) after repeated denials. If its work "
                "should go on, start a fresh thread for it.")
        rows.add_hop(w, hop_id=hop_id, chat_id=item["chat_id"], thread_ts=thread["ts"], kind="relay", from_role="xo",
                     from_session=None, to_role="project-coordinator", to_session=coordinator, cause=item["id"],
                     note=note, wake_id=f"deleg:{hop_id}")
        w.wake(coordinator, Wake(id=f"deleg:{hop_id}", priority="next", reason="relay", text=envelope.relay(
            sender_role="xo", sender_session=None, note=note, reason="paused", now=w.now, hop=hop_id)))

    def answered(self, w: Writes, item: Mapping[str, Any], text: str, option: str | None) -> str:
        chat = rows.get_chat(w, item["chat_id"]) if item["chat_id"] else None
        return envelope.review_answered(chat=chat, thread_ts=item["thread_ts"], item=item, answer=text, option=option,
                                        now=w.now)


def _owner_asked(w: Writes, session_id: str | None) -> list[dict[str, Any]]:
    """The owner's own messages that the session's running input took (its
    ``msg:`` wakes): what the owner asked the thread directly."""
    if session_id is None:
        return []
    ids = [wid.removeprefix("msg:") for (wid,) in w.execute(
        "SELECT k.id FROM wakes k JOIN turns t ON t.id = k.turn_id WHERE k.session_id = ? AND t.status = 'running' "
        "AND k.id LIKE 'msg:%' ORDER BY k.rowid", (session_id,)).fetchall()]
    return [m for m in (rows.get_message(w, mid) for mid in ids) if m and m["author_kind"] == "owner"]


def _thread_of(w: Writes, item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not item.get("chat_id") or not item.get("thread_ts"):
        return None
    return rows.get_thread(w, item["chat_id"], item["thread_ts"])
