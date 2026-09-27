"""
GitHub connector — XO GitHub App acquisition.

The third way to establish the GitHub connection, next to a pasted PAT
(``pat.py``) and the ``gh auth login`` device flow (``cli_auth.py``), which it
leaves untouched. The user installs the XO GitHub App; xo-swarm-api verifies
the install and hands back a signed *grant*, and the swarm turns that grant
into short-lived (1 h) installation tokens.

The installation token is stored in the same ``"github"`` entry the other two
methods use, so every consumer that reads ``get_github_token()`` works with
it unchanged — the issue poller (``gh`` via ``GH_TOKEN``) and the relay
poller (git via ``x-access-token``) included. What is different:

  - the grant is kept alongside the token, and ``refresh_if_needed`` swaps in
    a new token before the old one expires (run by
    :func:`start_app_token_refresher`);
  - an installation token is not a user, so ``GET /user`` answers 403.
    Status is checked against ``GET /installation/repositories`` instead.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx

from services.periodic import run_forever
from services.swarm_api import github_app as swarm_github_app

from ..token_store import get_entry, set_entry
from .common import GITHUB_API, _notify_issue_poller

log = logging.getLogger(__name__)

AUTH_METHOD = "app"

#: Renew when the token has less than this left. GitHub issues 60-minute
#: tokens, and the refresher ticks every ``REFRESH_INTERVAL_S``, so a token is
#: always replaced well before a poll could see it expire.
REFRESH_MARGIN_S = 15 * 60
REFRESH_INTERVAL_S = 5 * 60


def _entry() -> dict[str, Any] | None:
    entry = get_entry("github")
    if isinstance(entry, dict) and entry.get("auth_method") == AUTH_METHOD:
        return entry
    return None


def _expires_at(value: Any) -> float:
    """GitHub's ISO ``expires_at`` → epoch seconds (0 when unreadable)."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def start(return_to: str | None) -> dict[str, Any]:
    """Ask the swarm for the install URL. ``return_to`` is where the swarm's
    callback sends the browser (with the grant) once the app is installed."""
    res = await swarm_github_app.install_url(return_to)
    if res.unauthenticated:
        return {"ok": False, "status": 401, "error": "Sign in to XO first."}
    if not res.ok or not isinstance(res.data, dict) or not res.data.get("url"):
        return {"ok": False, "status": 502, "error": res.detail or "Could not get the GitHub App install URL."}
    return {"ok": True, "url": res.data["url"]}


async def _mint(grant: str) -> dict[str, Any]:
    """Exchange the grant for an installation token via the swarm."""
    res = await swarm_github_app.installation_token(grant)
    if res.ok and isinstance(res.data, dict) and res.data.get("token"):
        return {"ok": True, **res.data}
    if res.status in (403, 409):
        # Grant expired/foreign, or the app was uninstalled: reconnecting is the fix.
        return {"ok": False, "status": "needs_auth", "error": res.detail or "GitHub App access was revoked."}
    return {"ok": False, "status": "failed", "error": res.detail or "Could not reach XO to refresh GitHub access."}


def _store(grant: str, minted: dict[str, Any], *, account_login: str = "", installation_id: Any = None) -> None:
    previous = _entry() or {}
    set_entry("github", {
        "access_token": minted["token"],
        "refresh_token": None,
        "expires_at": _expires_at(minted.get("expires_at")),
        "token_type": "Bearer",
        "scope": "",
        "auth_method": AUTH_METHOD,
        "app_grant": grant,
        "account_login": account_login or previous.get("account_login", ""),
        "installation_id": installation_id if installation_id is not None else previous.get("installation_id"),
    })


async def connect(grant: str, *, account_login: str = "", installation_id: Any = None) -> dict[str, Any]:
    """Store the grant and a first installation token.

    Returns ``{"ok": True, "payload": ...}`` or ``{"ok": False, "status", "error"}``,
    the same shape ``pat.connect`` / ``cli_auth.connect`` use.
    """
    minted = await _mint(grant)
    if not minted["ok"]:
        return minted
    _store(grant, minted, account_login=account_login, installation_id=installation_id)
    # A new credential: let the issue poller drop a stale not_authenticated backoff now.
    _notify_issue_poller()
    log.info("GitHub connected via the XO GitHub App (account=%s)", account_login or "?")
    return {"ok": True, "payload": await status()}


async def refresh_if_needed(*, force: bool = False) -> bool:
    """Replace the stored installation token if it is close to expiry.

    A no-op unless the connection was made through the app, so a PAT or a
    ``gh`` session is never touched. Returns ``True`` iff a new token was stored.
    Never raises.
    """
    entry = _entry()
    if not entry or not entry.get("app_grant"):
        return False
    remaining = float(entry.get("expires_at") or 0) - time.time()
    if not force and remaining > REFRESH_MARGIN_S:
        return False
    minted = await _mint(entry["app_grant"])
    if not minted["ok"]:
        # Keep the old token: it may still have minutes left, and a poll that
        # outlives it reports not_authenticated on its own.
        log.warning("GitHub App token refresh failed (%s): %s", minted["status"], minted["error"])
        return False
    _store(entry["app_grant"], minted)
    log.info("GitHub App installation token refreshed")
    if remaining <= 0:
        # The old token had already lapsed, so the issue poller may be resting
        # on not_authenticated; the grant (its fingerprint) did not change, so
        # it would not notice on its own. Routine renewal skips this.
        _notify_issue_poller()
    return True


async def status() -> dict[str, Any]:
    """Connection status for an app-backed token, in ``get_status``'s shape."""
    entry = _entry()
    if not entry or not entry.get("access_token"):
        return {"status": "needs_auth", "auth_method": AUTH_METHOD}
    if float(entry.get("expires_at") or 0) <= time.time():
        await refresh_if_needed(force=True)
        entry = _entry() or entry
    base = {"auth_method": AUTH_METHOD, "username": entry.get("account_login", ""),
            "name": "", "avatar_url": "", "scopes": ""}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/installation/repositories",
                params={"per_page": 1},
                headers={
                    "Authorization": f"Bearer {entry['access_token']}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
    except httpx.TimeoutException:
        return {**base, "status": "failed", "error": "Timed out connecting to GitHub."}
    except Exception as exc:  # noqa: BLE001 — a status check reports, never raises
        return {**base, "status": "failed", "error": f"Could not connect to GitHub: {exc}"}
    if resp.status_code == 200:
        return {**base, "status": "connected", "valid": True,
                "repository_count": resp.json().get("total_count", 0)}
    if resp.status_code in (401, 403):
        return {**base, "status": "needs_auth", "error": "GitHub App access was revoked or expired."}
    return {**base, "status": "failed", "error": f"GitHub returned HTTP {resp.status_code}."}


async def start_app_token_refresher() -> None:
    """Background task: keep an app-backed token fresh. Idle for PAT / ``gh``."""
    await run_forever(
        "github app token refresher",
        refresh_if_needed,
        interval_s=lambda: float(REFRESH_INTERVAL_S),
        logger=log,
    )
