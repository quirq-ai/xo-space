"""Vercel authorization server client (Sign in with Vercel: OAuth 2.1 + OIDC)."""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

ISSUER = "https://vercel.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"

AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = "https://api.vercel.com/login/oauth/token"
REGISTER_URL = "https://api.vercel.com/login/oauth/register"
USERINFO_URL = "https://api.vercel.com/login/oauth/userinfo"
REVOKE_URL = "https://api.vercel.com/login/oauth/token/revoke"
INTROSPECT_URL = "https://api.vercel.com/login/oauth/token/introspect"

DEFAULT_SCOPES = ("openid", "email", "profile", "offline_access")

HTTP_TIMEOUT = 15.0
REFRESH_SKEW_SECONDS = 120


class VercelOAuthError(RuntimeError):
    """A protocol-level failure, carrying a message fit to show a user."""

    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class TokenSet:
    """What the token endpoint hands back."""

    access_token: str
    refresh_token: str | None
    expires_at: int
    scope: str
    token_type: str = "Bearer"

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at - REFRESH_SKEW_SECONDS


@dataclass(frozen=True)
class Identity:
    """Who a token belongs to."""

    sub: str = ""
    username: str = ""
    name: str = ""
    email: str = ""
    avatar_url: str = ""


@dataclass
class PendingAuth:
    """One in-flight authorization request, keyed by its state."""

    state: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    started_at: float = field(default_factory=time.time)

    def expired(self, ttl: float) -> bool:
        return time.time() - self.started_at > ttl


def new_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(32)


def _error_from(resp: httpx.Response, fallback: str) -> VercelOAuthError:
    code = ""
    detail = ""
    try:
        body = resp.json()
        code = str(body.get("error") or "")
        detail = str(body.get("error_description") or body.get("message") or "")
    except ValueError:
        detail = (resp.text or "").strip()[:300]
    return VercelOAuthError(detail or code or fallback, status=resp.status_code, code=code)


async def _post_form(url: str, data: dict[str, str], **kwargs: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            return await http.post(url, data=data, **kwargs)
    except httpx.TimeoutException as exc:
        raise VercelOAuthError(f"Timed out talking to Vercel ({url}).") from exc
    except httpx.HTTPError as exc:
        raise VercelOAuthError(f"Could not reach Vercel: {exc}") from exc


async def fetch_discovery() -> dict[str, Any]:
    """Fetch the OpenID configuration."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            resp = await http.get(DISCOVERY_URL)
    except httpx.HTTPError as exc:
        raise VercelOAuthError(f"Could not reach Vercel discovery: {exc}") from exc
    if resp.status_code != 200:
        raise _error_from(resp, "Vercel discovery request failed.")
    return resp.json()


async def register_client(redirect_uri: str, client_name: str = "xo-cowork") -> dict[str, Any]:
    """Register a public client for `redirect_uri`.

    Vercel approves loopback callbacks only, with an explicit port, and answers
    with its shared public client (auth method `none`, no secret).
    """
    payload = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": " ".join(DEFAULT_SCOPES),
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            resp = await http.post(REGISTER_URL, json=payload)
    except httpx.TimeoutException as exc:
        raise VercelOAuthError("Timed out registering with Vercel.") from exc
    except httpx.HTTPError as exc:
        raise VercelOAuthError(f"Could not reach Vercel to register: {exc}") from exc

    if resp.status_code not in (200, 201):
        err = _error_from(resp, "Vercel rejected the client registration.")
        if err.code == "invalid_redirect_uri":
            raise VercelOAuthError(
                f"Vercel will not accept {redirect_uri} as a callback URL. It "
                "approves loopback callbacks only, with an explicit port — "
                "http://127.0.0.1:<port>/callback or "
                "http://localhost:<port>/callback.",
                status=err.status,
                code=err.code,
            ) from err
        raise err

    client = resp.json()
    if not client.get("client_id"):
        raise VercelOAuthError("Vercel's registration response had no client_id.")
    return client


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
) -> str:
    """Build the URL to send the user's browser to."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _token_set(payload: dict[str, Any]) -> TokenSet:
    access_token = payload.get("access_token")
    if not access_token:
        raise VercelOAuthError("Vercel's token response contained no access_token.")
    expires_in = payload.get("expires_in")
    return TokenSet(
        access_token=access_token,
        refresh_token=payload.get("refresh_token"),
        expires_at=int(time.time()) + int(expires_in) if expires_in else 0,
        scope=payload.get("scope", ""),
        token_type=payload.get("token_type", "Bearer"),
    )


async def exchange_code(
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_secret: str | None = None,
) -> TokenSet:
    """Trade a single-use authorization code for tokens."""
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        form["client_secret"] = client_secret

    resp = await _post_form(TOKEN_URL, form)
    if resp.status_code != 200:
        raise _error_from(resp, "Vercel would not exchange the authorization code.")
    return _token_set(resp.json())


async def refresh_tokens(
    *,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
) -> TokenSet:
    """Exchange a refresh token for a new pair; Vercel rotates both."""
    form = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        form["client_secret"] = client_secret

    resp = await _post_form(TOKEN_URL, form)
    if resp.status_code != 200:
        raise _error_from(resp, "Vercel would not refresh the access token.")
    return _token_set(resp.json())


async def revoke_token(
    *, client_id: str, token: str, client_secret: str | None = None
) -> bool:
    """Revoke a token. Only possible with a client secret; True when confirmed."""
    if not client_secret:
        return False
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = await _post_form(
        REVOKE_URL, {"token": token}, headers={"Authorization": f"Basic {basic}"}
    )
    if resp.status_code >= 400:
        log.warning("Vercel token revocation failed: HTTP %s", resp.status_code)
        return False
    return True


async def introspect(token: str) -> dict[str, Any]:
    """Ask Vercel whether a token is active; {} when the call fails."""
    resp = await _post_form(INTROSPECT_URL, {"token": token})
    if resp.status_code != 200:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


async def fetch_userinfo(access_token: str) -> Identity:
    """Read OIDC claims for an access token."""
    resp = await _post_form(
        USERINFO_URL, {}, headers={"Authorization": f"Bearer {access_token}"}
    )
    if resp.status_code != 200:
        raise _error_from(resp, "Vercel would not return user info.")
    claims = resp.json()
    return Identity(
        sub=claims.get("sub", ""),
        username=claims.get("preferred_username", ""),
        name=claims.get("name", ""),
        email=claims.get("email", ""),
        avatar_url=claims.get("picture", ""),
    )
