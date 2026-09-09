"""One repo's open GitHub issues, read through ``gh api graphql``.

The read side of the workitems mirror (``docs/workitems-plan.md`` §6). The
poller loop (W5) and the mirror store (W6) are separate; this module is the
client both of them call, and it does exactly one thing per call: one
subprocess, one page, one point of GraphQL budget.

**It never raises and never logs.** ``visualizer/git_provenance.py`` is the
house reference for that shape and the reasoning carries over intact: every
caller here needs a well-formed answer whether ``gh`` is missing, the user
never connected GitHub, the repo was deleted, the network is down or the
budget is spent. Those are five different *states*, not five exceptions, and
:class:`IssuesResult` reports which one happened in ``error_kind`` so the UI
can offer the right remedy. Nothing is logged because the token lives in the
environment of the subprocess and gh's diagnostics are echoed into
``error`` — a log line is one more place for that text to land.

Four choices that are load-bearing, each measured on this machine against
``cjpais/Handy`` (87 open issues) rather than reasoned about:

* **``gh``, not raw REST via httpx** (D5). It brings the auth for free, and
  the part that turned out to matter: the poll lands on the **GraphQL**
  budget, 5,000 points/hr, which is *separate* from the core REST 5,000/hr.
  A 60-second poll therefore competes with nothing — not ``git``, not the
  sync module, not MCP GitHub tooling. Measured via ``x-ratelimit-used``: a
  87-issue fetch costs **0 core**.

* **``gh api graphql`` with a pinned query, not ``gh issue list --json``.**
  Cost is the binding constraint (§6.2) and a porcelain command's internal
  query shape is not a contract — it can change between ``gh`` releases and
  take the ceiling with it. :data:`ISSUES_QUERY` is that contract, written
  down here, and ``tests/test_github_issues.py`` fails if its shape drifts.

* **``labels`` is deliberately not fetched.** Measured with ``rateLimit
  { cost }`` inside the query: ``first:100`` with ``assignees(first:5)``
  costs **1 point** — the same as with no nested connection at all — and
  adding ``labels(first:10)`` costs **2**, which halves the ceiling from
  ~83 polled repos at 60 s to ~41. ``assignees`` is non-negotiable because
  assignment *is* a GitHub assignee (D1); labels are cosmetic and are
  fetched lazily on adoption instead.

* **The budget is read from inside the query.** ``rateLimit { limit cost
  remaining resetAt }`` is requested inline, so a poll learns its own cost
  and its reset time without a second call. This is not merely an
  optimisation: ``gh api rate_limit`` was measured reporting a *full*
  budget regardless of consumption on this token, so a monitor built on
  that endpoint would report health forever.

**There is no pull-request filter, and there must not be one.** In GraphQL,
``repository.issues`` and ``repository.pullRequests`` are separate
connections, so a PR cannot appear in the response — structural, not
filtered (§6.1). The REST endpoint ``/repos/{o}/{r}/issues`` *does* return
PRs carrying a ``pull_request`` key (verified: ``#2046`` on that repo, absent
from the ``gh`` listing at the same moment). Adding a defensive filter here
would tell the next reader that PRs can arrive, which is false.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .github_connector import get_github_token

GH_BIN = "gh"

#: A poll must not outlive its interval. 20 s against a 60 s poll leaves room
#: for the kill-and-reap path below to finish before the next tick.
GH_TIMEOUT_S = 20.0

#: GitHub's own maximum for a connection page. Larger is rejected by the API,
#: not silently clamped, so the clamp is here.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 100

#: The measured cost of one :data:`ISSUES_QUERY` poll, in GraphQL points.
#: At 1 point and a 60-second interval the hourly budget of 5,000 supports
#: ~83 continuously-polled repos (D6). This is a **cost contract**: anything
#: that pushes a poll above it halves the ceiling, so
#: ``tests/test_github_issues.py`` asserts both the query's shape and, when
#: ``gh`` is authenticated, the live ``rateLimit.cost``.
MAX_QUERY_COST = 1

#: The pinned query. Every field below is a scalar except the two
#: connections, and the two connections are the measured shape:
#: ``issues(first:100)`` + ``assignees(first:5)`` = 1 point. Adding a third
#: connection is the one edit that changes the cost, which is why the test
#: enumerates them rather than diffing the whole string — reformatting the
#: query is free, adding a connection is not.
#:
#: ``filterBy.since`` is the high-water mark of §6.3. It does **not** lower
#: the cost (measured: still 1 with and without) — it lowers the payload, so
#: a quiet repo settles into a small response rather than a cheap one.
#:
#: ``assignees(first: 5)`` is a literal rather than a constant, because a
#: pinned query that is assembled from parts is not pinned. Five is a
#: judgement: GitHub's own cap is ten, five covers every real assignment,
#: and the measurement was identical either way — the nested connection is
#: free at this size, and it is *labels* that crosses the threshold.
ISSUES_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String, $since: DateTime) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    hasIssuesEnabled
    issues(
      first: $first
      after: $after
      states: [OPEN]
      filterBy: { since: $since }
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        state
        stateReason
        url
        updatedAt
        assignees(first: 5) { nodes { login avatarUrl } }
      }
    }
  }
}
"""

#: The steady-state twin of :data:`ISSUES_QUERY`, identical in every
#: respect but one: ``states: [OPEN, CLOSED]``.
#:
#: **It exists because the high-water mark strands closed issues** (§6.3,
#: amendment 6 — a real design bug, not a detail). With ``since`` set and
#: ``states: [OPEN]``, an issue *closed* since the mark simply stops
#: matching, so an incremental merge leaves a stale ``open`` row in the
#: mirror forever and nothing ever corrects it — the exact "false statement
#: about who owes what" §5.3 forbids. Asking for ``CLOSED`` as well is the
#: only way a transition is ever observed.
#:
#: So the two are used in the two situations §6.3 tabulates, and nowhere
#: else:
#:
#: ==================================  =========  ======================
#: poll                                ``since``  query
#: ==================================  =========  ======================
#: first, or after a mirror reset      absent     :data:`ISSUES_QUERY`
#: steady state                        set        this one
#: ==================================  =========  ======================
#:
#: Seeding on ``[OPEN, CLOSED]`` would drag the repository's entire closed
#: history through the page budget; ``since`` is what bounds the volume,
#: and it does not exist on the first poll.
#:
#: **Measured on this machine, 2026-09-08**, against ``cjpais/Handy`` with
#: ``rateLimit { cost }`` inside the query: ``first:100`` +
#: ``assignees(first:5)`` + ``states:[OPEN, CLOSED]`` + ``filterBy.since``
#: costs **1 point** — the same as :data:`ISSUES_QUERY`, and the response
#: carried both open and closed rows. The ~83-repo ceiling is unchanged.
#:
#: **Written out in full rather than derived from the string above.** A
#: pinned query assembled from parts is not pinned, and a ``.replace()``
#: would degrade *silently* into the very bug this constant fixes the day
#: someone reformats the other query's ``states`` clause. The duplication
#: is held honest by ``tests/test_github_poller.py``, which asserts the two
#: differ in exactly the states clause and in nothing else.
ISSUES_QUERY_WITH_CLOSED = """
query($owner: String!, $name: String!, $first: Int!, $after: String, $since: DateTime) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    hasIssuesEnabled
    issues(
      first: $first
      after: $after
      states: [OPEN, CLOSED]
      filterBy: { since: $since }
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        state
        stateReason
        url
        updatedAt
        assignees(first: 5) { nodes { login avatarUrl } }
      }
    }
  }
}
"""

#: The closed vocabulary of failure states. Mirrored by the ``error.kind``
#: enum in ``visualizer/schema/github-issues.schema.json``; the test asserts
#: the two agree, so a new kind cannot reach the UI undeclared.
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

#: GraphQL enum → the lowercase vocabulary §5.4 pins to GitHub's own.
_STATES = {"OPEN": "open", "CLOSED": "closed"}
#: An unrecognised reason becomes ``None`` rather than being coerced into
#: the nearest neighbour. GitHub has added reasons since §5.4 was written
#: (``DUPLICATE``), and mapping one of those onto ``not_planned`` would put
#: a claim in the mirror that GitHub never made. Absent beats wrong — the
#: same rule §5.3 applies to a stale ``closed``.
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
#: ``ssh://git@host/owner/repo.git``, ``git://host/owner/repo.git``. The
#: userinfo group exists to be discarded — it is either an already-stripped
#: credential or the SSH username ``git``, and neither is part of the path.
_URL_RE = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://)?"
    r"(?:(?P<userinfo>[^/@]+)@)?"
    r"(?P<host>[^/:]+)(?::\d+)?"
    r"/(?P<path>.+)$"
)

#: ``git@host:owner/repo.git`` — the scp-short form, which has no ``//`` and
#: therefore no authority to parse. It is the default for a repo cloned over
#: SSH, and ``git_provenance.sanitize_remote_url`` returns it untouched, so
#: it reaches this module exactly as git stored it.
_SCP_RE = re.compile(r"^(?:(?P<userinfo>[^/@]+)@)?(?P<host>[^/:]+):(?P<path>.+)$")

#: What GitHub accepts in an owner or repository name. Strict not for
#: safety — owner and name travel as GraphQL *variables*, so there is no
#: string to inject into — but for budget: a name outside this set is not a
#: repo any poll could resolve, and rejecting it here costs nothing while
#: asking GitHub costs a point.
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
        """Whether this is github.com proper, as opposed to an Enterprise host.

        The distinction matters to the caller, not to this module: a GHE
        remote is polled through ``--hostname`` and works if ``gh`` holds a
        session for that host, while the poller may reasonably choose to
        skip non-github.com remotes rather than spend a point discovering it
        has no session there.
        """
        host = self.host.lower()
        return host == "github.com" or host.endswith(".github.com")

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.slug


def parse_remote_url(url: str | None) -> RepoRef | None:
    """``project.json:git.remote_url`` → :class:`RepoRef`, or ``None``.

    All three shapes that reach this system parse, because all three occur
    in the wild and ``git_provenance`` passes each through unchanged:

    ``https://github.com/owner/repo.git``
        The clone default over HTTPS. A ``user:token@`` userinfo has already
        been stripped upstream, but this function tolerates one anyway
        rather than depending on the order of two modules.

    ``ssh://git@github.com/owner/repo.git``
        The explicit SSH form. ``git@`` is the SSH *username*, which
        ``sanitize_remote_url`` deliberately preserves, so it is present
        here and must not be mistaken for a path segment.

    ``git@github.com:owner/repo.git``
        The scp-short form, and the one that has no ``//`` — a naive
        ``urlsplit`` reads the whole thing as a bare path and yields
        nothing. It is the default remote for an SSH clone, so getting it
        wrong would silently disable polling for a large share of projects.

    ``None`` for anything else: no remote, a local path, a URL with no
    ``owner/repo`` pair, or names outside GitHub's character set. The caller
    reports that as ``bad_remote`` and spends no budget on it.
    """
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    # A scheme decides which grammar applies. The scp-short form is tried
    # only when there is no ``://`` at all, so ``https://host/o/r`` can never
    # be read as a host named ``https``.
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
        # Exactly ``owner/repo`` and nothing else. Tolerating a longer path
        # would turn a URL this module does not understand into a
        # plausible-looking slug, and the caller would spend a point of the
        # global budget discovering it is not a repo. ``bad_remote`` costs
        # nothing.
        return None
    owner, name = parts
    if not _NAME_RE.match(owner) or not _NAME_RE.match(name):
        return None
    return RepoRef(host=host, owner=owner, name=name)


def _coerce_ref(value: str) -> RepoRef | None:
    """A :class:`RepoRef` from either a remote URL or a bare ``owner/name``.

    Callers hold both spellings — the poller has ``project.json``'s remote
    URL, while a route, a test or a fixture holds the slug it read back out
    of the mirror. The slug branch is tried first and only for a string with
    no scheme, no ``@`` and no ``:``, so ``git@host:o/r`` can never be
    mistaken for a two-segment slug.
    """
    text = (value or "").strip()
    if not text:
        return None
    if "://" not in text and "@" not in text and ":" not in text:
        parts = [p for p in text.strip("/").split("/") if p]
        if len(parts) == 2 and all(_NAME_RE.match(p) for p in parts):
            return RepoRef(host="github.com", owner=parts[0], name=parts[1])
    return parse_remote_url(text)


def parse_repo_slug(url: str | None) -> str | None:
    """``owner/repo`` for a remote URL, or ``None``. Convenience over
    :func:`parse_remote_url` for callers that only want the slug."""
    ref = parse_remote_url(url)
    return ref.slug if ref else None


# ---------------------------------------------------------------------------
# The cost contract, made checkable
# ---------------------------------------------------------------------------

#: A field selection carrying a pagination argument — i.e. a *connection*,
#: the only construct that adds to a query's point cost. ``$first`` in the
#: operation's variable list is excluded by the lookbehind: ``$first: Int!``
#: declares a variable, it does not open a connection.
_CONNECTION_RE = re.compile(
    r"\b(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(?P<args>[^()]*)\)", re.DOTALL
)
_PAGE_ARG_RE = re.compile(r"(?<![$\w])(?:first|last)\s*:\s*(?P<value>\$?[A-Za-z0-9_]+)")


def query_connections(query: str = ISSUES_QUERY) -> tuple[tuple[str, str], ...]:
    """Every paginated connection in ``query``, as ``(field, page size)``.

    Exists so the cost contract can be checked **without a network call**.
    GitHub's scoring is not a formula this module could reimplement — the
    measurements show ``assignees(first:5)`` adding nothing while
    ``labels(first:10)`` adds a point — so the test does not try to predict
    a cost. It pins the *shape* that was measured at 1 point, and any new
    connection changes this tuple and fails.

    Whitespace, field order and reformatting are all invisible here. That is
    the point: the query is allowed to be edited, it is not allowed to grow
    a connection unnoticed.
    """
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
    """The GraphQL budget as GitHub reported it *inside the response*.

    Every field is optional because a failed call still has to return
    something, and inventing a budget is how a poller talks itself past a
    limit it has actually hit.
    """

    limit: int | None = None
    cost: int | None = None
    remaining: int | None = None
    reset_at: str | None = None

    @property
    def known(self) -> bool:
        return self.remaining is not None and self.reset_at is not None

    def as_document(self) -> dict[str, Any] | None:
        """The ``rate`` value for the mirror, or ``None`` when unknown.

        ``None`` rather than a half-filled object, because the schema
        requires ``remaining`` and ``reset_at`` together: a budget is either
        observed or it is not, and "remaining: null" reads as a number to
        every consumer that does not check.
        """
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
    """One poll's outcome — never an exception, always one of these.

    ``ok`` is the only field a caller must branch on; ``error_kind`` says
    *which* failure it was, and is one of :data:`ERROR_KINDS`. ``rate`` is
    populated whenever GitHub answered at all, including on a NOT_FOUND,
    because a failed poll still spent a point and the global budget has to
    account for it.
    """

    ok: bool
    repo: str | None
    fetched_at: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    rate: RateLimit = field(default_factory=RateLimit)
    error_kind: str | None = None
    error: str | None = None
    has_next_page: bool = False
    end_cursor: str | None = None
    #: ``repository.hasIssuesEnabled`` — whether the issue tracker is turned
    #: on for this repository at all. ``None`` when the poll failed, because
    #: a failure establishes nothing about the repository's settings.
    #:
    #: It is on the query because a repository with issues disabled answers
    #: a *successful* poll with an empty issue set, byte-identical to a
    #: healthy repository that simply has no open issues (verified on
    #: ``dwivedi-ai/xo-cowork-api``). Without this the two are
    #: indistinguishable and the empty state has to guess. It is a scalar,
    #: not a connection, so it is free: measured on ``cjpais/Handy``,
    #: ``rateLimit.cost`` is 1 with and without it.
    issues_enabled: bool | None = None

    @property
    def high_water_mark(self) -> str | None:
        """The newest ``updated_at`` in this page, for the next ``since``.

        Computed from the rows rather than taken from the first one: the
        query is ``UPDATED_AT DESC`` so they coincide today, but a caller
        that reorders would otherwise silently rewind the mark.
        """
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
    """The environment for ``gh``, with the stored token injected if needed.

    ``gh`` authenticates from its own ``~/.config/gh/hosts.yml``, which the
    device-flow login in ``github_cli_auth`` populates. But a user who
    connected by pasting a PAT has a token in ``mcp-tokens.json`` and no gh
    session at all, and the poller should work for them too — so the stored
    token is exported as ``GH_TOKEN`` when the environment does not already
    carry one. An explicit ``GH_TOKEN``/``GITHUB_TOKEN`` in the process
    environment always wins; overriding an operator's choice would be the
    surprising direction.

    The token is placed in the child's environment and nowhere else: it is
    never logged, never interpolated into an argument (where it would show
    up in ``ps``), and never returned in an error message.
    """
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


def _kill_tree(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the timed-out process **and its children**.

    The group kill is the point. Killing only ``gh`` leaves any helper it
    spawned holding the write end of our stdout pipe, so the read never sees
    EOF and the cleanup below blocks for its full bound — once a minute,
    forever. Falls back to killing the process alone if the group is already
    gone, and swallows everything: this is the cleanup path of a function
    whose contract is that it does not raise.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


async def _run_gh(argv: list[str], timeout_s: float) -> tuple[int | None, str, str]:
    """One ``gh`` invocation. Returns ``(returncode, stdout, stderr)``.

    ``returncode`` is ``None`` for the two cases that have no exit status:
    the binary could not be executed, or it had to be killed on timeout.
    The caller tells them apart by the sentinel in stderr, which keeps this
    helper free of classification.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=_subprocess_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own process group, so a timeout can kill the whole tree.
            # ``gh`` may spawn helpers (a credential helper, a proxy dialer),
            # and SIGKILL to the parent alone leaves a child holding the read
            # end of our pipe — which a poller running once a minute turns
            # into an accumulating leak rather than a one-off.
            start_new_session=True,
        )
    except FileNotFoundError:
        return None, "", "__no_cli__"
    except Exception as exc:  # OSError: no fork, no exec, no permission
        return None, "", f"__spawn_failed__ {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        _kill_tree(proc)
        try:
            # ``communicate`` rather than ``wait``: it reaps the process AND
            # closes the three pipes. ``wait`` alone leaves the transport for
            # the garbage collector, which then tries to close it against an
            # event loop that may already be gone — a "Event loop is closed"
            # unraisable, which is exactly the noise this module promises not
            # to make. Bounded, because a kill that did not take must not
            # become a poller that never returns.
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            pass
        return None, "", "__timeout__"
    except Exception as exc:
        return None, "", f"__spawn_failed__ {exc}"

    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace") if stdout else "",
        stderr.decode("utf-8", errors="replace") if stderr else "",
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
    """The first GraphQL error, as ``(kind, message)``.

    GraphQL answers ``200 OK`` with an ``errors`` array, so a missing repo
    and a spent budget both arrive as a *successful* HTTP response with a
    partial body — which is also why ``data.rateLimit`` is still readable
    on a NOT_FOUND and the poll's point is still accounted for.
    """
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
    """A REST-shaped error body, which gh emits for transport-level failures.

    An auth failure never reaches GraphQL: ``gh`` prints
    ``{"message": "Bad credentials", "status": "401"}`` — the REST error
    envelope — on stdout and exits 1. Measured, not assumed.
    """
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
    # gh documents exit status 4 as "authentication required"; it is the
    # code for a machine with no gh session and no token in the environment.
    if returncode == 4 or any(m in lowered for m in _AUTH_MARKERS):
        return "not_authenticated", text or "GitHub CLI is not authenticated."
    if any(m in lowered for m in _RATE_MARKERS):
        return "rate_limited", text or "GitHub rate limit reached."
    if any(m in lowered for m in _NETWORK_MARKERS):
        return "network", text or "Could not reach GitHub."
    return "unknown", text or f"`gh api graphql` exited with status {returncode}."


def _issue_row(node: Any) -> dict[str, Any] | None:
    """One GraphQL issue node → one mirror row, or ``None`` if unusable.

    ``labels`` is absent by design: the poll does not fetch them (§6.2), and
    an empty array here would be the claim "this issue has no labels", which
    the poll never establishes. Absent beats wrong — the same rule §5.3
    applies to a stale ``closed``.
    """
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
    for person in nodes or []:
        if not isinstance(person, dict):
            continue
        login = person.get("login")
        if not isinstance(login, str) or not login:
            continue
        avatar = person.get("avatarUrl")
        assignees.append({
            "login": login,
            "avatar_url": avatar if isinstance(avatar, str) else None,
        })
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
    }


async def fetch_open_issues(
    repo: RepoRef | str,
    *,
    since: str | None = None,
    after: str | None = None,
    first: int = DEFAULT_PAGE_SIZE,
    include_closed: bool = False,
    timeout_s: float = GH_TIMEOUT_S,
    gh_bin: str = GH_BIN,
) -> IssuesResult:
    """One repo's issues, newest-updated first. **Never raises.**

    :param repo: a :class:`RepoRef`, an ``owner/name`` slug, or a remote URL.
    :param since: high-water mark — only issues updated at or after it
        (§6.3). Lowers the payload, not the cost.
    :param after: an ``end_cursor`` from a previous page. One call is one
        page and one point; pagination is the caller's decision because the
        budget is global (§6.3) and this module cannot see it.
    :param first: page size, clamped to 1..100.
    :param include_closed: which pinned query to send — ``False`` selects
        :data:`ISSUES_QUERY` (``states: [OPEN]``), ``True`` selects
        :data:`ISSUES_QUERY_WITH_CLOSED`. This is §6.3's parameterisation
        of ``states``, and it is a boolean rather than a list because
        there are exactly two legitimate settings and each has its own
        pinned, separately measured query. **Pass ``True`` whenever
        ``since`` is set**: an issue closed since the mark stops matching
        ``[OPEN]`` entirely, so an incremental merge would leave a stale
        ``open`` row in the mirror forever. Both cost 1 point (measured).
        The function keeps its name for its default behaviour.

    On success, ``issues`` holds mirror-shaped rows ready for §5.2 and
    ``has_next_page``/``end_cursor`` say whether the repo has more. On
    failure, ``issues`` is empty and ``error_kind`` is one of
    :data:`ERROR_KINDS` — the mirror keeps whatever it had, because a failed
    poll is not evidence that the issues went away.
    """
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

    returncode, stdout, stderr = await _run_gh(argv, timeout_s)

    if stderr == "__no_cli__":
        return _failure(slug, "no_cli", "GitHub CLI (`gh`) could not be executed.")
    if stderr == "__timeout__":
        return _failure(
            slug, "timeout",
            f"`gh api graphql` did not answer within {timeout_s:g}s.",
        )
    if stderr.startswith("__spawn_failed__"):
        return _failure(
            slug, "unknown",
            f"Could not run `gh`: {stderr[len('__spawn_failed__'):].strip()}",
        )

    payload: Any = None
    if stdout.strip():
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            payload = None

    rate = _parse_rate(payload)

    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            classified = _classify_graphql_errors(errors)
            if classified:
                return _failure(slug, classified[0], classified[1], rate)
        if "data" not in payload:
            classified = _classify_rest_error(payload)
            if classified:
                return _failure(slug, classified[0], classified[1], rate)

    if returncode != 0:
        kind, message = _classify_stderr(stderr, returncode)
        return _failure(slug, kind, message, rate)

    if not isinstance(payload, dict):
        return _failure(
            slug, "bad_response",
            "`gh api graphql` returned no JSON body.", rate,
        )

    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
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

    return IssuesResult(
        ok=True,
        repo=slug,
        fetched_at=_utc_now(),
        issues=rows,
        rate=rate,
        has_next_page=bool(info.get("hasNextPage")),
        end_cursor=cursor if isinstance(cursor, str) else None,
        # Absent (an older cached response, or a schema that stopped
        # offering it) reads as ``None`` — "unknown" — never as ``False``,
        # which would claim the tracker is off.
        issues_enabled=(
            bool(repository.get("hasIssuesEnabled"))
            if isinstance(repository.get("hasIssuesEnabled"), bool) else None
        ),
    )


async def fetch_open_issues_for_remote(
    remote_url: str | None, **kwargs: Any
) -> IssuesResult:
    """:func:`fetch_open_issues` straight from ``project.json:git.remote_url``.

    A remote that is not on github.com returns ``bad_remote`` **without
    spawning** ``gh``. That gate is here rather than left to the API to
    reject, because the poller's budget is global and a project whose origin
    is GitLab must not cost a point every minute to rediscover that it is
    not a GitHub repo. It is also not a hypothetical: pointing this query at
    ``gitlab.com`` returns ``DateTime isn't a defined input type`` — a
    different GraphQL schema entirely, which no amount of retrying fixes.

    A GitHub Enterprise host is reachable, but only deliberately: construct
    the :class:`RepoRef` and call :func:`fetch_open_issues`, which routes it
    with ``gh --hostname``. It is not done automatically because it needs a
    ``gh`` session on that host, and silently spending budget to find out
    there is none is the failure this gate exists to prevent.
    """
    ref = parse_remote_url(remote_url)
    if ref is None or not ref.is_github_com:
        return _failure(
            None, "bad_remote",
            f"Not a github.com remote: {remote_url!r}."
            if remote_url else "This project has no git remote.",
        )
    return await fetch_open_issues(ref, **kwargs)
