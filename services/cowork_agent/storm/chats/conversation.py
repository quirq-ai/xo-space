"""The conversation's part of a review item (design ch 15 §5.2, ch 11 §10,
§16): the review queue's ``Conversation``. Each method runs inside the
queue's transaction, on the store's thread: plain SQL, no awaiting.

* **opened**: an item a thread's agent raised (a held permission, its
  question, an ``ask`` to the founder) puts a blocking item's thread in
  ``needs_approval``. It wakes no one (w5-w7-design W5): the founder
  answers it, and the project coordinator sees it in ``read_state`` and at
  its next turn (``digest.py``), where it may add context or tell the
  person; it never decides (I4). Items XO raises itself (usage, a
  suggestion) wake no one either, except an integrity report on a thread
  (ch 16 §2.4): XO tells its coordinator with an ``ask`` hop from ``xo``
  and a ``next`` wake; nothing blocks.
* **closed**: decided, expired or withdrawn; the thread leaves
  ``needs_approval`` once nothing else blocks it.
* **paused**: the person confirmed pausing a thread after repeated denials;
  its coordinator gets a relay so it can start a fresh one (ch 15 §9).
* **answered**: the ``review-answered`` wake the asking session gets, built
  from the stored decision (ch 11 §4).
* **decided** (after the commit): a project the person allowed is created
  (``projects.create``, ch 11 §9); a job the person allowed is registered
  as a timer (``storm/jobs``, ch 12 §5.8); an agent the person allowed is
  saved through the project-files code, guarded by the version it was
  proposed against (an ``approval`` holding ``detail.create_persona``,
  ``comms.personas.save_allowed``).
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


class Conversation:
    def __init__(self) -> None:
        self.chats: Any = None                  # set when the conversation starts (``chats.boot``)

    async def decided(self, item: Mapping[str, Any]) -> None:
        """After a decision commits: a project the person allowed is made (ch 11 §9), a job registered (if
        this fails, storm's next boot registers it), a runtime uninstall the person approved started."""
        if self.chats is None:
            return
        if item["kind"] == "usage" and (item["decision"] or {}).get("decision") == "move":
            from services.cowork_agent.storm.chats import limits
            # Each move stops a thread and waits for it: never inside the decision's request.
            self.chats.spawn(limits.LimitCards(self.chats).moved(item), f"limit-move:{item['id']}")
            return
        decision = (item["decision"] or {}).get("decision")
        if item["kind"] == "approval" and decision == "approve" and "runtime_action" in (item["detail"] or {}):
            self.chats.spawn(self._runtime_action(item), f"runtime-action:{item['id']}")
            return
        if item["kind"] == "approval" and decision == "approve" and "create_persona" in (item["detail"] or {}):
            from services.cowork_agent.storm.comms import personas
            await personas.save_allowed(self.chats, item)
            return
        if decision != "allow":
            return
        if item["kind"] == "create_project":
            from services.cowork_agent.storm.chats import projects
            await projects.create(self.chats, item)
        elif item["kind"] == "create_job":
            from services.cowork_agent.storm import jobs
            await jobs.current().register(item)


    async def _runtime_action(self, item: Mapping[str, Any]) -> None:
        from services.cowork_agent.storm.comms import runtimes
        note = await runtimes.approved(item)
        await self.chats.engine.write(lambda w: self._relay(w, item, None, note, "runtime-action"))

    def opened(self, w: Writes, item: Mapping[str, Any]) -> None:
        thread = _thread_of(w, item)
        if thread is not None and item["origin"] not in AGENT_ORIGINS and item["kind"] in TOLD_XO_KINDS:
            self._told_by_xo(w, item, thread)
        if thread is None or item["origin"] not in AGENT_ORIGINS:
            return

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
        """Nothing to write: its thread's state is read from the open items
        themselves (``thread_status``)."""

    def paused(self, w: Writes, item: Mapping[str, Any]) -> None:
        thread = _thread_of(w, item)
        if thread is not None:
            self._relay(w, item, thread["ts"], f"The owner paused thread “{thread['title']}” ({thread['ts']}) after "
                        "repeated denials. If its work should go on, start a fresh thread for it.", "paused")

    @staticmethod
    def _relay(w: Writes, item: Mapping[str, Any], thread_ts: str | None, note: str, reason: str) -> None:
        """XO tells the chat's live coordinator ``note`` about ``item``."""
        coordinator = live_coordinator(w, item["chat_id"])
        if coordinator is None:
            return
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=item["chat_id"], thread_ts=thread_ts, kind="relay", from_role="xo",
                     from_session=None, to_role="project-coordinator", to_session=coordinator, cause=item["id"],
                     note=note, wake_id=f"deleg:{hop_id}")
        w.wake(coordinator, Wake(id=f"deleg:{hop_id}", priority="next", reason="relay", text=envelope.relay(
            sender_role="xo", sender_session=None, note=note, reason=reason, now=w.now, hop=hop_id)))

    def answered(self, w: Writes, item: Mapping[str, Any], text: str, option: str | None) -> str:
        chat = rows.get_chat(w, item["chat_id"]) if item["chat_id"] else None
        return envelope.review_answered(chat=chat, thread_ts=item["thread_ts"], item=item, answer=text, option=option,
                                        now=w.now)


def _thread_of(w: Writes, item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not item.get("chat_id") or not item.get("thread_ts"):
        return None
    return rows.get_thread(w, item["chat_id"], item["thread_ts"])
