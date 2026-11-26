"""``manage_runtime``, a project coordinator's tool (plan §5.4, decision 3):
install, update or uninstall a runtime's CLI through the lifecycle engine,
the same job the owner's buttons run, with the command fixed in the
runtime's manifest. Install and update start at once. Uninstall can stop
sessions, so it only asks: an approval whose ``runtime_action`` (a detail
key no ``ask`` can write) starts the job once the owner approves
(``Conversation.decided``), and the coordinator is told how it ended. Signing in stays the owner's."""

from __future__ import annotations

from typing import Any, Mapping

from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall, current_turn
from services.cowork_agent.storm.comms.tools import WORD_MAX
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.records.ledger import FOUNDER
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.tiers import Reason, Tier
from services.runtime_lifecycle import jobs, registry
from services.runtime_lifecycle.contract import LifecycleError

ACTIONS = ("install", "update", "uninstall")
GUIDANCE = "Nothing is uninstalled until the owner approves; their answer arrives as a new wake. Keep working."


def _runtime(t: ToolCall) -> registry.Runtime:
    try:
        return registry.get(a.text(t.args, "runtime", limit=WORD_MAX))
    except LifecycleError as exc:
        raise CommsError(exc.code, exc.message) from None


async def manage_runtime(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    rt = _runtime(t)
    action = a.choice(t.args, "action", ACTIONS, required=True)
    force = a.flag(t.args, "force", default=False)
    if rt.command(action) is None:
        raise CommsError("unsupported", f"{rt.spec.title} has no {action} action.")
    if action != "uninstall":
        try:
            job = await jobs.start(rt, action, by=caller.session_id)
        except LifecycleError as exc:
            raise CommsError(exc.code, exc.message) from None
        started = {"runtime": rt.name, "action": action, "job": job.as_dict()}
        return await t.write(lambda w: started)
    sessions = jobs.in_use(rt)
    if sessions and not force:
        raise CommsError("runtime_in_use", f"{len(sessions)} session(s) run on {rt.spec.title} "
                                           f"({', '.join(sessions)}); call again with force to stop them on "
                                           "uninstall.")
    stops = f" and stop {len(sessions)} session(s) running on it" if sessions else ""
    keeps = ", ".join((rt.command(action) or {}).get("keeps") or ())

    def fn(w: Writes) -> dict[str, Any]:
        item = t.chats.queue.open_item(w, ItemSpec(
            kind="approval", title=f"Uninstall {rt.spec.title}{stops}",
            why=f"The project coordinator asked to uninstall {rt.package}." + (f" {keeps} is kept." if keeps else ""),
            origin="agent", session_id=caller.session_id, chat_id=caller.chat_id, thread_ts=caller.thread_ts,
            turn_id=current_turn(w, caller.session_id), epoch=caller.epoch, risk=Tier.NOTABLE,
            risk_reasons=(Reason(Tier.NOTABLE, "removes a runtime's CLI"
                                 + (", stopping its sessions" if sessions else "")),),
            detail={"runtime_action": {"runtime": rt.name, "action": action, "force": force},
                    "text": f"Uninstall {rt.package}{stops}.", "sessions": sessions}))
        return t.record(w, {"review_id": item, "state": "open", "guidance": GUIDANCE}, to="founder")
    return await t.engine.write(fn)


async def approved(item: Mapping[str, Any]) -> str:
    """The owner approved a ``manage_runtime`` uninstall: run it and say how it ended. Sessions the card
    didn't list are never stopped: the job refuses instead."""
    asked = item["detail"]["runtime_action"]
    action, listed = asked["action"], frozenset(item["detail"].get("sessions") or ())
    try:
        rt = registry.get(asked["runtime"])
        job = await jobs.start(rt, action, force=bool(asked.get("force")), may_stop=listed, by=FOUNDER)
    except LifecycleError as exc:
        return f"The approved {action} of {asked['runtime']} was refused: {exc.message}"
    async for _ in job.follow(jobs.TIMEOUT_S):
        pass
    result = job.events[-1] if job.events else {}
    if job.status == "done":
        return f"The approved {action} of {rt.spec.title} finished."
    return f"The approved {action} of {rt.spec.title} failed: {result.get('message') or job.status}."
