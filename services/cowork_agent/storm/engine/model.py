"""The engine's own types: what callers hand it, what it hands back, and its
typed failures (design ch 07).

Everything here is plain data. The seams other packages implement (the
stream's listener, the review queue's desk) are in ``ports.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from services.cowork_agent.adapters.contract import PermissionRequest, Question
from services.cowork_agent.storm.review import Verdict
from services.errors import ServiceError

Priority = Literal["now", "next", "later"]
#: Fairness order: a lower rank goes first (ch 07 §5.3).
PRIORITY_RANK: Mapping[str, int] = {"now": 0, "next": 1, "later": 2}
#: A session's role is its comms role (ch 12 §3): the policy reads it.
ROLES = frozenset({"plain", "thread", "project-coordinator", "workspace-coordinator"})
#: The env variable an MCP server's ``bearer_env`` names to get the
#: session's own comms token, minted at each attach for that epoch (ch 12
#: §2.2). A spec never carries the value.
SESSION_TOKEN_ENV = "XO_COMMS_TOKEN"
#: What ``sessions.status`` may hold (ch 07 §2).
STATUSES = ("cold", "queued", "starting", "running", "warm", "waiting_on_person", "parked", "stopping")


@dataclass(frozen=True, kw_only=True)
class Wake:
    """One queued input for a session (ch 07 §4.1). ``id`` is the idempotency
    key (``msg:<message id>``, ``review:<item>:<seq>``, ``deleg:<id>``, …): a
    second delivery with the same id is a no-op. ``text`` is what the agent
    reads (an envelope, chapter 11, or the person's words); ``context`` is
    hidden per-turn context. ``delivery`` applies while a turn runs: ``add``
    joins the running turn where the runtime steers (else it waits for the
    next turn), ``now`` interrupts the turn first; None waits for the next
    turn. ``kept`` is what the wake's envelope becomes once it is consumed or
    dropped, when ``text`` quotes words the store keeps only redacted (the
    person's message as typed, §0.112): the original lives in the queue until
    it is delivered, and no longer. ``confirmed_runs`` is the count of
    background runs a ``now`` was confirmed to stop (§0.68): the interrupt
    goes ahead only while that is still the count."""

    id: str
    text: str
    priority: Priority = "next"
    reason: str = "message"
    origin: Literal["person", "wake", "system"] = "wake"
    context: str | None = None
    delivery: Literal["add", "now"] | None = None
    kept: str | None = None
    confirmed_runs: int | None = None


@dataclass(frozen=True, kw_only=True)
class CallRef:
    """What the engine needs to deliver a decision once the waiting call is
    gone (parked, or its process died): the review queue keeps it with the
    item and hands it back with the decision."""

    session_id: str
    epoch: int
    kind: Literal["permission", "question"]
    tool: str
    input_sha256: str
    summary: str                     # what was asked, one phrase (the exact command, in backticks)


@dataclass(frozen=True, kw_only=True)
class Ask:
    """Something only the person can settle. ``kind`` is the review item kind:
    ``permission`` (a held CLI check the policy said to ask about),
    ``question`` (a CLI's own question to the person) or ``usage`` (a pool
    moved onto paid usage). ``verdict`` is the policy's, for a permission."""

    kind: Literal["permission", "question", "usage"]
    title: str
    session_id: str | None = None
    epoch: int | None = None
    chat_id: str | None = None
    thread_ts: str | None = None
    turn_id: str | None = None
    run: str | None = None
    request: PermissionRequest | None = None
    question: Question | None = None
    verdict: Verdict | None = None
    call: CallRef | None = None
    pool: str | None = None
    run_title: str | None = None         # the asking sub-agent's title (ch 14 §6), so the item can name it


@dataclass(frozen=True, kw_only=True)
class Decision:
    """The person's answer to an ask. For a permission: ``allow``, the scope
    the runtime remembers (``session``: the same call again in this process),
    an edited input, and a note (the agent sees it on a deny). For a
    question: ``answers`` maps each question id to its answer."""

    allow: bool
    message: str | None = None
    scope: Literal["once", "session"] = "once"
    updated_input: Mapping[str, Any] | None = None
    answers: Mapping[str, Any] | None = None


Delivery = Literal["in_call", "grant", "wake"]


@dataclass(frozen=True)
class StopResult:
    stopped: bool
    turn_id: str | None = None


# ── failures ─────────────────────────────────────────────────────────────────


class EngineUnavailable(ServiceError):
    """The engine isn't running in this server: another server holds the
    lock (``engine_elsewhere``) or its store can't be used."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status=503)


class SessionNotFound(ServiceError):
    def __init__(self, session_id: str) -> None:
        super().__init__("session_not_found", f"There is no storm session {session_id!r}. List them with "
                                              "GET /api/chat/sessions.", status=404)


class SessionRefused(ServiceError):
    """A session can't be created or changed as asked (no runtime for the
    agent, a spec the runtime refuses, an unknown model)."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


class Unsupported(ServiceError):
    """The runtime's feature list doesn't offer this; ``feature`` is the key
    the UI looks up (ch 17 §1)."""

    def __init__(self, feature: str, message: str) -> None:
        super().__init__("unsupported", message, status=501)
        self.feature = feature


class WouldStopRuns(ServiceError):
    """"Send now" would stop live background runs (ch 07 §4.2, §0.68): the
    person confirms by sending ``count`` back."""

    def __init__(self, count: int) -> None:
        super().__init__("would_stop_runs", f"Sending now stops this session's {count} background task(s) "
                                            f"(sub-agents or shells). Send again confirming {count} to go ahead, "
                                            "or add to the turn instead.", status=409)
        self.count = count


class AlreadyDecided(ServiceError):
    """The item was decided already (a second click, a retry): a decision is
    single-use. ``record`` raises it from its compare-and-set on the item's
    open state; the engine raises it for a decision still being written."""

    def __init__(self, item_id: str) -> None:
        super().__init__("already_decided", f"Review item {item_id} was decided already; nothing more was sent.",
                         status=409)


class ReviewUnavailable(ServiceError):
    def __init__(self) -> None:
        super().__init__("review_unavailable", "XO can't ask the person here: the review queue isn't "
                                               "running in this server.", status=503)
