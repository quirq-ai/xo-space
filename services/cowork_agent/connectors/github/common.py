"""
GitHub connector — shared core, common to every auth method.

Both acquisition methods (a pasted PAT via ``github_pat.py``, and the
``gh auth login`` device flow via ``cli_auth.py``) end up with a bearer
token for github.com. Everything *after* that point is identical, and lives
here:

  - persistence  — provider key "github" in token.json (owned by token_store)
  - validation   — GET /user
  - status       — what the UI shows for the current connection
  - git identity — seed the workspace's global user.name / user.email

Nothing in this module knows how the token was obtained; the only trace of
that is the ``auth_method`` field carried alongside it for display purposes.

Token file: ~/.config/token.json  (see connectors/token_store.py)
"""

import asyncio
import logging
import shutil
from typing import Any, Literal

import httpx

from ..token_store import TOKEN_FILE, delete_entry, get_entry, set_entry

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

GitHubStatus = Literal["connected", "needs_auth", "failed"]
AuthMethod = Literal["pat", "cli"]


# ---------------------------------------------------------------------------
# Token storage (provider key "github" in token.json)
# ---------------------------------------------------------------------------

def get_github_token() -> str | None:
    """Return the stored GitHub access token, or None."""
    entry = get_entry("github")
    if not entry:
        return None
    return entry.get("access_token") or None


def get_github_auth_method() -> str | None:
    """Return the auth method used for the stored token: "pat", "cli", or None."""
    entry = get_entry("github")
    if not entry:
        return None
    # Pre-existing tokens (no auth_method field) are PATs.
    return entry.get("auth_method") or "pat"


def save_github_token(token: str, *, auth_method: str = "pat") -> None:
    """Save a GitHub access token to token.json.

    auth_method is "pat" (user-pasted PAT) or "cli" (from `gh auth login`).
    """
    set_entry("github", {
        "access_token": token,
        "refresh_token": None,
        "expires_at": 0,
        "token_type": "Bearer",
        "scope": "",
        "auth_method": auth_method,
    })
    log.info("GitHub token saved to %s (method=%s)", TOKEN_FILE, auth_method)


def delete_github_token() -> None:
    """Remove the GitHub entry from token.json."""
    delete_entry("github")
    log.info("GitHub token removed from %s", TOKEN_FILE)


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
    method = get_github_auth_method()
    if method:
        result["auth_method"] = method
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
GH_BIN = "gh"
_SUBPROCESS_TIMEOUT_SECONDS = 10


async def _run(*args: str) -> tuple[int, str]:
    """Run a command; return (returncode, merged output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_SUBPROCESS_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        log.warning("Command %s failed: %s", args[0], exc)
        return 1, ""
    return proc.returncode or 0, stdout.decode("utf-8", "replace").strip()


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

    When ``setup_credential_helper`` is set (the `gh auth login` flow, which
    leaves a real `gh` session behind), also runs `gh auth setup-git` so HTTPS
    pushes authenticate through that session instead of prompting.
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

    if setup_credential_helper and shutil.which(GH_BIN) is not None:
        rc, out = await _run(GH_BIN, "auth", "setup-git", "--hostname", "github.com")
        if rc != 0:
            log.warning("`gh auth setup-git` failed: %s", out)


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
