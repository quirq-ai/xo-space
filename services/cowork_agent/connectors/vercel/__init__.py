"""
Vercel connector.

Layers, innermost first:

    oauth.py      the Vercel authorization server: PKCE, registration, tokens
    api.py        the Vercel REST API: who a token belongs to
    connector.py  connection state, persistence, and the flows the routes call

Callers should import from this package rather than reaching into a module.
Credentials are persisted by the shared ``token_store`` under the provider keys
"vercel" and "vercel_client".
"""

from .api import TokenCheck, whoami
from .connector import (
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_TOKEN,
    Authorization,
    Connection,
    complete_authorization,
    connect_with_api_token,
    default_redirect_uri,
    disconnect,
    get_access_token,
    get_status,
    needs_auth,
    start_authorization,
)
from .oauth import Identity, TokenSet, VercelOAuthError, fetch_discovery

__all__ = [
    "AUTH_METHOD_OAUTH",
    "AUTH_METHOD_TOKEN",
    "Authorization",
    "Connection",
    "Identity",
    "TokenCheck",
    "TokenSet",
    "VercelOAuthError",
    "complete_authorization",
    "connect_with_api_token",
    "default_redirect_uri",
    "disconnect",
    "fetch_discovery",
    "get_access_token",
    "get_status",
    "needs_auth",
    "start_authorization",
    "whoami",
]
