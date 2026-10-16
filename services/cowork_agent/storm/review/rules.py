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

import shlex
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

from services.cowork_agent.adapters.contract import ToolKind
from services.cowork_agent.storm.review.tiers import (
    COMMS,
    DOWNLOAD_AND_RUN,
    PACKAGE_MANAGERS,
    SYSTEM_PACKAGES,
    GIT_LOCAL,
    GIT_READ,
    KNOWN_HOST_GET,
    READ_INSIDE,
    XO_DOCUMENTED_GET,
    Part,
    Tier,
    canonical_words,
    is_flag_run,
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

    def matches(self, part: Part, *, at_least: bool = False) -> bool:
        """A command is compared as its program, its positional words and its
        flags, wherever the flags stand (``rm -f src -r`` has the flags
        ``-f -r``). ``at_least`` (deny rules): a command carrying more flags
        than the rule still matches. Otherwise (allow rules) its short flags
        must be exactly the rule's, so "allow ``rm -f``" never covers a
        recursive delete; long flags may be added (``npm test --watch``)."""
        if not part.words:
            return False
        if self.command is not None:
            return _shape(part.words) == _shape(canonical_words(self.command))
        if self.prefix is not None:
            return _starts_with(part.words, canonical_words(self.prefix), at_least)
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
        return self.kind == kind and self.match.matches(part, at_least=self.effect == "deny")


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


#: Command families XO offers only exact-command rules for, never a prefix
#: rule: the writes B1 recorded as residuals (B1 handoff §2c) need the
#: person's own grant or exact rule first, and a prefix rule such as "run
#: `git clone` without asking" would let one through unasked (`git clone .
#: .claude/x`). Backup suffixes (cp, mv, ln, install), `sed -i`, the git
#: commands that write trees or run hooks, package managers and their
#: destinations, `pip` and `python -m`, and `gh`; and the git commands that
#: take `--upload-pack` or `--exec` (B1 §2c rates those notable, not inline code).
EXACT_RULES_ONLY = frozenset({"cp", "mv", "ln", "install", "sed", "gh", "python", "python3", "pip", "pip3",
                              *PACKAGE_MANAGERS, *DOWNLOAD_AND_RUN, *SYSTEM_PACKAGES})
EXACT_RULES_ONLY_GIT = frozenset({"clone", "init", "worktree", "submodule", "apply", "am", "checkout",
                                  "checkout-index", "restore", "format-patch", "rebase", "fetch", "pull", "ls-remote",
                                  "archive"})


def prefix_rules_allowed(words: tuple[str, ...]) -> bool:
    """Whether a prefix rule may be offered for a command with these
    (canonical) words."""
    if not words or words[0] in EXACT_RULES_ONLY:
        return False
    return not (words[0] == "git" and len(words) > 1 and words[1] in EXACT_RULES_ONLY_GIT)


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
        what = f"run exactly `{shlex.join(match.command)}`"
    elif match.prefix is not None:
        what = f"run `{shlex.join(match.prefix)}`"
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


def _shape(words: tuple[str, ...]) -> tuple[str, tuple[str, ...], frozenset[str], frozenset[str]]:
    """(program, positional words, short-flag letters, long flags)."""
    positional, letters, longs = [], set(), set()
    for word in words[1:]:
        if is_flag_run(word):
            letters.update(word[1:])
        elif word.startswith("--") and len(word) > 2:
            longs.add(word)
        else:
            positional.append(word)
    return words[0], tuple(positional), frozenset(letters), frozenset(longs)


def _starts_with(words: tuple[str, ...], prefix: tuple[str, ...], at_least: bool) -> bool:
    program, positional, letters, longs = _shape(words)
    want_program, want_positional, want_letters, want_longs = _shape(prefix)
    if program != want_program or positional[:len(want_positional)] != want_positional:
        return False
    if at_least:
        names = {flag.split("=", 1)[0] for flag in longs}
        return want_letters <= letters and {flag.split("=", 1)[0] for flag in want_longs} <= names
    return letters == want_letters and want_longs <= longs


def _glob(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        top = pattern[:-3]
        return path == top or path.startswith(top + "/")
    return fnmatchcase(path, pattern)
