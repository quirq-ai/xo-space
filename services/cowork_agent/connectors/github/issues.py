"""One repo's open GitHub issues, read through ``gh api graphql``."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from utils.commands import CommandResult, run

from .common import get_github_token

GH_BIN = "gh"

#: A poll must not outlive its interval. 20 s against a 60 s poll leaves room
#: for the kill-and-reap path below to finish before the next tick.
GH_TIMEOUT_S = 20.0

#: GitHub's own maximum for a connection page. Larger is rejected by the API,
#: not silently clamped, so the clamp is here.
MAX_PAGE_SIZE = 100

#: Fifty, not a hundred, so that labels fit the one-point budget. GitHub
#: prices a query by the connections it could have to walk, divided by 100
#: and rounded: 1 for ``issues``, then one per issue for each nested
#: connection (``assignees``, ``labels``). At 50 issues that is 1 + 50 + 50,
#: plus the pull requests below, and it rounds to 1; at 100 it is 2
#: (measured with ``rateLimit(dryRun: true)`` on 2026-11-26).
DEFAULT_PAGE_SIZE = 50

#: Pull requests ride the first page of a poll, never a continuation page.
#: Open ones carry their head commit's check state, which is one more nested
#: connection each (``commits(last: 1)``); recently merged or closed ones
#: carry nothing nested. Measured with 50 issues: 40 open + 20 recent costs 1
#: point, 48 open costs 2. ``comments { totalCount }`` is free: a connection
#: asked only for its count is never walked.
OPEN_PULLS_PAGE = 40
RECENT_PULLS_PAGE = 20

#: The measured cost of one poll, first page included, in GraphQL points.
MAX_QUERY_COST = 1

_ISSUE_FIELDS = """
        id
        number
        title
        state
        stateReason
        url
        updatedAt
        author { login avatarUrl }
        comments { totalCount }
        assignees(first: 5) { nodes { login avatarUrl } }
        labels(first: 10) { nodes { name } }"""

_PULL_FIELDS = """
        id
        number
        title
        state
        isDraft
        url
        updatedAt
        author { login avatarUrl }
        headRefName
        baseRefName
        headRefOid
        baseRef { target { oid } }
        reviewDecision
        mergeable
        additions
        deletions
        changedFiles
        comments { totalCount }"""

_PULLS_SELECTION = f"""
    openPulls: pullRequests(
      first: {OPEN_PULLS_PAGE}
      states: [OPEN]
      orderBy: {{ field: UPDATED_AT, direction: DESC }}
    ) {{
      totalCount
      nodes {{{_PULL_FIELDS}
        commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}
      }}
    }}
    recentPulls: pullRequests(
      first: {RECENT_PULLS_PAGE}
      states: [CLOSED, MERGED]
      orderBy: {{ field: UPDATED_AT, direction: DESC }}
    ) {{
      nodes {{{_PULL_FIELDS}
      }}
    }}"""

_QUERY_TEMPLATE = """
query($owner: String!, $name: String!, $first: Int!, $after: String, $since: DateTime) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    hasIssuesEnabled
    issues(
      first: $first
      after: $after
      STATES
      filterBy: { since: $since }
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {ISSUE_FIELDS
      }
    }PULLS
  }
}
"""


def _issues_query(states: str, *, pulls: bool) -> str:
    return (_QUERY_TEMPLATE.replace("STATES", states)
            .replace("ISSUE_FIELDS", _ISSUE_FIELDS)
            .replace("PULLS", _PULLS_SELECTION if pulls else ""))


#: The pinned query: open issues only, for a mirror's first (seeding) poll.
ISSUES_QUERY = _issues_query("states: [OPEN]", pulls=False)

#: The steady-state twin of :data:`ISSUES_QUERY`, identical in every respect
#: but one: ``states: [OPEN, CLOSED]``. Seeding on ``[OPEN, CLOSED]`` would
#: drag the repository's entire closed history through the page budget;
#: ``since`` is what bounds the volume, and it does not exist on the first
#: poll.
ISSUES_QUERY_WITH_CLOSED = _issues_query("states: [OPEN, CLOSED]", pulls=False)

#: The same two, as a poll's first page asks them: with the pull requests.
ISSUES_AND_PULLS_QUERY = _issues_query("states: [OPEN]", pulls=True)
ISSUES_AND_PULLS_QUERY_WITH_CLOSED = _issues_query("states: [OPEN, CLOSED]", pulls=True)

#: The closed vocabulary of failure states.
ERROR_KINDS: tuple[str, ...] = (
    "no_cli",             # gh is not installed on this machine
    "not_authenticated",  # no gh session and no stored token, or it was revoked
    "forbidden",          # authenticated, but this token cannot see this repo
    "not_found",          # repo deleted, renamed, transferred, or never existed
    "rate_limited",       # primary or secondary GraphQL rate limit
    "network",            # DNS/TLS/connection failure, or GitHub returned 5xx
    "timeout",            # gh did not answer inside timeout_s
    "bad_remote",         # the remote URL is not a GitHub owner/repo
    "bad_response",       # gh answered with something this module cannot parse
    "unknown",            # gh failed in a way not classified above
)

#: Every kind in :data:`ERROR_KINDS`, as the HTTP answer a route gives it:
#: ``(status, code)``. A ``not_found`` route names its own code.
ERROR_ANSWERS: dict[str, tuple[int, str]] = {
    "no_cli": (503, "github_unavailable"),
    "not_authenticated": (503, "github_not_connected"),
    "forbidden": (403, "github_forbidden"),
    "not_found": (404, "issue_not_found"),
    "rate_limited": (503, "github_rate_limited"),
    "network": (502, "github_unavailable"),
    "timeout": (502, "github_unavailable"),
    "bad_remote": (400, "not_a_github_project"),
    "bad_response": (502, "github_unavailable"),
    "unknown": (502, "github_unavailable"),
}

#: GraphQL enum → the lowercase vocabulary §5.4 pins to GitHub's own.
_STATES = {"OPEN": "open", "CLOSED": "closed"}
#: An unrecognised reason becomes ``None`` rather than being coerced into the
#: nearest neighbour.
_STATE_REASONS = {
    "COMPLETED": "completed",
    "NOT_PLANNED": "not_planned",
    "REOPENED": "reopened",
}

#: Substrings that identify a transport failure in gh's stderr. gh wraps Go's
#: net errors verbatim, so these are the strings it actually prints.
_NETWORK_MARKERS = (
    "no such host", "dial tcp", "connection refused", "network is unreachable",
    "i/o timeout", "timeout awaiting", "tls", "eof", "connection reset",
    "temporary failure in name resolution", "proxyconnect",
)
_AUTH_MARKERS = (
    "gh auth login", "not logged in", "authentication token",
    "bad credentials", "requires authentication",
)
_RATE_MARKERS = ("rate limit", "secondary rate", "abuse detection")


# ---------------------------------------------------------------------------
# Remote URL → owner/repo
# ---------------------------------------------------------------------------

#: Anything with a scheme: ``https://host/owner/repo.git``,
#: ``ssh://git@host/owner/repo.git``, ``git://host/owner/repo.git``.
_URL_RE = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://)?"
    r"(?:(?P<userinfo>[^/@]+)@)?"
    r"(?P<host>[^/:]+)(?::\d+)?"
    r"/(?P<path>.+)$"
)

#: ``git@host:owner/repo.git`` — the scp-short form, which has no ``//`` and
#: therefore no authority to parse.
_SCP_RE = re.compile(r"^(?:(?P<userinfo>[^/@]+)@)?(?P<host>[^/:]+):(?P<path>.+)$")

#: What GitHub accepts in an owner or repository name.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class RepoRef:
    """A parsed remote: which host, which ``owner/name``."""

    host: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def is_github_com(self) -> bool:
        """Whether this is github.com proper, as opposed to an Enterprise host."""
        host = self.host.lower()
        return host == "github.com" or host.endswith(".github.com")

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.slug


def parse_remote_url(url: str | None) -> RepoRef | None:
    """``project.json:git.remote_url`` → :class:`RepoRef`, or ``None``."""
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    # A scheme decides which grammar applies.
    match = _URL_RE.match(text) if "://" in text else _SCP_RE.match(text)
    if match is None:
        return None
    host = match.group("host").strip()
    path = match.group("path").strip().strip("/")
    if not host or not path:
        return None
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2:
        # Exactly ``owner/repo`` and nothing else.
        return None
    owner, name = parts
    if not _NAME_RE.match(owner) or not _NAME_RE.match(name):
        return None
    return RepoRef(host=host, owner=owner, name=name)


def _coerce_ref(value: str) -> RepoRef | None:
    """A :class:`RepoRef` from either a remote URL or a bare ``owner/name``."""
    text = (value or "").strip()
    if not text:
        return None
    if "://" not in text and "@" not in text and ":" not in text:
        parts = [p for p in text.strip("/").split("/") if p]
        if len(parts) == 2 and all(_NAME_RE.match(p) for p in parts):
            return RepoRef(host="github.com", owner=parts[0], name=parts[1])
    return parse_remote_url(text)


def parse_repo_slug(url: str | None) -> str | None:
    """
    ``owner/repo`` for a remote URL, or ``None``. Convenience over
    :func:`parse_remote_url` for callers that only want the slug.
    """
    ref = parse_remote_url(url)
    return ref.slug if ref else None


# ---------------------------------------------------------------------------
# The cost contract, made checkable
# ---------------------------------------------------------------------------

#: A field selection carrying a pagination argument — i.e. a *connection*, the
#: only construct that adds to a query's point cost.
_CONNECTION_RE = re.compile(
    r"\b(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(?P<args>[^()]*)\)", re.DOTALL
)
_PAGE_ARG_RE = re.compile(r"(?<![$\w])(?:first|last)\s*:\s*(?P<value>\$?[A-Za-z0-9_]+)")


def query_connections(query: str = ISSUES_QUERY) -> tuple[tuple[str, str], ...]:
    """Every paginated connection in ``query``, as ``(field, page size)``."""
    out: list[tuple[str, str]] = []
    for match in _CONNECTION_RE.finditer(query):
        page = _PAGE_ARG_RE.search(match.group("args"))
        if page:
            out.append((match.group("field"), page.group("value")))
    return tuple(sorted(out))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RateLimit:
    """The GraphQL budget as GitHub reported it *inside the response*."""

    limit: int | None = None
    cost: int | None = None
    remaining: int | None = None
    reset_at: str | None = None

    @property
    def known(self) -> bool:
        return self.remaining is not None and self.reset_at is not None

    def as_document(self) -> dict[str, Any] | None:
        """The ``rate`` value for the mirror, or ``None`` when unknown."""
        if not self.known:
            return None
        doc: dict[str, Any] = {"remaining": self.remaining, "reset_at": self.reset_at}
        if self.limit is not None:
            doc["limit"] = self.limit
        if self.cost is not None:
            doc["cost"] = self.cost
        return doc


@dataclass(frozen=True)
class IssuesResult:
    """One poll's outcome — never an exception, always one of these."""

    ok: bool
    repo: str | None
    fetched_at: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    rate: RateLimit = field(default_factory=RateLimit)
    error_kind: str | None = None
    error: str | None = None
    has_next_page: bool = False
    end_cursor: str | None = None
    #: ``repository.hasIssuesEnabled`` — whether the issue tracker is turned on
    #: for this repository at all.
    issues_enabled: bool | None = None
    #: The pull requests, on a poll's first page only: ``None`` means this page
    #: did not ask (a continuation page, or a failure), never "there are none".
    pulls: list[dict[str, Any]] | None = None
    #: How many pull requests are open on GitHub, which may exceed the open
    #: rows in :attr:`pulls` (capped at :data:`OPEN_PULLS_PAGE`).
    open_pulls_total: int | None = None

    @property
    def high_water_mark(self) -> str | None:
        """The newest ``updated_at`` in this page, for the next ``since``."""
        stamps = [i["updated_at"] for i in self.issues if i.get("updated_at")]
        return max(stamps) if stamps else None

    def issues_by_node_id(self) -> dict[str, dict[str, Any]]:
        """The rows keyed the way the mirror keys them (§5.2)."""
        return {i["node_id"]: i for i in self.issues if i.get("node_id")}

    def error_document(self) -> dict[str, Any] | None:
        """The ``error`` value for the mirror, or ``None`` on success."""
        if self.ok or not self.error_kind:
            return None
        return {
            "kind": self.error_kind,
            "message": self.error or self.error_kind,
            "at": self.fetched_at,
        }


def _failure(
    repo: str | None,
    kind: str,
    message: str,
    rate: RateLimit | None = None,
) -> IssuesResult:
    return IssuesResult(
        ok=False,
        repo=repo,
        fetched_at=_utc_now(),
        rate=rate or RateLimit(),
        error_kind=kind,
        error=message,
    )


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def gh_available(gh_bin: str = GH_BIN) -> bool:
    """Whether the ``gh`` binary is on PATH. Never raises."""
    try:
        return shutil.which(gh_bin) is not None
    except Exception:
        return False


def _subprocess_env() -> dict[str, str]:
    """The environment for ``gh``, with the stored token injected if needed."""
    env = os.environ.copy()
    if env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
        return env
    try:
        token = get_github_token()
    except Exception:
        token = None
    if token:
        env["GH_TOKEN"] = token
    return env


async def _run_gh(argv: list[str], timeout_s: float) -> CommandResult:
    """One ``gh`` invocation, through the one executor (DEVELOPING.md §7).

    ``separate_stderr`` because this parses stdout as JSON while gh writes its
    warnings to stderr. The runner closes stdin, so gh can never block on a
    prompt, and a timeout kills the whole process group.
    """
    return await run(
        argv, timeout=timeout_s, env=_subprocess_env(), separate_stderr=True,
    )


def _parse_rate(payload: Any) -> RateLimit:
    """``data.rateLimit`` → :class:`RateLimit`, tolerating any shape."""
    if not isinstance(payload, dict):
        return RateLimit()
    node = payload.get("data")
    node = node.get("rateLimit") if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return RateLimit()

    def _int(key: str) -> int | None:
        value = node.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    reset = node.get("resetAt")
    return RateLimit(
        limit=_int("limit"),
        cost=_int("cost"),
        remaining=_int("remaining"),
        reset_at=reset if isinstance(reset, str) else None,
    )


def _classify_graphql_errors(errors: list[Any]) -> tuple[str, str] | None:
    """The first GraphQL error, as ``(kind, message)``."""
    for item in errors:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        etype = str(item.get("type") or "").upper()
        lowered = message.lower()
        if etype == "NOT_FOUND":
            return "not_found", message or "Repository not found."
        if etype == "RATE_LIMITED" or any(m in lowered for m in _RATE_MARKERS):
            return "rate_limited", message or "GitHub rate limit reached."
        if etype == "FORBIDDEN":
            return "forbidden", message or "Access to this repository is forbidden."
        if etype in ("UNAUTHORIZED", "UNAUTHENTICATED"):
            return "not_authenticated", message or "GitHub credentials rejected."
        if message:
            return "unknown", message
    return None


def _classify_rest_error(payload: dict[str, Any]) -> tuple[str, str] | None:
    """A REST-shaped error body, which gh emits for transport-level failures."""
    message = payload.get("message")
    if not isinstance(message, str):
        return None
    status = payload.get("status")
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = 0
    lowered = message.lower()
    if code == 401 or "bad credentials" in lowered:
        return "not_authenticated", message
    if code == 403 or code == 429:
        if any(m in lowered for m in _RATE_MARKERS):
            return "rate_limited", message
        return "forbidden", message
    if code == 404:
        return "not_found", message
    if code >= 500:
        return "network", f"GitHub returned HTTP {code}: {message}"
    return "unknown", message


def _classify_stderr(stderr: str, returncode: int | None) -> tuple[str, str]:
    """Last resort: gh failed without a parseable body on stdout."""
    text = (stderr or "").strip()
    lowered = text.lower()
    # gh documents exit status 4 as "authentication required"; it is the code
    # for a machine with no gh session and no token in the environment.
    if returncode == 4 or any(m in lowered for m in _AUTH_MARKERS):
        return "not_authenticated", text or "GitHub CLI is not authenticated."
    if any(m in lowered for m in _RATE_MARKERS):
        return "rate_limited", text or "GitHub rate limit reached."
    if any(m in lowered for m in _NETWORK_MARKERS):
        return "network", text or "Could not reach GitHub."
    return "unknown", text or f"`gh api graphql` exited with status {returncode}."


@dataclass(frozen=True)
class GhResult:
    """
    One ``gh api`` call, already classified. ``ok`` carries ``data``, else
    ``kind``/``message`` (the :data:`ERROR_KINDS` vocabulary). ``rate`` is set
    on both paths — a failed call still spent the point the budget tracks.
    """

    ok: bool
    data: dict[str, Any] | None = None
    kind: str = ""
    message: str = ""
    rate: RateLimit = field(default_factory=RateLimit)


def _spawn_failure(result: CommandResult, timeout_s: float, label: str):
    """The three ways a ``gh`` call fails before it ever answers."""
    if result.binary_missing:
        return GhResult(False, kind="no_cli",
                        message="GitHub CLI (`gh`) could not be executed.")
    if result.timed_out:
        return GhResult(False, kind="timeout",
                        message=f"`{label}` did not answer within {timeout_s:g}s.")
    if result.exception:
        return GhResult(False, kind="unknown",
                        message=f"Could not run `gh`: {result.exception}")
    return None


def _parse_json(text: str) -> Any:
    """``gh``'s stdout as JSON, or ``None``: an unreadable body is classified
    further down, never raised here."""
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _exit_failure(result: CommandResult, payload: Any, rate: RateLimit):
    """A non-zero exit, read from the REST error body if there is one, else
    from stderr."""
    if result.returncode == 0:
        return None
    if isinstance(payload, dict):
        classified = _classify_rest_error(payload)
        if classified:
            return GhResult(False, kind=classified[0], message=classified[1], rate=rate)
    kind, message = _classify_stderr(result.stderr, result.returncode)
    return GhResult(False, kind=kind, message=message, rate=rate)


async def run_graphql(argv: list[str], *, timeout_s: float) -> GhResult:
    """
    One GraphQL query, with every way it can fail classified in one place: an
    unrunnable binary, a timeout, a GraphQL ``errors`` array, a REST-shaped
    error body, a bare non-zero exit, or a reply that is not JSON.
    """
    result = await _run_gh(argv, timeout_s)
    failed = _spawn_failure(result, timeout_s, "gh api graphql")
    if failed is not None:
        return failed

    payload = _parse_json(result.stdout)
    rate = _parse_rate(payload)

    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            classified = _classify_graphql_errors(errors)
            if classified:
                return GhResult(False, kind=classified[0],
                                message=classified[1], rate=rate)
        if "data" not in payload:
            classified = _classify_rest_error(payload)
            if classified:
                return GhResult(False, kind=classified[0],
                                message=classified[1], rate=rate)

    failed = _exit_failure(result, payload, rate)
    if failed is not None:
        return failed

    if not isinstance(payload, dict):
        return GhResult(False, kind="bad_response",
                        message="`gh api graphql` returned no JSON body.", rate=rate)

    data = payload.get("data")
    return GhResult(True, data=data if isinstance(data, dict) else {}, rate=rate)


async def run_rest(argv: list[str], *, timeout_s: float, label: str) -> GhResult:
    """``gh api <endpoint>`` — a bare JSON object, no ``data`` envelope and no
    budget in the body. Same failure vocabulary as :func:`run_graphql`."""
    result = await _run_gh(argv, timeout_s)
    failed = _spawn_failure(result, timeout_s, label)
    if failed is not None:
        return failed

    payload = _parse_json(result.stdout)
    failed = _exit_failure(result, payload, RateLimit())
    if failed is not None:
        return failed

    if not isinstance(payload, dict):
        return GhResult(False, kind="bad_response",
                        message=f"`{label}` returned no JSON body.")
    return GhResult(True, data=payload)


def _int(value: Any) -> int | None:
    """A GraphQL Int, or ``None``: a bool is never a count."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def person(node: Any) -> dict[str, Any] | None:
    """``{login, avatarUrl}`` → ``{login, avatar_url}``, or ``None`` for a
    missing or deleted account (GitHub's "ghost" answers ``author: null``)."""
    if not isinstance(node, dict):
        return None
    login = _text(node.get("login"))
    if login is None:
        return None
    avatar = node.get("avatarUrl")
    return {"login": login, "avatar_url": avatar if isinstance(avatar, str) else None}


def total_count(node: Any) -> int | None:
    """``{totalCount}`` → the count, or ``None``."""
    return _int(node.get("totalCount")) if isinstance(node, dict) else None


def label_names(node: Any) -> list[str] | None:
    """``labels { nodes { name } }`` → names, or ``None`` when not asked."""
    if not isinstance(node, dict) or not isinstance(node.get("nodes"), list):
        return None
    names: list[str] = []
    for label in node["nodes"]:
        name = label.get("name") if isinstance(label, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


#: GraphQL's PullRequestState → the mirror's lowercase words.
PULL_STATES = {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}
#: The enums the mirror keeps verbatim, so an unrecognised value is dropped
#: rather than passed to a client that switches on it.
CHECK_STATES = frozenset({"SUCCESS", "FAILURE", "ERROR", "PENDING", "EXPECTED"})
REVIEW_DECISIONS = frozenset({"REVIEW_REQUIRED", "CHANGES_REQUESTED", "APPROVED"})
MERGEABLE_STATES = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})


def _enum(value: Any, allowed: frozenset[str]) -> str | None:
    text = str(value or "").upper()
    return text if text in allowed else None


def head_check_state(node: Any) -> str | None:
    """``commits(last: 1) { … statusCheckRollup { state } }`` → the state."""
    commits = node.get("commits") if isinstance(node, dict) else None
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if not isinstance(nodes, list) or not nodes or not isinstance(nodes[-1], dict):
        return None
    commit = nodes[-1].get("commit")
    rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else None
    return _enum(rollup.get("state"), CHECK_STATES) if isinstance(rollup, dict) else None


def base_tip(node: Any) -> str | None:
    """The base branch's tip now, which is what GitHub merges against.
    ``baseRefOid`` is not it: that is the base as of the pull request's last
    push, and a merge against it can be clean where GitHub says it conflicts."""
    ref = node.get("baseRef") if isinstance(node, dict) else None
    target = ref.get("target") if isinstance(ref, dict) else None
    return _text(target.get("oid")) if isinstance(target, dict) else None


def pull_row(node: Any) -> dict[str, Any] | None:
    """One GraphQL pull request node → one mirror row, or ``None``."""
    if not isinstance(node, dict):
        return None
    node_id = _text(node.get("id"))
    number = _int(node.get("number"))
    state = PULL_STATES.get(str(node.get("state") or "").upper())
    if node_id is None or number is None or number < 1 or state is None:
        return None
    return {
        "node_id": node_id,
        "number": number,
        "title": str(node.get("title") or ""),
        "state": state,
        "is_draft": node.get("isDraft") is True,
        "url": node.get("url") if isinstance(node.get("url"), str) else "",
        "updated_at": node.get("updatedAt") if isinstance(node.get("updatedAt"), str) else "",
        "author": person(node.get("author")),
        "head": _text(node.get("headRefName")),
        "base": _text(node.get("baseRefName")),
        "head_oid": _text(node.get("headRefOid")),
        "base_oid": base_tip(node),
        "checks": head_check_state(node),
        "review_decision": _enum(node.get("reviewDecision"), REVIEW_DECISIONS),
        # A closed pull request has no merge to compute; GitHub answers
        # UNKNOWN for it, which is what the mirror says too.
        "mergeable": _enum(node.get("mergeable"), MERGEABLE_STATES) or "UNKNOWN",
        "additions": _int(node.get("additions")),
        "deletions": _int(node.get("deletions")),
        "changed_files": _int(node.get("changedFiles")),
        "comment_count": total_count(node.get("comments")),
    }


def _pull_rows(repository: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, int | None]:
    """``(rows, open total)`` from a first page, or ``(None, None)`` when the
    page did not ask for pull requests."""
    if "openPulls" not in repository and "recentPulls" not in repository:
        return None, None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alias in ("openPulls", "recentPulls"):
        holder = repository.get(alias)
        nodes = holder.get("nodes") if isinstance(holder, dict) else None
        for node in nodes or []:
            row = pull_row(node)
            if row is not None and row["node_id"] not in seen:
                seen.add(row["node_id"])
                rows.append(row)
    return rows, total_count(repository.get("openPulls"))


def _issue_row(node: Any) -> dict[str, Any] | None:
    """One GraphQL issue node → one mirror row, or ``None`` if unusable."""
    if not isinstance(node, dict):
        return None
    node_id = node.get("id")
    number = node.get("number")
    if not isinstance(node_id, str) or not node_id:
        return None
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    state = _STATES.get(str(node.get("state") or "").upper())
    if state is None:
        return None
    reason_raw = node.get("stateReason")
    assignees: list[dict[str, Any]] = []
    holder = node.get("assignees")
    nodes = holder.get("nodes") if isinstance(holder, dict) else None
    for who in nodes or []:
        assignee = person(who)
        if assignee is not None:
            assignees.append(assignee)
    updated = node.get("updatedAt")
    url = node.get("url")
    return {
        "node_id": node_id,
        "number": number,
        "title": str(node.get("title") or ""),
        "state": state,
        "state_reason": _STATE_REASONS.get(str(reason_raw or "").upper()),
        "assignees": assignees,
        "url": url if isinstance(url, str) else "",
        "updated_at": updated if isinstance(updated, str) else "",
        "author": person(node.get("author")),
        "comment_count": total_count(node.get("comments")),
        "labels": label_names(node.get("labels")),
    }


async def fetch_open_issues(
    repo: RepoRef | str,
    *,
    since: str | None = None,
    after: str | None = None,
    first: int = DEFAULT_PAGE_SIZE,
    include_closed: bool = False,
    include_pulls: bool = False,
    timeout_s: float = GH_TIMEOUT_S,
    gh_bin: str = GH_BIN,
) -> IssuesResult:
    """One repo's issues, newest-updated first, and with ``include_pulls`` its
    open and recently closed pull requests in the same query. **Never raises.**"""
    ref = repo if isinstance(repo, RepoRef) else _coerce_ref(str(repo or ""))
    if ref is None:
        return _failure(
            None,
            "bad_remote",
            f"Not a GitHub owner/repo: {repo!r}." if repo else "No repository given.",
        )
    slug = ref.slug

    if not gh_available(gh_bin):
        return _failure(
            slug,
            "no_cli",
            "GitHub CLI (`gh`) is not installed on this machine. "
            "Install it from https://cli.github.com/ — GitHub polling is "
            "disabled until then.",
        )

    page = max(1, min(int(first) if isinstance(first, int) else DEFAULT_PAGE_SIZE,
                      MAX_PAGE_SIZE))
    if include_pulls:
        query = ISSUES_AND_PULLS_QUERY_WITH_CLOSED if include_closed else ISSUES_AND_PULLS_QUERY
    else:
        query = ISSUES_QUERY_WITH_CLOSED if include_closed else ISSUES_QUERY
    argv = [
        gh_bin, "api", "graphql",
        "-f", f"query={query}",
        "-f", f"owner={ref.owner}",
        "-f", f"name={ref.name}",
        "-F", f"first={page}",
    ]
    if not ref.is_github_com:
        argv[3:3] = ["--hostname", ref.host]
    # Omitted rather than passed empty: a null GraphQL variable disables the
    # filter, an empty string is a malformed DateTime and fails the query.
    if since:
        argv += ["-f", f"since={since}"]
    if after:
        argv += ["-f", f"after={after}"]

    answer = await run_graphql(argv, timeout_s=timeout_s)
    if not answer.ok:
        return _failure(slug, answer.kind, answer.message, answer.rate)
    rate = answer.rate

    repository = (answer.data or {}).get("repository")
    if repository is None:
        return _failure(
            slug, "not_found",
            f"Repository {slug} is not visible to this GitHub account.", rate,
        )
    connection = repository.get("issues") if isinstance(repository, dict) else None
    if not isinstance(connection, dict):
        return _failure(
            slug, "bad_response",
            "`gh api graphql` returned a repository with no issues connection.",
            rate,
        )

    nodes = connection.get("nodes")
    rows = [row for row in (_issue_row(n) for n in nodes or []) if row is not None]
    info = connection.get("pageInfo")
    info = info if isinstance(info, dict) else {}
    cursor = info.get("endCursor")
    pulls, open_pulls_total = _pull_rows(repository) if include_pulls else (None, None)

    return IssuesResult(
        ok=True,
        repo=slug,
        fetched_at=_utc_now(),
        issues=rows,
        rate=rate,
        has_next_page=bool(info.get("hasNextPage")),
        end_cursor=cursor if isinstance(cursor, str) else None,
        # Absent (an older cached response, or a schema that stopped offering
        # it) reads as ``None`` — "unknown" — never as ``False``, which would
        # claim the tracker is off.
        issues_enabled=(
            bool(repository.get("hasIssuesEnabled"))
            if isinstance(repository.get("hasIssuesEnabled"), bool) else None
        ),
        pulls=pulls,
        open_pulls_total=open_pulls_total,
    )


async def fetch_open_issues_for_remote(
    remote_url: str | None, **kwargs: Any
) -> IssuesResult:
    """:func:`fetch_open_issues` straight from ``project.json:git.remote_url``."""
    ref = parse_remote_url(remote_url)
    if ref is None or not ref.is_github_com:
        return _failure(
            None, "bad_remote",
            f"Not a github.com remote: {remote_url!r}."
            if remote_url else "This project has no git remote.",
        )
    return await fetch_open_issues(ref, **kwargs)
