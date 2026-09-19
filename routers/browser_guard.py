"""Which browser requests may change things in Space.

Every POST/PUT/PATCH/DELETE passes :class:`BrowserWriteGuard` (installed by
server.py through :func:`add_browser_write_guard`), so another website cannot
use the user's browser to write files, secrets or settings. Routes that run
programs or control the server additionally require a same-machine connection
(:func:`is_local_mutation`). Two attacks matter:

- Cross-site requests: a page elsewhere submits a simple POST (a form, a
  multipart upload), which needs no CORS preflight. The browser labels it
  (``Sec-Fetch-Site``, ``Origin``) and the labels do not match this server.
- DNS rebinding: an attacker's hostname resolves to 127.0.0.1, so its page is
  same-origin with this server and the labels do match. Only the hostname
  tells it apart.

A write is accepted when:

- it carries no ``Origin`` and no cross-site ``Sec-Fetch-Site``: a CLI, an
  agent, or another app's server-side proxy, not a browser page; or
- its ``Origin`` is an entry of the CORS allow list (``ALLOWED_ORIGINS``), a
  frontend this API already trusts to read its responses. A ``*`` entry never
  counts here; or
- its ``Origin`` is the address the request was sent to (:func:`origin_allowed`);
  or
- it came through a proxy on this machine (TCP peer is loopback) and its
  ``Origin`` is the address in ``X-Forwarded-Host``: a frontend's own server
  proxying to Space, e.g. Next.js rewrites, which set ``Host`` to the target
  and ``X-Forwarded-Host`` to the host the browser used. A page cannot add that
  header to a cross-site request without a CORS preflight, and a remote caller
  is not a loopback peer.

"The address it was sent to" must also be one rebinding cannot reach:

- ``localhost`` or an IP literal (rebinding needs a DNS name), compared by
  scheme, host and port; or
- an ``https`` hostname. The server listens on plain http, so an https Origin
  means the browser reached a TLS-terminating proxy that answers for that name
  (e.g. a Coder app URL). A rebinding page cannot produce one: its browser
  would attempt TLS against the plain-http port. The addressed host must carry
  no port or the Origin's port. The request's own scheme is not consulted for
  hostnames, because it comes from ``X-Forwarded-Proto``, which a page can set.

"Loopback peer" means the TCP connection, not ``request.client``. A proxy on
the same machine (the Coder agent) connects from 127.0.0.1 and sends
``X-Forwarded-For``; uvicorn's proxy-header handling would overwrite
``scope["client"]`` with that before the app runs. So server.py starts uvicorn
with ``proxy_headers=False`` and calls :func:`add_forwarding_middleware`, which
records the TCP peer and then applies the same forwarding inside the app:
the rest of the app still sees the forwarded client and scheme.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

_LOOPBACK_PEERS = ("127.0.0.1", "::1", "localhost")
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Scope key holding the TCP peer as the server reported it, before forwarding.
TRANSPORT_CLIENT_KEY = "xo.transport_client"

REFUSED_DETAIL = (
    "This change came from another website, so Space refused it. Make changes from "
    "Space's own pages, or add a trusted frontend's origin to ALLOWED_ORIGINS."
)


class RecordTransportClient:
    """Copy ``scope["client"]`` aside before any proxy-header rewrite."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope[TRANSPORT_CLIENT_KEY] = scope.get("client")
        await self.app(scope, receive, send)


def add_forwarding_middleware(app) -> None:
    """Apply proxy headers inside the app, after recording the TCP peer.

    Pair with ``uvicorn.run(..., proxy_headers=False)``. Trusts the same hosts
    uvicorn would (``FORWARDED_ALLOW_IPS``, default 127.0.0.1).
    """
    app.add_middleware(ProxyHeadersMiddleware,
                       trusted_hosts=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
    app.add_middleware(RecordTransportClient)  # added last, so it runs first


def is_loopback_peer(request: Request) -> bool:
    # Without the recorder (a bare test app) the scope client is the TCP peer;
    # if uvicorn rewrote it anyway, this fails closed.
    client = request.scope.get(TRANSPORT_CLIENT_KEY, request.scope.get("client"))
    host = client[0] if client else ""
    return host in _LOOPBACK_PEERS


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _is_localhost_or_ip(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _parse_origin(origin: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of a well-formed http(s) Origin, else None."""
    if any(char.isspace() for char in origin):
        return None
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else _default_port(parsed.scheme)
    except ValueError:
        return None
    return parsed.scheme, host, port


def _names_address(origin: tuple[str, str, int], host: str | None, port: int | None, scheme: str) -> bool:
    """The Origin is the address ``host[:port]`` and not one rebinding can reach."""
    origin_scheme, origin_host, origin_port = origin
    if origin_host != host:
        return False
    if _is_localhost_or_ip(origin_host):
        return (origin_scheme, origin_port) == (scheme, port if port is not None else _default_port(scheme))
    return origin_scheme == "https" and port in (None, origin_port)


def _same_origin_fetch(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site is None or fetch_site == "same-origin"


def origin_allowed(request: Request) -> bool:
    """True for a caller without an Origin, or a browser request rebinding cannot forge."""
    if not _same_origin_fetch(request):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    parsed = _parse_origin(origin)
    if parsed is None:
        return False
    try:
        port = request.url.port
    except ValueError:
        return False
    return _names_address(parsed, request.url.hostname, port, request.url.scheme)


def _forwarded_address(request: Request) -> tuple[str, int | None] | None:
    """Host and port from ``X-Forwarded-Host`` (the first entry), else None."""
    value = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    if not value or any(char.isspace() for char in value):
        return None
    try:
        parts = urlsplit("//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or parts.path or parts.query or parts.fragment or parts.username is not None:
        return None
    return host, port


def _proxied_origin_allowed(request: Request) -> bool:
    """A same-machine proxy's request whose Origin is the host the browser used."""
    if not is_loopback_peer(request) or not _same_origin_fetch(request):
        return False
    addressed = _forwarded_address(request)
    parsed = _parse_origin(request.headers.get("origin", ""))
    if addressed is None or parsed is None:
        return False
    return _names_address(parsed, addressed[0], addressed[1], request.url.scheme)


def _trusted_write_origins(origins: Iterable[str]) -> frozenset[str]:
    """CORS allow-list entries as browsers serialise an Origin; ``*`` and ``null`` never count."""
    normalized = (origin.strip().rstrip("/").lower() for origin in origins)
    return frozenset(origin for origin in normalized if origin and origin not in {"*", "null"})


def write_allowed(request: Request, trusted_origins: frozenset[str]) -> bool:
    """Whether a POST/PUT/PATCH/DELETE may reach its route (see the module docstring)."""
    origin = request.headers.get("origin")
    if origin is None:
        return _same_origin_fetch(request)
    if origin.strip().rstrip("/").lower() in trusted_origins:
        return True
    return origin_allowed(request) or _proxied_origin_allowed(request)


class BrowserWriteGuard:
    """Refuse cross-site browser writes on every route before the route runs."""

    def __init__(self, app: ASGIApp, trusted_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.trusted_origins = _trusted_write_origins(trusted_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and scope["method"] in _WRITE_METHODS
                and not write_allowed(Request(scope), self.trusted_origins)):
            await JSONResponse({"detail": REFUSED_DETAIL}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def add_browser_write_guard(app, trusted_origins: Iterable[str]) -> None:
    """Guard every write. Call before :func:`add_forwarding_middleware`, so the
    TCP peer is recorded (outermost) before this runs."""
    app.add_middleware(BrowserWriteGuard, trusted_origins=list(trusted_origins))


def is_local_mutation(request: Request) -> bool:
    """A loopback peer whose request, if it came from a browser, passes :func:`origin_allowed`."""
    return is_loopback_peer(request) and origin_allowed(request)
