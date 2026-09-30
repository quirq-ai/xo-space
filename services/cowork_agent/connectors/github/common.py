"""
GitHub connector — shared core, common to every auth method.

Both acquisition methods (a pasted PAT via ``github_pat.py``, and the
``gh auth login`` device flow via ``cli_auth.py``) end with the GitHub CLI
signed in to github.com. Everything *after* that point is identical, and lives
here:

  - persistence  — gh's own credential store; the connector keeps no copy
  - validation   — GET /user
  - status       — what the UI shows for the current connection
  - git identity — seed the workspace's global user.name / user.email, and
                   `gh auth setup-git` for HTTPS credentials; disconnecting
                   removes both, so the next account starts clean

Nothing in this module knows how the token was obtained; the only trace of
that is ``auth_method``, read off the token's prefix for display purposes.
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Literal

import httpx

from utils.commands import run, run_sync

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_HOSTNAME = "github.com"

GitHubStatus = Literal["connected", "needs_auth", "failed"]
AuthMethod = Literal["pat", "cli"]


# ---------------------------------------------------------------------------
# Token storage: gh's credential store
# ---------------------------------------------------------------------------
#
# The token lives where `gh auth login` puts it: the system credential store,
# or ~/.config/gh/hosts.yml on a host without one. GitHub is connected exactly
# when gh holds a token for github.com, whether the connector or a terminal
# signed it in.

GH_BIN = "gh"
# Covers gh's own round-trip to GitHub to check the token and its scopes.
_GH_LOGIN_TIMEOUT_SECONDS = 30
_GH_TOKEN_TIMEOUT_SECONDS = 5
_GH_STATUS_TIMEOUT_SECONDS = 10

# `gh auth token` is a process spawn, and the issue poller asks for the token on
# every `gh` call it makes. gh rewrites hosts.yml on every login, logout and
# account switch — even when the secret itself sits in the system keyring — so
# its mtime says when the answer may have changed. (mtime_ns, token or None)
_gh_token_cache: tuple[int, str | None] | None = None


def gh_available() -> bool:
    return shutil.which(GH_BIN) is not None


def gh_hosts_file() -> Path:
    """Where ``gh`` keeps its own session, honouring its config-dir overrides."""
    override = (os.getenv("GH_CONFIG_DIR", "") or "").strip()
    if override:
        return Path(override) / "hosts.yml"
    xdg = (os.getenv("XDG_CONFIG_HOME", "") or "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "gh" / "hosts.yml"


def _gh_env() -> dict[str, str]:
    """The environment for `gh auth`, minus the variables that bypass its store.

    With GH_TOKEN or GITHUB_TOKEN set, `gh auth login` refuses to run and
    `gh auth token` echoes the variable instead of the stored token.
    """
    env = os.environ.copy()
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return env


def _forget_gh_token() -> None:
    global _gh_token_cache
    _gh_token_cache = None


def get_github_token(*, read_only: bool = False) -> str | None:
    """Return the token gh holds for github.com, or None when it is signed out.

    Synchronous, so the first read after gh's state changes blocks on one
    `gh auth token`; the cache answers the rest. When gh cannot be asked (it
    hangs, or its config is unreadable) ``read_only`` callers — status checks —
    get an exception, so a failed read cannot pass for "not connected"; the
    rest get None.
    """
    global _gh_token_cache
    try:
        stamp = gh_hosts_file().stat().st_mtime_ns
    except FileNotFoundError:
        return None  # gh has never been signed in here
    except OSError as exc:
        return _gh_unreadable(str(exc), read_only=read_only)
    if _gh_token_cache is not None and _gh_token_cache[0] == stamp:
        return _gh_token_cache[1]

    res = run_sync(
        [GH_BIN, "auth", "token", "--hostname", GITHUB_HOSTNAME],
        env=_gh_env(), timeout=_GH_TOKEN_TIMEOUT_SECONDS, separate_stderr=True, log_output=False,
    )
    if res.binary_missing:
        return None
    if res.timed_out or res.exception is not None:
        # Never res.output: on a timeout it keeps what gh printed before the
        # kill, which can be the token itself.
        detail = (f"`gh auth token` timed out after {_GH_TOKEN_TIMEOUT_SECONDS}s" if res.timed_out
                  else f"`gh auth token` could not run: {res.exception}")
        return _gh_unreadable(detail, read_only=read_only)
    token = (res.output.strip() if res.returncode == 0 else "") or None
    _gh_token_cache = (stamp, token)
    return token


def _gh_unreadable(detail: str, *, read_only: bool) -> None:
    """``detail`` ends up in logs, tracebacks and error responses: it must
    never carry anything gh printed."""
    if read_only:
        raise RuntimeError(f"Could not read the GitHub token from gh: {detail}")
    log.warning("Could not read the GitHub token from gh: %s", detail)
    return None


def auth_method_for(token: str) -> str:
    """"cli" for the OAuth token gh's device flow mints (gho_), else "pat"."""
    return "cli" if token.startswith("gho_") else "pat"


def get_github_auth_method() -> str | None:
    """Return the auth method behind the connected token: "pat", "cli", or None."""
    token = get_github_token()
    return auth_method_for(token) if token else None


def note_github_connected() -> None:
    """Both acquisition flows end here, once gh holds the new token.

    This is where the issue poller learns its backoff is stale: a poller
    resting ten minutes on ``not_authenticated`` must not outlive the sign-in
    that fixed it.
    """
    _forget_gh_token()
    _notify_issue_poller()


def _notify_issue_poller() -> None:
    """Tell the GitHub issue poller a new credential is in play. Never raises."""
    try:
        # Imported here rather than at module scope: the poller imports this
        # package, so a top-level import would close the cycle.
        from services.cowork_agent import github_poller

        github_poller.note_auth_change()
    except Exception:
        # Storing the token is the caller's actual business; a poller that
        # misses the hint still recovers on its own next tick.
        log.debug("could not notify the GitHub issue poller", exc_info=True)


async def login_gh_with_token(token: str) -> dict[str, str] | None:
    """`gh auth login --with-token`: put the token in gh's credential store.

    The token goes in on stdin, never argv or a file on disk. Returns None on
    success, else ``{"status", "error"}`` carrying gh's reason, typically a
    classic PAT without the ``repo`` and ``read:org`` scopes gh insists on.
    """
    res = await run(
        [GH_BIN, "auth", "login", "--hostname", GITHUB_HOSTNAME,
         "--git-protocol", "https", "--with-token"],
        input=token.encode(), env=_gh_env(), timeout=_GH_LOGIN_TIMEOUT_SECONDS,
    )
    _forget_gh_token()
    if res.ok:
        return None
    if res.timed_out or res.binary_missing or res.exception is not None:
        return {"status": "failed", "error": f"GitHub CLI could not sign in: {res.output.strip()}"}
    reason = res.output.strip() or f"`gh auth login` exited with status {res.returncode}."
    return {"status": "needs_auth", "error": f"GitHub CLI rejected this token: {reason}"}


async def disconnect_github_account() -> None:
    """Sign gh out of its active github.com account and remove what connecting
    wrote to the global gitconfig. Never raises."""
    await _logout_gh(await _gh_active_login())
    await _clear_git_config()


async def _gh_active_login() -> str | None:
    """The account gh uses for github.com. `gh auth logout` needs it named
    once gh holds several; None when it cannot tell."""
    res = await run(
        [GH_BIN, "auth", "status", "--hostname", GITHUB_HOSTNAME, "--active", "--json", "hosts"],
        env=_gh_env(), timeout=_GH_STATUS_TIMEOUT_SECONDS, separate_stderr=True,
    )
    if not res.ok:
        return None
    try:
        return json.loads(res.output)["hosts"][GITHUB_HOSTNAME][0]["login"] or None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def _logout_gh(username: str | None) -> None:
    """Sign gh out of github.com (one account of it, when named). Never raises."""
    argv = [GH_BIN, "auth", "logout", "--hostname", GITHUB_HOSTNAME]
    if username:
        argv += ["--user", username]
    res = await run(argv, env=_gh_env(), timeout=_SUBPROCESS_TIMEOUT_SECONDS)
    _forget_gh_token()
    if not res.ok:
        log.warning("`gh auth logout` failed: %s", res.output.strip())


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

async def validate_token(token: str) -> dict[str, Any]:
    """
    Validate a GitHub token by calling /user.

    Returns:
        {
            "valid": True/False,
            "status": "connected" | "needs_auth" | "failed",
            "username": "...",       # if valid
            "avatar_url": "...",     # if valid
            "scopes": "...",         # X-OAuth-Scopes header
            "error": "...",          # if not valid
        }
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )

        if resp.status_code == 200:
            user = resp.json()
            scopes = resp.headers.get("x-oauth-scopes", "")
            return {
                "valid": True,
                "status": "connected",
                "username": user.get("login", ""),
                "name": user.get("name", ""),
                "avatar_url": user.get("avatar_url", ""),
                "scopes": scopes,
                # Used only to seed the local git identity; not part of the
                # connection payload the UI receives.
                "user_id": user.get("id"),
                "email": user.get("email") or "",
            }
        elif resp.status_code in (401, 403):
            return {
                "valid": False,
                "status": "needs_auth",
                "error": "Token is invalid or revoked.",
            }
        else:
            return {
                "valid": False,
                "status": "failed",
                "error": f"GitHub returned HTTP {resp.status_code}.",
            }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "status": "failed",
            "error": "Timed out connecting to GitHub. Check your internet connection.",
        }
    except Exception as exc:
        return {
            "valid": False,
            "status": "failed",
            "error": f"Could not connect to GitHub: {exc}",
        }


async def get_status() -> dict[str, Any]:
    """
    Compute the current GitHub connector status.

    Returns a dict with `status`, and optionally `username`, `avatar_url`,
    `scopes`, and `auth_method` ("pat" | "cli") so the UI can show how the
    user is connected.
    """
    token = get_github_token()
    if not token:
        return {"status": "needs_auth"}

    result = await validate_token(token)
    result["auth_method"] = auth_method_for(token)
    return result


# ---------------------------------------------------------------------------
# Local git identity
# ---------------------------------------------------------------------------
#
# Connecting GitHub gives the *API* a token, but git itself still has no idea
# who the user is. In a fresh workspace `~/.gitconfig` does not exist, so the
# first `git commit` dies with:
#
#     Author identity unknown
#     *** Please tell me who you are.
#     fatal: unable to auto-detect email address (got 'coder@<pod-hostname>.(none)')
#
# The pod hostname has no domain, so git's auto-detected address is invalid.
# Since we have just authenticated the user, we know their name and email —
# seed the global config here so every repo in the workspace can commit.

GIT_BIN = "git"
_SUBPROCESS_TIMEOUT_SECONDS = 10


async def _run(*args: str) -> tuple[int, str]:
    """Run a command; return (returncode, merged output). Never raises."""
    res = await run(list(args), timeout=_SUBPROCESS_TIMEOUT_SECONDS)
    if res.timed_out or res.binary_missing or res.exception is not None:
        log.warning("Command %s failed: %s", args[0], res.output.strip())
        return 1, ""
    return res.returncode or 0, res.output.strip()


def commit_email(validation: dict[str, Any]) -> str:
    """Best commit email for the authenticated user.

    Prefers the public profile email. When the user keeps it private, GitHub
    returns null and we fall back to the noreply form, which GitHub still
    attributes to the account — and which a push never rejects, unlike a real
    address on an account with "block command line pushes" enabled.
    """
    email = (validation.get("email") or "").strip()
    if email:
        return email

    login = (validation.get("username") or "").strip()
    if not login:
        return ""

    user_id = validation.get("user_id")
    if user_id:
        return f"{user_id}+{login}@users.noreply.github.com"
    return f"{login}@users.noreply.github.com"


async def configure_git_identity(
    validation: dict[str, Any],
    *,
    setup_credential_helper: bool = False,
) -> None:
    """Seed the global git identity from a freshly validated GitHub account.

    Best-effort and non-fatal: connecting GitHub must still succeed on a box
    without `git`, or with a read-only HOME. Never overwrites values the user
    has already set — an explicitly configured identity wins over ours.

    When ``setup_credential_helper`` is set (the token is in gh's credential
    store), also runs `gh auth setup-git` so HTTPS clones and pushes to
    github.com authenticate through `gh auth git-credential` instead of
    prompting (on Coder, an external-auth GIT_ASKPASS that never completes in
    a terminal).
    """
    if shutil.which(GIT_BIN) is None:
        log.warning("git is not installed; skipping git identity setup")
        return

    name = (validation.get("name") or validation.get("username") or "").strip()
    email = commit_email(validation)

    for key, value in (("user.name", name), ("user.email", email)):
        if not value:
            continue
        rc, existing = await _run(GIT_BIN, "config", "--global", "--get", key)
        if rc == 0 and existing:
            log.info("git %s already set to %r; leaving it alone", key, existing)
            continue
        rc, out = await _run(GIT_BIN, "config", "--global", key, value)
        if rc != 0:
            log.warning("Could not set git %s: %s", key, out)
        else:
            log.info("git %s set to %r", key, value)

    if setup_credential_helper and gh_available():
        rc, out = await _run(GH_BIN, "auth", "setup-git", "--hostname", GITHUB_HOSTNAME)
        if rc != 0:
            log.warning("`gh auth setup-git` failed: %s", out)


# What connecting writes to the global gitconfig: the identity above, and the
# credential helpers `gh auth setup-git` points at gh.
_GIT_KEYS_SET_ON_CONNECT = (
    "user.name",
    "user.email",
    f"credential.https://{GITHUB_HOSTNAME}.helper",
    "credential.https://gist.github.com.helper",
)


async def _clear_git_config() -> None:
    """Remove what connecting wrote to the global gitconfig. Never raises.

    The identity goes even when the user set it by hand: the next account to
    connect, the same or another, seeds its own. Other settings stay, and git
    drops a section once its last key is gone.
    """
    if shutil.which(GIT_BIN) is None:
        return
    for key in _GIT_KEYS_SET_ON_CONNECT:
        rc, out = await _run(GIT_BIN, "config", "--global", "--unset-all", key)
        if rc not in (0, 5):  # 5: the key was not set
            log.warning("Could not remove git %s: %s", key, out)


def connection_payload(validation: dict[str, Any], auth_method: str) -> dict[str, Any]:
    """Shape a successful validation into the response body both flows return."""
    return {
        "status": "connected",
        "auth_method": auth_method,
        "username": validation.get("username", ""),
        "name": validation.get("name", ""),
        "avatar_url": validation.get("avatar_url", ""),
        "scopes": validation.get("scopes", ""),
    }
