"""Vercel connection state, persistence, and flow orchestration.

Two ways in: a pasted API token (https://vercel.com/account/settings/tokens), or
Sign in with Vercel. Nothing has to be configured for either — the OAuth client
is registered with Vercel on first use, and its callback is
``http://127.0.0.1:<PORT>/callback`` because Vercel's dynamic registration
approves loopback callbacks only. When the browser cannot open that address the
flow is finished by passing the address-bar URL to ``complete_authorization``.

Persisted by ``token_store`` under the keys "vercel" and "vercel_client".
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from ..token_store import TOKEN_FILE, delete_entry, get_entry, set_entry
from . import api, oauth
from .oauth import Identity, PendingAuth, TokenSet, VercelOAuthError

log = logging.getLogger(__name__)

PROVIDER_KEY = "vercel"
CLIENT_KEY = "vercel_client"
PENDING_KEY = "vercel_pending"

PENDING_TTL_SECONDS = 30 * 60
MAX_PENDING = 32

AUTH_METHOD_TOKEN = "api_token"
AUTH_METHOD_OAUTH = "oauth"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def default_redirect_uri() -> str:
    """The callback URL used when the caller doesn't name one."""
    port = _env("PORT") or "5002"
    return f"http://127.0.0.1:{port}/callback"


def _stored_client() -> dict[str, Any]:
    return get_entry(CLIENT_KEY) or {}


def _client_secret_for(client_id: str) -> str | None:
    """The secret registered with `client_id`, if Vercel issued one."""
    stored = _stored_client()
    if stored.get("client_id") == client_id:
        return stored.get("client_secret")
    return None


@dataclass
class Connection:
    """The stored connection, in the shape the HTTP layer returns."""

    status: str
    auth_method: str = ""
    identity: Identity | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"status": self.status}
        if self.status == "connected":
            body["valid"] = True
        if self.auth_method:
            body["auth_method"] = self.auth_method
        if self.identity:
            body.update({
                "username": self.identity.username,
                "name": self.identity.name or self.identity.username,
                "email": self.identity.email,
                "avatar_url": self.identity.avatar_url,
            })
        if self.error:
            body["error"] = self.error
        return body


def needs_auth() -> Connection:
    return Connection(status="needs_auth")


def _identity_from_entry(entry: dict[str, Any]) -> Identity:
    return Identity(
        sub=entry.get("sub", ""),
        username=entry.get("username", ""),
        name=entry.get("name", ""),
        email=entry.get("email", ""),
        avatar_url=entry.get("avatar_url", ""),
    )


def _save(
    *,
    auth_method: str,
    access_token: str,
    identity: Identity,
    refresh_token: str | None = None,
    expires_at: int = 0,
    scope: str = "",
) -> None:
    set_entry(PROVIDER_KEY, {
        "auth_method": auth_method,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "scope": scope,
        "token_type": "Bearer",
        "sub": identity.sub,
        "username": identity.username,
        "name": identity.name,
        "email": identity.email,
        "avatar_url": identity.avatar_url,
        "connected_at": int(time.time()),
    })
    log.info("Vercel connected via %s; credentials in %s", auth_method, TOKEN_FILE)


async def connect_with_api_token(token: str) -> Connection:
    """Validate a pasted API token and store it."""
    token = token.strip()
    if not token:
        return Connection(status="failed", error="A token is required.")

    check = await api.whoami(token)
    if not check.ok:
        return Connection(
            status="needs_auth" if check.rejected else "failed",
            error=check.error,
        )

    identity = check.identity or Identity()
    _save(auth_method=AUTH_METHOD_TOKEN, access_token=token, identity=identity)
    return Connection(status="connected", auth_method=AUTH_METHOD_TOKEN, identity=identity)


def _remember_pending(auth: PendingAuth) -> None:
    """Record an in-flight authorization so any instance can finish it.

    Kept in the credential store rather than process memory: the callback is
    routinely served by a different process than the one that started the flow
    (a restart, a second instance, a forwarded port), and a code_verifier only
    that first process holds is a flow nobody can complete.
    """
    pending = {
        state: auth
        for state, auth in (get_entry(PENDING_KEY) or {}).items()
        if time.time() - float(auth.get("started_at") or 0) <= PENDING_TTL_SECONDS
    }
    while len(pending) >= MAX_PENDING:
        oldest = min(pending, key=lambda s: float(pending[s].get("started_at") or 0))
        pending.pop(oldest)

    pending[auth.state] = {
        "code_verifier": auth.code_verifier,
        "redirect_uri": auth.redirect_uri,
        "client_id": auth.client_id,
        "started_at": auth.started_at,
    }
    set_entry(PENDING_KEY, pending)


def _take_pending(state: str) -> PendingAuth | None:
    """Consume an in-flight authorization by state, so a code is used once."""
    pending = get_entry(PENDING_KEY) or {}
    record = pending.pop(state, None)
    if record is None:
        return None

    if pending:
        set_entry(PENDING_KEY, pending)
    else:
        delete_entry(PENDING_KEY)

    return PendingAuth(
        state=state,
        code_verifier=record.get("code_verifier", ""),
        redirect_uri=record.get("redirect_uri", ""),
        client_id=record.get("client_id", ""),
        started_at=float(record.get("started_at") or 0),
    )


async def _client_for(redirect_uri: str) -> tuple[str, str | None]:
    """Return (client_id, client_secret) able to use `redirect_uri`.

    A client only works with the redirect URIs it was registered against, so a
    stored one that doesn't cover this redirect is replaced.
    """
    stored = _stored_client()
    if stored.get("client_id") and redirect_uri in (stored.get("redirect_uris") or []):
        return stored["client_id"], stored.get("client_secret")

    client = await oauth.register_client(redirect_uri)
    set_entry(CLIENT_KEY, {
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret"),
        "redirect_uris": client.get("redirect_uris") or [redirect_uri],
        "registered_at": int(time.time()),
    })
    log.info("Registered Vercel OAuth client for %s", redirect_uri)
    return client["client_id"], client.get("client_secret")


@dataclass
class Authorization:
    """Everything the caller needs to drive one authorization attempt."""

    auth_url: str
    state: str
    redirect_uri: str
    reachable_callback: bool
    instructions: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "auth_url": self.auth_url,
            "state": self.state,
            "redirect_uri": self.redirect_uri,
            "reachable_callback": self.reachable_callback,
            "instructions": self.instructions,
            "exchange_url": "/api/connectors/vercel/oauth/exchange",
        }


_PASTE_INSTRUCTIONS = (
    "After approving, the browser will try to open {redirect} and may fail to "
    "load it — that is expected when this API runs on another machine. Copy the "
    "full URL from the address bar and send it to "
    "/api/connectors/vercel/oauth/exchange to finish connecting."
)


async def start_authorization(redirect_uri: str | None = None) -> Authorization:
    """Begin an authorization: mint PKCE + state and build the Vercel URL."""
    effective_redirect = (redirect_uri or "").strip() or default_redirect_uri()
    client_id, _secret = await _client_for(effective_redirect)

    verifier, challenge = oauth.new_pkce_pair()
    state = oauth.new_state()

    _remember_pending(PendingAuth(
        state=state,
        code_verifier=verifier,
        redirect_uri=effective_redirect,
        client_id=client_id,
    ))

    auth_url = oauth.build_authorize_url(
        client_id=client_id,
        redirect_uri=effective_redirect,
        state=state,
        code_challenge=challenge,
    )
    loopback = effective_redirect.startswith(("http://127.0.0.1", "http://localhost"))
    return Authorization(
        auth_url=auth_url,
        state=state,
        redirect_uri=effective_redirect,
        reachable_callback=not loopback,
        instructions=_PASTE_INSTRUCTIONS.format(redirect=effective_redirect) if loopback else "",
    )


async def complete_authorization(code: str, state: str) -> Connection:
    """Finish an authorization with the code and state Vercel handed back."""
    pending = _take_pending(state.strip())
    if pending is None:
        return Connection(
            status="failed",
            error=(
                "That authorization has expired or was already used. Start the "
                "connection again from this workspace, so the sign-in and the "
                "callback are handled by the same API."
            ),
        )
    if pending.expired(PENDING_TTL_SECONDS):
        return Connection(status="failed", error="That authorization expired. Start again.")

    try:
        tokens = await oauth.exchange_code(
            client_id=pending.client_id,
            code=code.strip(),
            code_verifier=pending.code_verifier,
            redirect_uri=pending.redirect_uri,
            client_secret=_client_secret_for(pending.client_id),
        )
    except VercelOAuthError as exc:
        return Connection(status="failed", error=str(exc))

    identity = await _identify(tokens.access_token)
    _save(
        auth_method=AUTH_METHOD_OAUTH,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        scope=tokens.scope,
        identity=identity,
    )
    if not tokens.refresh_token:
        log.warning(
            "Vercel granted no refresh token (scope=%r) — the session will end "
            "when the access token expires.", tokens.scope,
        )
    return Connection(status="connected", auth_method=AUTH_METHOD_OAUTH, identity=identity)


async def _identify(access_token: str) -> Identity:
    """Identity for a new access token: OIDC claims, then /v2/user, then empty."""
    try:
        return await oauth.fetch_userinfo(access_token)
    except VercelOAuthError as exc:
        log.info("Vercel userinfo unavailable (%s); falling back to /v2/user", exc)

    check = await api.whoami(access_token)
    if check.ok and check.identity:
        return check.identity
    return Identity()


async def get_access_token() -> str | None:
    """Return a usable access token, refreshing and persisting when near expiry."""
    entry = get_entry(PROVIDER_KEY) or {}
    access_token = entry.get("access_token")
    if not access_token:
        return None

    expires_at = int(entry.get("expires_at") or 0)
    if not expires_at:
        return access_token

    current = TokenSet(
        access_token=access_token,
        refresh_token=entry.get("refresh_token"),
        expires_at=expires_at,
        scope=entry.get("scope", ""),
    )
    if not current.expired:
        return access_token

    if not current.refresh_token:
        log.info("Vercel access token expired and no refresh token is stored.")
        return None

    client_id = _stored_client().get("client_id")
    if not client_id:
        log.warning("Vercel token needs refreshing but no client is registered.")
        return None

    try:
        refreshed = await oauth.refresh_tokens(
            client_id=client_id,
            refresh_token=current.refresh_token,
            client_secret=_client_secret_for(client_id),
        )
    except VercelOAuthError as exc:
        log.warning("Vercel token refresh failed: %s", exc)
        return None

    entry.update({
        "access_token": refreshed.access_token,
        "refresh_token": refreshed.refresh_token or current.refresh_token,
        "expires_at": refreshed.expires_at,
        "scope": refreshed.scope or entry.get("scope", ""),
    })
    set_entry(PROVIDER_KEY, entry)
    log.info("Refreshed the Vercel access token")
    return refreshed.access_token


async def get_status() -> Connection:
    """Current connection state. OAuth is reported from what's stored; a pasted
    API token is checked live, since only Vercel knows if it was revoked."""
    entry = get_entry(PROVIDER_KEY) or {}
    if not entry.get("access_token"):
        return needs_auth()

    method = entry.get("auth_method", AUTH_METHOD_TOKEN)
    identity = _identity_from_entry(entry)

    if method == AUTH_METHOD_OAUTH:
        token = await get_access_token()
        if not token:
            return Connection(
                status="needs_auth",
                error="The Vercel session expired. Connect again.",
            )
        return Connection(status="connected", auth_method=method, identity=identity)

    check = await api.whoami(entry["access_token"])
    if check.ok:
        return Connection(
            status="connected", auth_method=method, identity=check.identity or identity
        )
    return Connection(
        status="needs_auth" if check.rejected else "failed",
        auth_method=method,
        error=check.error,
    )


async def disconnect() -> Connection:
    """Drop the stored credentials, revoking first when a secret allows it.

    The client registration is kept; it holds no user data.
    """
    entry = get_entry(PROVIDER_KEY) or {}
    token = entry.get("access_token")
    if token and entry.get("auth_method") == AUTH_METHOD_OAUTH:
        client_id = _stored_client().get("client_id", "")
        client_secret = _client_secret_for(client_id) if client_id else None
        if client_id and client_secret:
            try:
                await oauth.revoke_token(
                    client_id=client_id, token=token, client_secret=client_secret
                )
            except VercelOAuthError as exc:
                log.info("Vercel revocation skipped: %s", exc)

    delete_entry(PROVIDER_KEY)
    log.info("Vercel credentials removed from %s", TOKEN_FILE)
    return needs_auth()
