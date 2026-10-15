"""The person's rules as XO stores them (design ch 15 §3, §3.1; ch 08 §2):
the ``rules`` rows, and the revocations of XO's default rules.

Only the person makes a rule: from a decided item ("make it a rule", the
review queue) or in settings (``POST /api/chat/rules``). Either way the
sentence they saw must be the one the server renders, and the rule keeps the
tier it may cover. Rules are never written into a CLI's settings. A revoked
rule stays in the store (every decision is kept) and answers nothing.

Everything here runs inside a store transaction or a store read: plain SQL.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Mapping

from services.cowork_agent.storm.review.rules import DEFAULT_RULES, Match, Rule, Scope, render_sentence
from services.cowork_agent.storm.review.tiers import Tier
from services.errors import ServiceError

log = logging.getLogger(__name__)

DEFAULT_IDS = frozenset(d.id for d in DEFAULT_RULES)
#: Which action kinds each way of matching applies to.
MATCH_KINDS: Mapping[str, frozenset[str]] = {
    "command": frozenset({"command"}), "prefix": frozenset({"command"}),
    "path_glob": frozenset({"read", "search", "list", "edit_file", "create_file", "delete_file"}),
    "host": frozenset({"web_fetch"}), "mcp": frozenset({"mcp"}),
}
_COLUMNS = ("id, seq, scope, project_pid, thread_ts, tool, match, effect, sentence, tier, from_item, created_at, "
            "expires_at, revoked_at, uses")


class RuleRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(code, message, status=status)
        for key, value in extra.items():
            setattr(self, key, value)


def match_json(match: Match) -> dict[str, Any]:
    """The stored form: exactly one key."""
    for key in ("command", "prefix", "path_glob", "host", "mcp"):
        value = getattr(match, key)
        if value is not None:
            return {key: list(value) if isinstance(value, tuple) else value}
    raise ValueError("a rule needs something to match")


def match_of(data: Mapping[str, Any]) -> Match:
    return Match(**{k: tuple(v) if isinstance(v, list) else v for k, v in data.items()})


def widest_tier(scope: Scope, match: Match) -> Tier:
    """The tier a rule made in settings may cover: the most its scope allows
    (ch 15 §3: destructive only as this thread's exact command, notable at
    most a project, a workspace rule routine only)."""
    if scope == "workspace":
        return Tier.ROUTINE
    if scope == "thread" and match.command is not None:
        return Tier.DESTRUCTIVE
    return Tier.NOTABLE


def project_name(conn: sqlite3.Connection | Any, project_pid: str | None) -> str | None:
    """How a rule's sentence names its project: the project chat's name,
    else its pid."""
    if project_pid is None:
        return None
    row = conn.execute("SELECT project_name FROM chats WHERE project_pid = ?", (project_pid,)).fetchone()
    return (row[0] if row else None) or project_pid


def check_match(kind: str, match: Match) -> None:
    key = next(iter(match_json(match)))
    if kind not in MATCH_KINDS[key]:
        raise RuleRefused("bad_rule", f"A rule matching {key} applies to {', '.join(sorted(MATCH_KINDS[key]))} "
                                      f"actions, not {kind}.")
    words = match.command or match.prefix
    if words is not None and (not words or not all(isinstance(w, str) and w for w in words)):
        raise RuleRefused("bad_rule", "A command rule needs its parsed words, each one non-empty.")
    if match.mcp is not None and (len(match.mcp) != 2 or not all(match.mcp)):
        raise RuleRefused("bad_rule", "An MCP rule names its server and tool (\"*\" for every tool).")


def create(w, *, scope: Scope, kind: str, match: Match, effect: str, sentence: str, tier: Tier,
           project_pid: str | None = None, thread_ts: str | None = None, from_item: str | None = None,
           client_id: str | None = None) -> dict[str, Any]:
    """Store a rule and its ``rule.created`` event; returns its view."""
    rule_id = f"rule_{uuid.uuid4().hex[:16]}"
    view = {"id": rule_id, "scope": scope, "project_pid": project_pid, "thread_ts": thread_ts, "kind": kind,
            "match": match_json(match), "effect": effect, "sentence": sentence, "tier": tier.name.lower(),
            "from_item": from_item, "created_at": w.now, "expires_at": None, "revoked_at": None, "uses": 0}
    seq = w.append("rule.created", {"rule": view}, thread_ts=thread_ts)
    w.execute("INSERT INTO rules (id, seq, scope, project_pid, thread_ts, tool, match, effect, sentence, tier, "
              "from_item, client_id, created_at) VALUES (?, ?, ?, ?, ?, ?, json(?), ?, ?, ?, ?, ?, ?)",
              (rule_id, seq, scope, project_pid, thread_ts, kind, json.dumps(view["match"]), effect, sentence,
               view["tier"], from_item, client_id, w.now))
    return {**view, "seq": seq}


def by_client_id(conn, client_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM rules WHERE client_id = ?", (client_id,)).fetchone()
    return _view(row) if row else None


def create_from_settings(w, *, scope: Scope, kind: str, match: Match, effect: str, sentence: str,
                         project_pid: str | None, thread_ts: str | None, client_id: str) -> dict[str, Any]:
    """A rule the person made in settings. A retry with the same
    ``client_id`` returns the rule it made."""
    known = by_client_id(w, client_id)
    if known is not None:
        return known
    if (scope == "workspace" and (project_pid or thread_ts)) or (scope == "project" and (not project_pid or thread_ts)) \
            or (scope == "thread" and not thread_ts):
        raise RuleRefused("bad_rule", "A workspace rule names no project or thread; a project rule names its "
                                      "project; a thread rule names its thread.")
    check_match(kind, match)
    rendered = render_sentence(effect, scope, kind, match, project_name(w, project_pid))
    if sentence != rendered:
        raise RuleRefused("rule_sentence_mismatch", "The sentence isn't the one XO renders for this rule. Show "
                                                    "the person this one, then send it back.", sentence=rendered)
    return create(w, scope=scope, kind=kind, match=match, effect=effect, sentence=rendered,
                  tier=widest_tier(scope, match), project_pid=project_pid, thread_ts=thread_ts, client_id=client_id)


def revoke(w, rule_id: str) -> dict[str, Any]:
    """Revoke the person's rule, or one of XO's default rules. Revoking
    twice changes nothing."""
    if rule_id in DEFAULT_IDS:
        if w.execute("INSERT INTO default_rule_revocations (id, revoked_at) VALUES (?, ?) ON CONFLICT (id) "
                     "DO NOTHING", (rule_id, w.now)).rowcount:
            w.append("rule.revoked", {"rule": rule_id, "default": True})
        return default_views(w)[rule_id]
    row = w.execute(f"SELECT {_COLUMNS} FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise RuleRefused("rule_not_found", f"There is no rule {rule_id!r}. List them with GET /api/chat/rules.",
                          status=404)
    if row[13] is None:
        w.execute("UPDATE rules SET revoked_at = ? WHERE id = ?", (w.now, rule_id))
        w.append("rule.revoked", {"rule": rule_id, "default": False}, thread_ts=row[4])
        row = w.execute(f"SELECT {_COLUMNS} FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return _view(row)


def list_rules(conn, *, after_seq: int, limit: int) -> list[dict[str, Any]]:
    """The person's rules in force, oldest first, paged by seq."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM rules WHERE revoked_at IS NULL AND seq > ? ORDER BY seq LIMIT ?",
                        (after_seq, limit)).fetchall()
    return [_view(r) for r in rows]


def default_views(conn) -> dict[str, dict[str, Any]]:
    revoked = dict(conn.execute("SELECT id, revoked_at FROM default_rule_revocations").fetchall())
    return {d.id: {"id": d.id, "sentence": d.sentence, "default": True, "revoked_at": revoked.get(d.id)}
            for d in DEFAULT_RULES}


def for_policy(conn, project_pid: str | None) -> tuple[tuple[Rule, ...], frozenset[str]]:
    """What the policy decides a session's checks with: the rules in force
    that can apply to it (the workspace's and its project's), and the default
    rules the person revoked."""
    rows = conn.execute("SELECT id, scope, effect, tool, match, sentence, tier, project_pid, thread_ts, from_item, "
                        "expires_at FROM rules WHERE revoked_at IS NULL AND (scope = 'workspace' OR project_pid = ?)",
                        (project_pid,)).fetchall()
    rules = []
    for row in rows:
        try:
            rules.append(_rule(row))
        except (ValueError, TypeError, KeyError) as exc:
            # One bad allow rule must not turn every check into a host error;
            # a deny rule that can't be read fails the check closed.
            if row[2] != "allow":
                raise
            log.error("storm: allow rule %s can't be read (%s); it is ignored", row[0], exc)
    revoked = frozenset(r[0] for r in conn.execute("SELECT id FROM default_rule_revocations").fetchall())
    return tuple(rules), revoked


def _rule(row: tuple) -> Rule:
    rule_id, scope, effect, kind, match, sentence, tier, project_pid, thread_ts, from_item, expires_at = row
    return Rule(id=rule_id, scope=scope, effect=effect, kind=kind, match=match_of(json.loads(match)),
                sentence=sentence, tier=Tier[tier.upper()], project_pid=project_pid, thread_ts=thread_ts,
                from_item=from_item, expires_at=expires_at)


def _view(row: tuple) -> dict[str, Any]:
    (rule_id, seq, scope, project_pid, thread_ts, kind, match, effect, sentence, tier, from_item, created_at,
     expires_at, revoked_at, uses) = row
    return {"id": rule_id, "seq": seq, "scope": scope, "project_pid": project_pid, "thread_ts": thread_ts,
            "kind": kind, "match": json.loads(match), "effect": effect, "sentence": sentence, "tier": tier,
            "from_item": from_item, "created_at": created_at, "expires_at": expires_at, "revoked_at": revoked_at,
            "uses": uses}
