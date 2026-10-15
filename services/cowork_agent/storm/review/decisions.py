"""Checking a person's decision against its item, and what it does (design
ch 15 §7; ch 17 §2.5).

``plan`` reads nothing and writes nothing: from the stored item and the
request it builds the decision record, the answer for the waiting call, the
rule to make, the wake text for an ask, or refuses with an actionable 400.
The queue applies the plan in one transaction.

A decision is single-use and bound to the exact input it was asked for: the
record keeps that input's hash, and an ``edited_input`` (the person's own
change, allowed once only) is recorded with its own hash, which is what a
grant then matches.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from services.cowork_agent.storm.review.engine_link import Decision
from services.cowork_agent.storm.review.items import DECISIONS, render_rule
from services.cowork_agent.storm.review.rule_store import match_of
from services.cowork_agent.storm.review.rules import scope_refusal
from services.cowork_agent.storm.review.tiers import Tier
from services.errors import ServiceError


class ReviewRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(code, message, status=status)
        for key, value in extra.items():
            setattr(self, key, value)


@dataclass(frozen=True, kw_only=True)
class Request:
    """The decision body (``POST /api/chat/review/{id}/decision``), without
    ``decided_by``: the server sets that."""

    decision: str
    idempotency_key: str
    scope: str | None = None
    option: str | None = None
    answer: str | Mapping[str, Any] | None = None
    note: str | None = None
    edited_input: Mapping[str, Any] | None = None
    rule_sentence: str | None = None

    def digest(self) -> str:
        """What a retry with the same key must repeat exactly."""
        body = {k: v for k, v in self.__dict__.items() if k != "idempotency_key"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class Plan:
    record: dict[str, Any]                         # the stored decision
    call: Decision | None = None                   # the answer for a held (or parked) CLI call
    input_sha256: str | None = None                # what a grant is bound to, when it differs from the ask's
    rule: Mapping[str, Any] | None = None          # the rule the decision makes
    tier: Tier | None = None
    wake: str | None = None                        # an ask's answer, for the asking session
    denied: bool = False                           # a permission denied: the loop breaker counts it
    pause: bool = False                            # a suggestion confirmed: pause the thread
    usage_continue: bool | None = None
    sentence: str | None = None                    # the rule's, as the server renders it


def plan(item: Mapping[str, Any], native: Mapping[str, Any] | None, req: Request) -> Plan:
    kind, decision = item["kind"], req.decision
    if decision not in DECISIONS[kind]:
        raise ReviewRefused("bad_decision", f"A {kind} item takes {' or '.join(sorted(DECISIONS[kind]))}, "
                                            f"not {decision}.")
    if kind != "permission" and (req.scope or req.edited_input is not None or req.rule_sentence):
        raise ReviewRefused("bad_decision", "scope, edited_input and rule_sentence apply to permissions only.")
    record = {"decision": decision, "note": req.note, "idempotency_key": req.idempotency_key,
              "request_sha256": req.digest()}
    if kind == "permission":
        return _permission(item, native or {}, req, record)
    if kind == "question":
        answers = _answers(item, req)
        record["answer"] = answers
        if native and native.get("call"):
            return Plan(record, call=Decision(allow=True, answers=answers))
        said = "; ".join(f"{q}: {a}" for q, a in answers.items())
        return Plan(record, wake=f"The person answered your question ({item['title']}): {said}")
    if req.option is not None or (req.answer is not None and kind != "create_project"):
        raise ReviewRefused("bad_decision", f"A {kind} item takes no option or answer.")
    if kind == "create_project" and req.answer is not None:
        if not isinstance(req.answer, str) or not req.answer.strip():
            raise ReviewRefused("bad_answer", "The project's name is a non-empty string.")
        record["answer"] = req.answer.strip()
    if kind == "usage":
        return Plan(record, usage_continue=decision == "continue")
    if kind == "suggestion":
        return Plan(record, pause=decision == "approve")
    if kind == "integrity":
        return Plan(record)
    named = f" (named {record['answer']})" if record.get("answer") else ""
    note = f" Their note: {req.note}" if req.note else ""
    return Plan(record, wake=f"The person {_ASK_SAYS[decision]} {item['title']}{named}.{note}")


#: How an ask's answer reads to the agent that asked (ch 11 §4).
_ASK_SAYS = {"approve": "approved:", "send_back": "sent this back:", "done": "did it:", "cant": "can't do it:",
             "allow": "allowed:"}


def _permission(item: Mapping[str, Any], native: Mapping[str, Any], req: Request, record: dict) -> Plan:
    tier = Tier[(item["risk"] or "notable").upper()]
    asked = (native.get("call") or {}).get("input_sha256")
    if req.decision == "deny":
        if req.scope or req.option or req.edited_input is not None or req.rule_sentence or req.answer is not None:
            raise ReviewRefused("bad_decision", "A deny takes only a note.")
        record["input_sha256"] = asked
        return Plan(record, call=Decision(allow=False, message=req.note), denied=True)
    if req.answer is not None:
        raise ReviewRefused("bad_decision", "An allow takes no answer.")
    scope = req.scope or "once"
    record["scope"] = scope
    if req.edited_input is not None:
        if scope != "once":
            raise ReviewRefused("scope_not_allowed", "An edited input can be allowed once only; a rule or this "
                                                     "thread's allowance covers what the agent asked.")
        edited = hashlib.sha256(json.dumps(dict(req.edited_input), sort_keys=True, separators=(",", ":"),
                                           default=str).encode()).hexdigest()
        record.update(input_sha256=edited, edited_input=dict(req.edited_input), edited=True)
        return Plan(record, call=Decision(allow=True, updated_input=dict(req.edited_input)), input_sha256=edited,
                    tier=tier)
    record["input_sha256"] = asked
    if scope == "once":
        if req.option not in (None, "once") or req.rule_sentence:
            raise ReviewRefused("bad_decision", "Allow once takes no rule option or sentence.")
        return Plan(record, call=Decision(allow=True), tier=tier)
    if scope == "rule" and tier == Tier.DESTRUCTIVE:
        raise ReviewRefused("scope_not_allowed", "A destructive action can only be allowed once, or for this "
                                                 "thread as this exact command.")
    option_id = "thread" if scope == "thread" else req.option
    option = next((o for o in item["options"] if o["id"] == option_id and o.get("rule")), None)
    if option is None or (scope == "rule" and option["scope"] != "rule"):
        if scope == "thread":
            raise ReviewRefused("scope_not_allowed", "This item can't be allowed for its thread (it has no thread, "
                                                     "or no rule can cover what it asks). Allow it once.")
        if req.option is None:
            raise ReviewRefused("bad_option", "Name one of the item's rule options (its `id`) to make it a rule.")
        offered = [o["id"] for o in item["options"] if o.get("scope") == "rule"]
        raise ReviewRefused("scope_not_allowed", f"This item can't become that rule (its tier or where it was asked "
                                                 f"limits how wide a rule may be); it offers "
                                                 f"{', '.join(offered) or 'no rule'}.")
    rule = option["rule"]
    why = scope_refusal(tier, rule["scope"], match_of(rule["match"]))
    if why is not None:
        raise ReviewRefused("scope_not_allowed", f"Not allowed: {why}.")
    sentence = render_rule(rule)
    if (scope == "rule" or req.rule_sentence is not None) and req.rule_sentence != sentence:
        raise ReviewRefused("rule_sentence_mismatch", "rule_sentence must be exactly the sentence XO renders for "
                                                      "this rule. Show the person this one, then send it back.",
                            sentence=sentence)
    record["option"] = option_id
    session_scope = scope == "thread" and "session" in (native.get("scopes") or ())
    return Plan(record, call=Decision(allow=True, scope="session" if session_scope else "once"), rule=rule,
                tier=tier, sentence=sentence)


def _answers(item: Mapping[str, Any], req: Request) -> dict[str, Any]:
    """Each question's id to its answer, as the CLI's question reads them."""
    questions = item["detail"].get("questions") or [{"id": "answer", "options": []}]
    ids = [str(q.get("id")) for q in questions]
    if req.option is not None:
        if len(questions) != 1 or req.option not in [o.get("label") for o in questions[0].get("options") or []]:
            raise ReviewRefused("bad_answer", "option must be the label of one of the question's options.")
        return {ids[0]: req.option}
    if isinstance(req.answer, str) and req.answer.strip():
        if len(ids) != 1:
            raise ReviewRefused("bad_answer", f"This item asks {len(ids)} questions: answer with an object of "
                                              "each question's id to its answer.")
        return {ids[0]: req.answer}
    if isinstance(req.answer, Mapping) and sorted(req.answer) == sorted(ids) and all(
            (isinstance(a, str) and a.strip()) or (isinstance(a, list) and a and all(isinstance(x, str) for x in a))
            for a in req.answer.values()):
        return dict(req.answer)
    raise ReviewRefused("bad_answer", f"Answer each question ({', '.join(ids)}) in words, or pick an option.")
