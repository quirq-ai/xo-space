"""The review item: one model for every kind (design ch 15 §5; ch 08 §2).

An item is a row in ``review_items``: who asked (chat, thread, session,
sub-agent, turn, epoch), its kind, its **computed** risk with the reasons
(the policy's, never the agent's words), a title, the agent's own "why"
(display only), the exact detail (command, paths, URL, input; never prose),
the options the person may choose, its state (``open | decided | expired |
withdrawn``) and, once decided, the decision with who made it.

The options say exactly what each choice does. A permission's rule options
carry the rule they would make and the sentence the person sees; the decision
must send that sentence back ("make it a rule", ch 15 §3). Scope limits by
tier hold here and again when deciding: a destructive action is allowed only
once, or for this thread as this exact command; a notable one at most for a
project; a workspace rule only for routine work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence

from services.cowork_agent.storm.redact import redact, redact_data
from services.cowork_agent.storm.review.rule_store import match_json, match_of
from services.cowork_agent.storm.review.rules import (
    DEFAULT_RULES,
    Match,
    prefix_rules_allowed,
    render_sentence,
    scope_refusal,
)
from services.cowork_agent.storm.review.tiers import Assessment, Part, Tier
from services.timestamps import iso, parse_ts

KINDS = ("permission", "question", "approval", "action", "create_project", "create_job", "usage", "integrity",
         "suggestion")
#: The decisions each kind takes (ch 15 §5.1; ch 17 §2.5). A suggestion
#: (ch 15 §9) is approved (pause it) or denied (dismissed); an integrity
#: report is marked seen with ``done``.
DECISIONS: Mapping[str, frozenset[str]] = {
    "permission": frozenset({"allow", "deny"}), "question": frozenset({"answer"}),
    "approval": frozenset({"approve", "send_back"}), "action": frozenset({"done", "cant"}),
    "create_project": frozenset({"allow", "send_back"}), "create_job": frozenset({"allow", "send_back"}),
    "usage": frozenset({"continue", "wait"}), "integrity": frozenset({"done"}),
    "suggestion": frozenset({"approve", "deny"}),
}
#: An unanswered ask expires after 7 days (ch 15 §5.2; ch 11 §10); a held
#: call never does while it is held, and a parked one 7 days after parking.
EXPIRY = timedelta(days=7)
EXPIRING_KINDS = frozenset({"question", "approval", "action"})
#: Who decides, always: the server writes it, never a request body (ch 15 §7.1).
DECIDED_BY = {"kind": "owner", "via": "ui"}
_GLOB = frozenset("*?[")

#: Why an action that carries a secret is offered no rule that keeps it.
SECRET_NO_RULE = ("It carries a secret, so no rule that would keep the secret is offered: a rule stays in XO's "
                    "records for good.")
_ALLOW_ONCE = {"id": "once", "decision": "allow", "scope": "once", "label": "Allow once"}
_DENY = {"id": "deny", "decision": "deny", "label": "Deny", "note": True}
_FIXED_OPTIONS: Mapping[str, tuple[dict[str, Any], ...]] = {
    "permission": (_ALLOW_ONCE, _DENY),         # what no rule may cover is only ever allowed once
    "question": ({"id": "answer", "decision": "answer", "label": "Answer"},),
    "approval": ({"id": "approve", "decision": "approve", "label": "Approve"},
                 {"id": "send_back", "decision": "send_back", "label": "Send back", "note": True}),
    "action": ({"id": "done", "decision": "done", "label": "Done"},
               {"id": "cant", "decision": "cant", "label": "Can't", "note": True}),
    "create_project": ({"id": "allow", "decision": "allow", "label": "Allow", "answer": "name"},
                       {"id": "send_back", "decision": "send_back", "label": "Send back", "note": True}),
    "create_job": ({"id": "allow", "decision": "allow", "label": "Allow"},
                   {"id": "send_back", "decision": "send_back", "label": "Send back", "note": True}),
    "usage": ({"id": "continue", "decision": "continue", "label": "Continue until the next reset"},
              {"id": "wait", "decision": "wait", "label": "Wait"}),
    "integrity": ({"id": "done", "decision": "done", "label": "Seen"},),
    "suggestion": ({"id": "approve", "decision": "approve", "label": "Pause it", "hold_to_confirm": True},
                   {"id": "deny", "decision": "deny", "label": "Dismiss"}),
}


@dataclass(frozen=True, kw_only=True)
class ItemSpec:
    """What opens an item. The engine's asks become one (``queue.open``);
    the conversation layer's asks (``ask_owner``, ``propose_project``,
    ``create_job``, B3) and the integrity check (PR 17) open theirs with
    ``ReviewQueue.open_item``."""

    kind: str
    title: str
    origin: str                              # cli | engine | agent | xo
    session_id: str | None = None
    chat_id: str | None = None
    thread_ts: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    epoch: int | None = None
    risk: Tier | None = None
    risk_reasons: Sequence[str] = ()
    blocking: bool = False
    why: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    options: Sequence[Mapping[str, Any]] | None = None     # None: the kind's own
    native: Mapping[str, Any] | None = None                # a held call's ids: answered in the call
    expires: bool | None = None                            # None: by kind (asks without a held call)


def fixed_options(kind: str) -> list[dict[str, Any]]:
    return [dict(o) for o in _FIXED_OPTIONS[kind]]


def expiry(now: str) -> str:
    return iso(parse_ts(now) + EXPIRY)


def permission_options(*, kind: str, tier: Tier, assessment: Assessment | None, revoked: frozenset[str],
                       coordinator: bool, thread_ts: str | None, project_pid: str | None,
                       project_name: str | None) -> list[dict[str, Any]]:
    """Allow once; for this thread; the rules it may become; deny.

    A rule is offered only for the one part of the action no default rule
    covers (a compound like ``cd src && npm test`` needs one rule, for
    ``npm test``), never for what XO can't see through, and never for a
    coordinator, whose checks no rule or grant widens. Nor for an action that
    carries a secret (ch 16 §3) in its match or sentence: a rule (a
    thread's too) would keep it for good. The once option says why.
    """
    options = [dict(_ALLOW_ONCE)]
    withheld = False
    part = None if coordinator else _the_asked_part(assessment, revoked)
    exact = _exact_match(kind, part) if part else None
    if exact is not None:
        wider = [("rule:project", "project", exact), ("rule:workspace", "workspace", exact)]
        if exact.command is not None and len(exact.command) > 2 and not exact.command[1].startswith("-") \
                and prefix_rules_allowed(exact.command):
            prefix = Match(prefix=exact.command[:2])
            wider[1:1] = [("rule:project:prefix", "project", prefix)]
            wider.append(("rule:workspace:prefix", "workspace", prefix))
        candidates = ([("thread", "thread", exact)] if thread_ts else []) + \
            [c for c in wider if c[1] == "workspace" or project_pid]
        for option_id, scope, match in candidates:
            if scope_refusal(tier, scope, match) is not None:
                continue
            rule = {"scope": scope, "kind": kind, "match": match_json(match), "effect": "allow",
                    "project_pid": project_pid if scope != "workspace" else None,
                    "thread_ts": thread_ts if scope == "thread" else None, "project_name": project_name}
            sentence = render_rule(rule)
            if redact(sentence) != sentence or redact_data(rule["match"]) != rule["match"]:
                withheld = True
                continue
            options.append({"id": option_id, "decision": "allow", "scope": "thread" if scope == "thread" else "rule",
                            "label": "Allow for this thread" if scope == "thread" else sentence,
                            "rule": rule, "sentence": sentence})
    if withheld:
        options[0]["why"] = SECRET_NO_RULE
    options.append(dict(_DENY))
    return options


def render_rule(rule: Mapping[str, Any]) -> str:
    """The server's rendering of an option's rule: what the person must see."""
    return render_sentence(rule["effect"], rule["scope"], rule["kind"], match_of(rule["match"]),
                           rule.get("project_name"))


def _the_asked_part(assessment: Assessment | None, revoked: frozenset[str]) -> Part | None:
    if assessment is None or assessment.refused or assessment.unverifiable:
        return None
    asked = [p for p in assessment.parts if not (p.tier == Tier.ROUTINE and any(
        p.category in d.categories and d.id not in revoked for d in DEFAULT_RULES))]
    return asked[0] if len(asked) == 1 else None


def _exact_match(kind: str, part: Part) -> Match | None:
    if kind == "command" and part.words:
        return Match(command=part.words)
    if kind in ("read", "search", "list", "edit_file", "create_file", "delete_file") and part.path \
            and not _GLOB & set(part.path):
        return Match(path_glob=part.path)
    if kind == "web_fetch" and part.host:
        return Match(host=part.host)
    if kind == "mcp" and part.mcp:
        return Match(mcp=part.mcp)
    return None


COLUMNS = ("id, seq, kind, state, risk, risk_reasons, blocking, title, why, detail, options, chat_id, thread_ts, "
           "session_id, run_id, turn_id, epoch, origin, parked, created_at, expires_at, decision, decided_by, "
           "decided_at, delivery, annotations, native")


def view(row: Sequence[Any]) -> dict[str, Any]:
    """An item as the routes and the stream show it. ``native`` (the CLI's
    ids, to answer its call) stays inside."""
    (item_id, seq, kind, state, risk, reasons, blocking, title, why, detail, options, chat_id, thread_ts,
     session_id, run_id, turn_id, epoch, origin, parked, created_at, expires_at, decision, decided_by, decided_at,
     delivery, annotations, _native) = row
    return {"id": item_id, "seq": seq, "kind": kind, "state": state, "risk": risk, "risk_reasons": json.loads(reasons),
            "blocking": bool(blocking), "title": title, "why": why, "detail": json.loads(detail),
            "options": json.loads(options), "chat_id": chat_id, "thread_ts": thread_ts, "session_id": session_id,
            "run_id": run_id, "turn_id": turn_id, "epoch": epoch, "origin": origin, "parked": bool(parked),
            "created_at": created_at, "expires_at": expires_at,
            "decision": json.loads(decision) if decision else None,
            "decided_by": json.loads(decided_by) if decided_by else None, "decided_at": decided_at,
            "delivery": delivery, "annotations": json.loads(annotations)}
