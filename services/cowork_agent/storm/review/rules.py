"""Rules: the person's standing answers, and XO's default rules (design ch 15
§3, §3.1; decision §0.62).

Only the person makes a rule, from a decided item or in settings; no agent
tool creates, widens or deletes one. A rule matches parsed words, never text:
the prefix ``["npm", "test"]`` matches ``npm test --watch`` and not
``npm testing``. Rules live in XO's store, never in a CLI's settings, and
none can allow a protected path (the policy checks those first).

Limits by tier: a destructive action can only ever be "this thread, this
exact command"; a notable one at most a project; only routine work may get a
workspace rule. An allow rule also keeps the tier of what it was made for and
never covers anything riskier: "allow ``git log``" doesn't allow
``git log --output=/elsewhere``.

XO's default rules ship on. Each covers a category of routine work
(``tiers.py``) and can be revoked on its own; the project's test command is
deliberately not one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

from services.cowork_agent.adapters.contract import ToolKind
from services.cowork_agent.storm.review.tiers import (
    COMMS,
    GIT_LOCAL,
    GIT_READ,
    KNOWN_HOST_GET,
    READ_INSIDE,
    XO_DOCUMENTED_GET,
    Part,
    Tier,
    canonical_words,
)

Scope = Literal["thread", "project", "workspace"]


@dataclass(frozen=True)
class Match:
    """Exactly one of these is set."""

    command: tuple[str, ...] | None = None     # the exact parsed words
    prefix: tuple[str, ...] | None = None      # the first parsed words
    path_glob: str | None = None               # relative to the session's folder; "dir/**" is the whole folder
    host: str | None = None
    mcp: tuple[str, str] | None = None         # (server, tool); tool "*" is every tool of the server

    def matches(self, part: Part) -> bool:
        if self.command is not None:
            return part.words == canonical_words(self.command)
        if self.prefix is not None:
            prefix = canonical_words(self.prefix)
            return part.words[:len(prefix)] == prefix
        if self.path_glob is not None:
            return part.path is not None and _glob(part.path, self.path_glob)
        if self.host is not None:
            return part.host == self.host.lower()
        if self.mcp is not None:
            return part.mcp is not None and part.mcp[0] == self.mcp[0] and self.mcp[1] in ("*", part.mcp[1])
        return False


@dataclass(frozen=True)
class Rule:
    id: str
    scope: Scope
    effect: Literal["allow", "deny"]
    kind: ToolKind
    match: Match
    sentence: str                              # exactly what the person saw
    tier: Tier                                 # the tier of what it was made for: it covers nothing riskier
    project_pid: str | None = None             # for "thread" and "project"
    thread_ts: str | None = None               # for "thread"
    from_item: str | None = None
    expires_at: str | None = None              # UTC, ending in Z

    def applies(self, kind: str, part: Part, *, project_pid: str | None, thread_ts: str | None, now: str) -> bool:
        if self.expires_at is not None and self.expires_at <= now:
            return False
        if self.scope in ("thread", "project") and self.project_pid != project_pid:
            return False
        if self.scope == "thread" and self.thread_ts != thread_ts:
            return False
        if self.effect == "allow" and part.tier > self.tier:
            return False                       # "allow git log" never covers git log --output=/elsewhere
        return self.kind == kind and self.match.matches(part)


@dataclass(frozen=True)
class DefaultRule:
    id: str
    sentence: str
    categories: frozenset[str]


DEFAULT_RULES = (
    DefaultRule("default:read-inside", "Read, search and list files in the session's own folder without asking",
                frozenset({READ_INSIDE})),
    DefaultRule("default:git-read", "Run read-only git commands without asking", frozenset({GIT_READ})),
    DefaultRule("default:git-local", "Run git add, git commit and git checkout -b in the worktree without asking",
                frozenset({GIT_LOCAL})),
    DefaultRule("default:comms", "Use XO's own comms tools without asking", frozenset({COMMS})),
    DefaultRule("default:known-hosts", "Read from package registries and documentation sites without asking",
                frozenset({KNOWN_HOST_GET})),
    DefaultRule("default:xo-routes", "Read the Inbox and backups through XO's API without asking",
                frozenset({XO_DOCUMENTED_GET})),
)


def scope_refusal(tier: Tier, scope: Scope, match: Match) -> str | None:
    """Why a rule this wide may not cover an action of ``tier``, or None."""
    if tier == Tier.DESTRUCTIVE and (scope != "thread" or match.command is None):
        return "a destructive action can only be allowed for this thread, for this exact command"
    if tier == Tier.NOTABLE and scope == "workspace":
        return "this action can be allowed at most for one project"
    return None


def render_sentence(rule_effect: str, scope: Scope, kind: str, match: Match, project_name: str | None) -> str:
    """The sentence a person sees for a rule, and must be shown before making
    it: "In billing-api, run `npm test` without asking"."""
    where = {"thread": "In this thread", "project": f"In {project_name or 'this project'}",
             "workspace": "In every project"}[scope]
    if match.command is not None:
        what = f"run exactly `{' '.join(match.command)}`"
    elif match.prefix is not None:
        what = f"run `{' '.join(match.prefix)}`"
    elif match.path_glob is not None:
        verb = {"read": "read", "search": "search", "list": "list", "delete_file": "delete",
                "create_file": "create"}.get(kind, "edit")
        what = f"{verb} files matching `{match.path_glob}`"
    elif match.host is not None:
        what = f"fetch from {match.host}"
    elif match.mcp is not None:
        server, tool = match.mcp
        what = f"use every {server} tool" if tool == "*" else f"use {server}/{tool}"
    else:
        raise ValueError("a rule needs something to match")
    if rule_effect == "deny":
        return f"{where}, never {what}"
    return f"{where}, {what} without asking"


def _glob(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        top = pattern[:-3]
        return path == top or path.startswith(top + "/")
    return fnmatchcase(path, pattern)
