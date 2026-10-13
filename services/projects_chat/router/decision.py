"""The router contract: one request shape in, one decision shape out, for every provider.

A :class:`Decision` is a list of actions for one triggering message. The
agent provider produces it from the coordinator's comms tool calls; a
decision model produces it from its answers. Core applies it the same way
either way, and logs it to the chat's ``router/decisions.jsonl``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from services.errors import ServiceError

SCHEMA = 1

# Which actions each level may take.
WORKSPACE_ACTIONS = ("forward", "no_such_project", "answer", "react", "no_reply")
PROJECT_ACTIONS = ("start_thread", "message_thread", "ask_workspace", "answer", "react", "no_reply")
ACTIONS = {"workspace": WORKSPACE_ACTIONS, "project": PROJECT_ACTIONS}
PROVIDERS = ("agent", "sage", "jev")


class DecisionError(ServiceError):
    """A decision does not fit the contract."""


@dataclass
class Candidate:
    """A place the message could go: a project (workspace level) or a thread."""

    id: str                      # "proj:<folder>" or a thread ts ("cmsg_…")
    title: str
    description: str = ""
    state: Optional[str] = None
    headline: Optional[str] = None
    last_activity_at: Optional[str] = None


@dataclass
class RouterRequest:
    level: str
    chat_id: str
    trigger: dict                # the stored message row
    candidates: list[Candidate] = field(default_factory=list)
    recent: list[dict] = field(default_factory=list)

    @property
    def allowed_actions(self) -> tuple[str, ...]:
        return ACTIONS[self.level]


@dataclass
class Action:
    kind: str
    target: Optional[str] = None             # "proj:<folder>" or a thread ts
    title: Optional[str] = None
    note: Optional[str] = None
    cited: list[str] = field(default_factory=list)
    reply: Optional[str] = None
    question: Optional[str] = None
    emoji: Optional[str] = None
    reason: Optional[str] = None
    setup: Optional[dict] = None             # {profile, model, effort, source}

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {})}


@dataclass
class Decision:
    level: str
    chat_id: str
    trigger_id: str
    provider: str
    actions: list[Action] = field(default_factory=list)
    ack: Optional[str] = None
    confidence: Optional[float] = None
    probabilities: Optional[dict] = None
    abstained: bool = False
    escalated_from: Optional[str] = None     # a decision model that handed over to the agent
    usage: Optional[dict] = None
    latency_ms: Optional[float] = None
    reasons: list[str] = field(default_factory=list)

    def validate(self) -> "Decision":
        if self.level not in ACTIONS:
            raise DecisionError("invalid_level", f"level must be one of {', '.join(ACTIONS)}.")
        if self.provider not in PROVIDERS:
            raise DecisionError("invalid_provider", f"provider must be one of {', '.join(PROVIDERS)}.")
        allowed = ACTIONS[self.level]
        for action in self.actions:
            if action.kind not in allowed:
                raise DecisionError("invalid_action",
                                    f"{action.kind!r} is not a {self.level}-level action ({', '.join(allowed)}).")
            if action.kind in ("forward", "message_thread") and not action.target:
                raise DecisionError("missing_target", f"{action.kind} needs a target.")
            if action.kind == "start_thread" and not (action.title or "").strip():
                raise DecisionError("missing_title", "start_thread needs a title.")
            if len(action.cited) > 8:
                raise DecisionError("too_many_cited", "At most 8 messages can be cited.")
        return self

    def as_dict(self) -> dict[str, Any]:
        out = {"schema": SCHEMA, "level": self.level, "chat_id": self.chat_id,
               "trigger_id": self.trigger_id, "provider": self.provider,
               "actions": [a.as_dict() for a in self.actions]}
        for name in ("ack", "confidence", "probabilities", "escalated_from", "usage", "latency_ms"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.abstained:
            out["abstained"] = True
        if self.reasons:
            out["reasons"] = self.reasons
        return out
