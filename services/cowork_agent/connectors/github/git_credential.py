"""
GitHub connector — make plain `git` honour the repository selection.

The allowlist in ``repo_access`` only binds code that asks it. `git clone` in a
terminal never asks: after `gh auth login`, git's credential helper is
`gh auth git-credential`, which hands the all-repository token to any
github.com URL. So while access is "selected" this module swaps that out:

  * git's github.com credential helper becomes ``scripts/github_git_credential.py``
    (-> :func:`main` here), with ``useHttpPath`` on so the helper is told
    *which* repository is being accessed. It returns the token for a selected
    repository and refuses (``quit=true``, no prompt) for any other.
  * gh's own stored session is logged out, since `gh repo clone` / `gh api`
    have no per-repository notion to hook. This workspace's own `gh` calls are
    unaffected: they inject the stored token through ``GH_TOKEN``.

Back on "all", the gh session and `gh auth setup-git` are restored (CLI method)
or our helper is simply removed (PAT method, which never had one).

Still not a sandbox: the token stays readable in token.json by anything running
as this user, and public repositories clone with no credential at all. It stops
git and gh from *using* the token outside the selection; it cannot stop a
process that goes and reads the token itself.
"""

import logging
import shlex
import shutil
import sys
from pathlib import Path

from utils.commands import run

from .common import GH_BIN, GIT_BIN, get_github_auth_method, get_github_token
from .repo_access import NOT_SELECTED_MESSAGE, get_repo_access, is_repo_allowed

log = logging.getLogger(__name__)

_HOSTS = ("github.com", "gist.github.com")
_REPO_ROOT = Path(__file__).resolve().parents[4]
HELPER_SCRIPT = _REPO_ROOT / "scripts" / "github_git_credential.py"
_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# The helper git runs: `<script> get|store|erase`, request on stdin
# ---------------------------------------------------------------------------

def answer(request: dict[str, str]) -> tuple[dict[str, str], str]:
    """Decide one credential request. Returns (reply fields, stderr message)."""
    if request.get("protocol") != "https" or request.get("host") not in _HOSTS:
        return {}, ""
    token = get_github_token()
    if not token:
        return {}, ""

    if get_repo_access()["mode"] == "selected":
        # Gists are not repositories, so a selection can never include one.
        slug = "/".join(request.get("path", "").split("/")[:2])
        if request["host"] != "github.com" or not is_repo_allowed(slug):
            # quit=true: stop here rather than fall through to another helper
            # or a username prompt.
            return {"quit": "true"}, f"xo-space: {NOT_SELECTED_MESSAGE}"

    return {"username": "x-access-token", "password": token}, ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["get"]:
        return 0  # nothing to store or erase: token.json is the store
    request = dict(
        line.split("=", 1) for line in sys.stdin.read().splitlines() if "=" in line
    )
    try:
        reply, message = answer(request)
    except Exception:
        reply, message = {"quit": "true"}, "xo-space: could not read GitHub repository access."
    if message:
        print(message, file=sys.stderr)
    for key, value in reply.items():
        print(f"{key}={value}")
    return 0


# ---------------------------------------------------------------------------
# Installing / removing the helper
# ---------------------------------------------------------------------------

def _helper_command() -> str:
    return f"!{shlex.quote(sys.executable)} {shlex.quote(str(HELPER_SCRIPT))}"


async def _git(*args: str) -> tuple[int, str]:
    res = await run([GIT_BIN, "config", "--global", *args], timeout=_TIMEOUT_SECONDS)
    if res.timed_out or res.binary_missing or res.exception is not None:
        return 1, ""
    return res.returncode or 0, res.output.strip()


async def _install_helper() -> None:
    for host in _HOSTS:
        key = f"credential.https://{host}"
        # The empty first value resets the helper list, so no system- or
        # user-level helper (gh's included) is consulted after ours.
        await _git("--replace-all", f"{key}.helper", "")
        await _git("--add", f"{key}.helper", _helper_command())
        await _git(f"{key}.useHttpPath", "true")


async def _remove_helper() -> None:
    for host in _HOSTS:
        key = f"credential.https://{host}"
        rc, current = await _git("--get-all", f"{key}.helper")
        if rc == 0 and HELPER_SCRIPT.name in current:
            await _git("--unset-all", f"{key}.helper")
            await _git("--unset-all", f"{key}.useHttpPath")


async def apply_policy() -> None:
    """Bring git and gh in line with the stored selection. Never raises.

    Call after anything that changes the token or the selection.
    """
    try:
        if shutil.which(GIT_BIN) is None:
            return
        token = get_github_token()
        have_gh = shutil.which(GH_BIN) is not None

        if token and get_repo_access()["mode"] == "selected":
            await _install_helper()
            if have_gh:
                await run([GH_BIN, "auth", "logout", "--hostname", "github.com"],
                          timeout=_TIMEOUT_SECONDS)
            log.info("git now authenticates to GitHub only for selected repositories")
            return

        await _remove_helper()
        if token and have_gh and get_github_auth_method() == "cli":
            # A previous "selected" spell logged gh out; put its session back
            # so git and gh behave exactly as they did after `gh auth login`.
            status = await run([GH_BIN, "auth", "status", "--hostname", "github.com"],
                               timeout=_TIMEOUT_SECONDS)
            if status.returncode != 0:
                await run(
                    [GH_BIN, "auth", "login", "--hostname", "github.com",
                     "--git-protocol", "https", "--insecure-storage", "--with-token"],
                    input=token.encode(), timeout=_TIMEOUT_SECONDS,
                )
            await run([GH_BIN, "auth", "setup-git", "--hostname", "github.com"],
                      timeout=_TIMEOUT_SECONDS)
    except Exception:
        log.warning("Could not apply the GitHub git credential policy", exc_info=True)


async def enforce_selection() -> None:
    """Startup hook: re-apply the policy, but only when a selection is in force.

    With access on "all" there is nothing to enforce, and leaving git and gh
    exactly as the user has them is the whole point of that mode.
    """
    try:
        selected = bool(get_github_token()) and get_repo_access()["mode"] == "selected"
    except Exception:
        log.warning("Could not read GitHub repository access", exc_info=True)
        return
    if selected:
        await apply_policy()
