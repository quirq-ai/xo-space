"""One repo's open GitHub issues, read through ``gh api graphql``."""

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
MAX_QUERY_COST = 1

#: The pinned query.
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

#: The steady-state twin of :data:`ISSUES_QUERY`, identical in every respect
#: but one: ``states: [OPEN, CLOSED]``.
#: ==================================  =========  ======================
#: poll ``since`` query
#: ==================================  =========  ======================
#: first, or after a mirror reset absent :data:`ISSUES_QUERY` steady state set
#: this one
#: ==================================  =========  ======================
#: Seeding on ``[OPEN, CLOSED]`` would drag the repository's entire closed
#: history through the page budget; ``since`` is what bounds the volume, and it
#: does not exist on the first poll.
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


def _kill_tree(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the timed-out process **and its children**."""
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
    """One ``gh`` invocation. Returns ``(returncode, stdout, stderr)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=_subprocess_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own process group, so a timeout can kill the whole tree.
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
            # closes the three pipes.
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
    """One repo's issues, newest-updated first. **Never raises.**"""
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
        # Absent (an older cached response, or a schema that stopped offering
        # it) reads as ``None`` — "unknown" — never as ``False``, which would
        # claim the tracker is off.
        issues_enabled=(
            bool(repository.get("hasIssuesEnabled"))
            if isinstance(repository.get("hasIssuesEnabled"), bool) else None
        ),
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
