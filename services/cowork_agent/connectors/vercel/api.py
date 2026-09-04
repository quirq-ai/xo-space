"""Vercel REST API client."""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .oauth import Identity

log = logging.getLogger(__name__)

API_BASE = "https://api.vercel.com"
USER_URL = f"{API_BASE}/v2/user"
TOKENS_PAGE = "https://vercel.com/account/settings/tokens"

HTTP_TIMEOUT = 10.0


@dataclass(frozen=True)
class TokenCheck:
    """Outcome of a token check. `rejected` means Vercel refused it (401/403),
    as opposed to the call not completing."""

    ok: bool
    identity: Identity | None = None
    rejected: bool = False
    error: str = ""


async def whoami(token: str) -> TokenCheck:
    """Identify the owner of a token via GET /v2/user."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            resp = await http.get(USER_URL, headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException:
        return TokenCheck(ok=False, error="Timed out connecting to Vercel.")
    except httpx.HTTPError as exc:
        return TokenCheck(ok=False, error=f"Could not reach Vercel: {exc}")

    if resp.status_code in (401, 403):
        return TokenCheck(
            ok=False,
            rejected=True,
            error="Vercel rejected the token — it is invalid, expired, or revoked.",
        )
    if resp.status_code != 200:
        return TokenCheck(ok=False, error=f"Vercel returned HTTP {resp.status_code}.")

    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        return TokenCheck(ok=False, error="Vercel returned a malformed user response.")

    user = body.get("user") or body
    return TokenCheck(
        ok=True,
        identity=Identity(
            sub=user.get("id", "") or user.get("uid", ""),
            username=user.get("username", ""),
            name=user.get("name", ""),
            email=user.get("email", ""),
            avatar_url=user.get("avatar", "") or "",
        ),
    )
