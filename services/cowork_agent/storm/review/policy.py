"""The policy engine: what happens to an action a CLI asked about (design
ch 15 §2; decisions §0.61, §0.62, §0.75, §0.80).

Every check that reaches ``Host.decide_permission`` becomes an ``Action``,
built from the runtime's neutral fields only (never a CLI's own input keys).
The checks run in this order, first answer wins::

    deny rules → protected paths → one-time grants → allow rules → tier
       deny          deny               allow            allow     routine and a default rule: allow
                                                                   otherwise: ask the person

Every simple command of a compound must be allowed on its own for the whole
to be allowed. A command the parser can't see through is never allowed, only
denied by a rule or asked. Coordinators pass the deny rules and protected
paths like everyone, then never reach "ask": anything that isn't read-only is
denied, except a read that may touch credentials, which is asked (§0.75).
Grants and allow rules never widen a coordinator, nor cover a write XO can't
place (it may hit a protected path). The engine fails closed: if it breaks,
the answer is deny.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from services.cowork_agent.adapters.contract import PermissionRequest
from services.cowork_agent.storm.review.paths import Places
from services.cowork_agent.storm.review.rules import DEFAULT_RULES, Rule, scope_refusal
from services.cowork_agent.storm.review.shell import canonical_command
from services.cowork_agent.storm.review.tiers import (
    OTHER,
    PLAN_REASON,
    Assessment,
    Part,
    Tier,
    assess_command,
    assess_mcp,
    assess_paths,
    assess_url,
)

log = logging.getLogger(__name__)

Effect = Literal["allow", "deny", "ask"]
FILE_KINDS = frozenset({"read", "search", "list", "edit_file", "create_file", "delete_file"})
#: Kinds whose content defines the action: without the runtime's digest the
#: hash binds the raw input instead (a forgotten digest asks more, never less).
CONTENT_KINDS = frozenset({"edit_file", "create_file"})


@dataclass(frozen=True)
class SessionRef:
    id: str
    role: Literal["plain", "coordinator", "thread", "job"]
    cwd: Path
    project_pid: str | None = None
    thread_ts: str | None = None
    branch: str | None = None              # a thread's own branch


@dataclass(frozen=True)
class Action:
    kind: str
    tool: str
    session: SessionRef
    command: str | None = None
    paths: tuple[str, ...] = ()
    url: str | None = None
    mcp: tuple[str, str] | None = None
    raw: Mapping[str, Any] | None = None   # the CLI's own input, for display only
    cwd: Path | None = None
    content_sha256: str | None = None      # what else defines it (the runtime's digest)

    @classmethod
    def from_request(cls, req: PermissionRequest, session: SessionRef) -> "Action":
        return cls(kind=req.kind, tool=req.tool, session=session, command=req.command, paths=req.paths,
                   url=req.url, mcp=req.mcp, raw=req.input, cwd=Path(req.cwd) if req.cwd else None,
                   content_sha256=req.content_sha256)

    @property
    def sha256(self) -> str:
        """The action's canonical hash (a grant binds to it)."""
        return action_sha256(kind=self.kind, tool=self.tool, command=self.command, paths=self.paths, url=self.url,
                             mcp=self.mcp, content_sha256=self.content_sha256, raw=self.raw)


@dataclass(frozen=True)
class Grant:
    """One-time permission given on a parked call's approval: this session,
    this tool, this exact input, until it expires or is spent."""

    id: str
    session_id: str
    tool: str
    input_sha256: str
    expires_at: str
    used_at: str | None = None


@dataclass(frozen=True)
class PolicyContext:
    places: Places
    now: str                                       # UTC, ending in Z
    rules: tuple[Rule, ...] = ()
    revoked_defaults: frozenset[str] = frozenset()
    grants: tuple[Grant, ...] = ()
    xo_ports: frozenset[int] = frozenset({5002})


@dataclass(frozen=True)
class Verdict:
    effect: Effect
    tier: Tier
    reasons: tuple[str, ...] = ()
    message: str | None = None             # for the agent, when denied
    rules_used: tuple[str, ...] = ()       # the person's rules and default rules that answered
    grant_id: str | None = None            # the grant to spend
    assessment: Assessment | None = None   # on an ask: what was judged, for the review item's options


def action_sha256(*, kind: str, tool: str, command: str | None = None, paths: Iterable[str] = (),
                  url: str | None = None, mcp: tuple[str, str] | None = None,
                  content_sha256: str | None = None, raw: Mapping[str, Any] | None = None) -> str:
    """The hash of an action's canonical input (ch 15 §6.3): its neutral
    fields plus the runtime's digest of what else defines it, never a
    display-only field. One definition for a grant, the stored permission
    step and a decision's request, so "the same action" means one thing. The
    command is hashed as ``canonical_command`` gives it: whitespace the shell
    ignores never makes a new action, a quoted operator never matches a real
    one. A write or an edit without the runtime's digest binds its raw input
    (``raw``) instead, so a runtime that forgets the digest fails safe."""
    if not content_sha256 and kind in CONTENT_KINDS:
        raw_json = json.dumps(dict(raw or {}), sort_keys=True, separators=(",", ":"), default=str)
        content_sha256 = "raw:" + hashlib.sha256(raw_json.encode()).hexdigest()
    canonical = {"kind": kind, "tool": tool, "command": None if command is None else canonical_command(command),
                 "paths": list(paths), "url": url,
                 "mcp": list(mcp) if mcp else None, "content": content_sha256}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_sha256(req: PermissionRequest) -> str:
    return action_sha256(kind=req.kind, tool=req.tool, command=req.command, paths=req.paths, url=req.url,
                         mcp=req.mcp, content_sha256=req.content_sha256, raw=req.input)


def decide(action: Action, ctx: PolicyContext) -> Verdict:
    try:
        return _decide(action, ctx)
    except Exception:
        log.exception("policy: host error deciding a %s action for session %s", action.kind, action.session.id)
        return Verdict("deny", Tier.DESTRUCTIVE, ("host error",),
                       message="XO could not check this action (host error), so it was not allowed.")


def assess(action: Action, ctx: PolicyContext) -> Assessment:
    cwd = action.cwd or action.session.cwd
    if action.kind == "command":
        return assess_command(action.command or "", ctx.places, cwd, branch=action.session.branch,
                              xo_ports=ctx.xo_ports)
    if action.kind in FILE_KINDS:
        return assess_paths(action.kind, action.paths, ctx.places, cwd)
    if action.kind in ("web_fetch", "web_search"):
        return assess_url(action.kind, action.url, ctx.places, ctx.xo_ports)
    if action.kind == "mcp" and action.mcp:
        return assess_mcp(*action.mcp)
    if action.kind == "plan":
        return Assessment((Part(Tier.NOTABLE, OTHER, (PLAN_REASON,)),))
    return Assessment((Part(Tier.NOTABLE, OTHER, (f"XO doesn't know this kind of action ({action.kind})",)),))


def _decide(action: Action, ctx: PolicyContext) -> Verdict:
    found = assess(action, ctx)
    session = action.session

    def applying(effect: str, part: Part) -> Rule | None:
        for rule in ctx.rules:
            if rule.effect == effect and rule.applies(action.kind, part, project_pid=session.project_pid,
                                                      thread_ts=session.thread_ts, now=ctx.now):
                if effect == "deny" or scope_refusal(part.tier, rule.scope, rule.match) is None:
                    return rule
        return None

    for part in found.parts:
        rule = applying("deny", part)
        if rule:
            return Verdict("deny", found.tier, found.reasons, message=f"Not allowed by the person's rule: "
                           f"{rule.sentence}.", rules_used=(rule.id,))
    if found.protected:
        return Verdict("deny", Tier.DESTRUCTIVE, found.reasons, message=f"XO never allows writing "
                       f"{found.protected[0]}. Leave that file as it is.")
    if session.role == "coordinator":
        return _coordinator(action, found)
    if found.unverifiable:
        # It may write a protected path XO can't see: no grant or rule covers it.
        return Verdict("ask", found.tier, found.reasons, assessment=found)
    grant = _grant(action, ctx)
    if grant:
        return Verdict("allow", found.tier, found.reasons, grant_id=grant.id)
    if not found.refused:
        used = _cover(found.parts, applying, ctx.revoked_defaults)
        if used is not None:
            return Verdict("allow", found.tier, found.reasons, rules_used=used)
    return Verdict("ask", found.tier, found.reasons, assessment=found)


def _cover(parts: Iterable[Part], applying, revoked: frozenset[str]) -> tuple[str, ...] | None:
    """The rules that allow every part, or None if one part isn't covered."""
    used: list[str] = []
    for part in parts:
        rule = applying("allow", part)
        if rule is None and part.tier == Tier.ROUTINE:
            rule = next((d for d in DEFAULT_RULES if part.category in d.categories and d.id not in revoked), None)
        if rule is None:
            return None
        if rule.id not in used:
            used.append(rule.id)
    return tuple(used)


def _grant(action: Action, ctx: PolicyContext) -> Grant | None:
    digest = action.sha256
    return next((g for g in ctx.grants if g.session_id == action.session.id and g.tool == action.tool
                 and g.input_sha256 == digest and g.used_at is None and g.expires_at > ctx.now), None)


def _coordinator(action: Action, found: Assessment) -> Verdict:
    outside_tools = action.kind in ("web_fetch", "web_search") or (
        action.kind == "mcp" and not any(p.category == "comms" for p in found.parts))
    if found.refused or outside_tools or not found.read_only:
        why = found.reasons[0] if found.reasons else "it is not read-only"
        return Verdict("deny", found.tier, found.reasons,
                       message=f"A coordinator only reads and delegates; this was not allowed ({why}). "
                               "Ask one of your workers or a thread to do it.")
    if any(p.secret_read for p in found.parts):
        return Verdict("ask", Tier.DESTRUCTIVE, found.reasons, assessment=found)
    return Verdict("allow", found.tier, found.reasons)
