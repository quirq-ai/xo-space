"""The decision-model provider: Sage or Jev routes, the agent handles everything else.

With ``XO_ROUTER_MODE=decision`` (or a level set to it), an owner's message
is first classified by the decision model:

- **workspace level**: which projects does it concern? (a tags question over
  the projects, each described by its README or ``project.json``). Every
  project that clearly applies gets the message forwarded.
- **project level**: is it a new task, a follow-up to a thread, a question,
  or chat? (a choice), and if a follow-up, which thread? (a choice over the
  project's threads plus ``none``). A confident new task starts a thread on
  the message; a confident follow-up is relayed to that thread.

Whatever the model is unsure about (an abstain, under the threshold, a
failure, no key) and everything that needs words (answering a question,
chat, ``no_such_project``) goes to the agent router: the chat's
coordinator gets the message as usual, with a note that the model handed it
over. A decision model can only route; it never writes an answer.

The decision is applied through the same comms tools the agent uses, on the
coordinator's behalf, so the store, the threads and the decision log look
the same whichever provider routed.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import services.decision_models as dm
from services.projects_chat import comms, coordinators, store, workspace
from services.projects_chat.router import mode
from services.projects_chat.router.decision import Action, Decision

log = logging.getLogger(__name__)

INTENT_ID, THREAD_ID, PROJECTS_ID = "intent", "thread", "projects"
INTENTS = {
    "new_task": "asks for new work that changes the project: code, files, a fix, a feature, a PR",
    "follow_up": "adds to or changes work already under way in one of this project's threads",
    "question": "asks a question about the project that can be answered without changing it",
    "chat": "a greeting, thanks, or a remark that asks for nothing",
}
NO_THREAD = "none"


def _title(text: str) -> str:
    first = re.split(r"[.\n!?]", text.strip(), maxsplit=1)[0]
    return (first[:60].rstrip() + ("…" if len(first) > 60 else "")) or "Thread"


def project_questions(project: str) -> list:
    questions = [dm.Choice(INTENT_ID, "The content is a message the owner of a software project sent to the "
                                      "project's chat. What does it ask for?", dict(INTENTS))]
    threads = [t for t in store.list_threads(project)][:20]
    if threads:
        options = {t["ts"]: f"{t.get('title')} ({t.get('state')})" for t in threads}
        options[NO_THREAD] = "none of these threads is about it"
        questions.append(dm.Choice(THREAD_ID, "If the message is about work already under way, which thread is "
                                              "doing that work?", options))
    return questions


def workspace_questions() -> list:
    projects = {p: (workspace._describe(p) or p) for p in workspace._projects()}
    return [dm.Tags(PROJECTS_ID, "The content is a message to a workspace that holds these projects. Tag every "
                                 "project the message asks to change or asks about. Do not tag a project it only "
                                 "mentions in passing.", projects)] if projects else []


def decide(level: str, project: Optional[str], trigger: dict, result: dm.Result) -> Decision:
    """Turn the model's answers into a :class:`Decision`. An empty action list
    means: hand the message to the agent router."""
    ch = store.chat(project)
    threshold = mode.abstain_below()
    decision = Decision(level=level, chat_id=ch.id, trigger_id=trigger["id"], provider=result.provider,
                        usage={"input_tokens": result.input_tokens}, latency_ms=result.latency_ms)
    if level == "workspace":
        answer = result.answers.get(PROJECTS_ID)
        chosen = sorted(k for k, v in (answer.applies if answer else {}).items() if v is True)
        decision.probabilities = answer.probabilities if answer else None
        if answer is None or answer.error or not chosen:
            decision.abstained = True
            decision.reasons.append(answer.error if answer and answer.error else "no project clearly applies")
            return decision
        decision.actions = [Action("forward", target=f"proj:{p}", cited=[trigger["id"]]) for p in chosen]
        decision.confidence = min(answer.probabilities.get(p, 0.0) for p in chosen)
        return decision
    intent = result.answers.get(INTENT_ID)
    decision.probabilities = intent.probabilities if intent else None
    decision.confidence = intent.probability if intent else None
    if intent is None or intent.abstains(threshold):
        decision.abstained = True
        decision.reasons.append(intent.error if intent and intent.error else "unsure what the message asks for")
        return decision
    if intent.chosen == "new_task":
        decision.actions = [Action("start_thread", title=_title(trigger["text"]), cited=[trigger["id"]])]
    elif intent.chosen == "follow_up":
        thread = result.answers.get(THREAD_ID)
        if thread is None or thread.abstains(threshold) or thread.chosen == NO_THREAD:
            decision.abstained = True
            decision.reasons.append("a follow-up, but unsure to which thread")
        else:
            decision.actions = [Action("message_thread", target=thread.chosen, cited=[trigger["id"]])]
            decision.confidence = min(intent.probability or 0.0, thread.probability or 0.0)
    else:
        decision.reasons.append(f"{intent.chosen}: needs words, so the agent answers")
    return decision


def apply(decision: Decision, project: Optional[str], trigger: dict) -> bool:
    """Carry out a routing decision through the comms tools. Returns ``False``
    when it routed nothing (the agent router takes the message)."""
    if not decision.actions:
        return False
    coordinator = coordinators.ensure(project)
    caller = comms.Caller(project, store.get_session(project, coordinator["id"]))
    for action in decision.actions:
        if action.kind == "forward":
            comms.call(caller, "forward_to_project", {"project": action.target.split(":", 1)[1],
                                                      "context_message_ids": action.cited})
        elif action.kind == "start_thread":
            comms.call(caller, "start_thread_session", {
                "message_id": trigger["id"], "title": action.title or _title(trigger["text"]),
                "instructions": f"Routed by {decision.provider}: the owner's message is attached; do what it asks.",
                "project_ack": "On it, starting a thread."})
        elif action.kind == "message_thread":
            comms.call(caller, "message_thread", {"thread_id": action.target, "context_message_ids": action.cited})
            thread = store.get_thread(project, action.target) or {}
            comms.call(caller, "post_message", {"text": f"Sent to [{thread.get('title', 'its thread')}](#{action.target})."})
    if decision.level == "workspace":
        names = ", ".join(a.target.split(":", 1)[1] for a in decision.actions)
        comms.call(caller, "post_message", {"text": f"Sent to {names}."})
    return True


async def route(level: str, project: Optional[str], trigger: dict) -> Decision:
    """Classify, decide, apply, log. Returns the decision (``escalated_from``
    set when the agent router must take the message)."""
    provider = mode.decision_model()
    questions = workspace_questions() if level == "workspace" else project_questions(project)
    started = time.perf_counter()
    if not questions:
        result = dm.Result(provider, {})
    else:
        result = await dm.ask(provider, trigger["text"][:4000], questions)
    decision = decide(level, project, trigger, result)
    decision.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    routed = False
    try:
        routed = apply(decision, project, trigger)
    except comms.CommsError as exc:
        decision.reasons.append(f"could not apply: {exc.message}")
        decision.actions = []
    if not routed:
        decision.escalated_from = provider
    store.record_decision(project, decision.as_dict())
    return decision
