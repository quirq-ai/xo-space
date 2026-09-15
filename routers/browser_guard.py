"""Who may call Space's dangerous routes: process control, saved commands, project folders.

Those routes run programs or change the machine, so another website must not
be able to trigger them through the user's browser. Two attacks matter:

- Cross-site requests: a page elsewhere submits a simple POST, which needs no
  CORS preflight. The browser labels it (``Sec-Fetch-Site``, ``Origin``) and
  the labels do not match this server.
- DNS rebinding: an attacker's hostname resolves to 127.0.0.1, so its page is
  same-origin with this server and the labels do match. Only the hostname
  tells it apart.

A browser request is accepted when its Origin is exactly the address it was
sent to, and that address cannot be a rebinding target:

- ``localhost`` or an IP literal (rebinding needs a DNS name), compared by
  scheme, host and port; or
- an ``https`` hostname. The server listens on plain http, so an https Origin
  means the browser reached a TLS-terminating proxy that answers for that name
  (e.g. a Coder app URL). A rebinding page cannot produce one: its browser
  would attempt TLS against the plain-http port. The request's Host must carry
  no port or the Origin's port. The request's own scheme is not consulted,
  because uvicorn takes it from ``X-Forwarded-Proto``, which a page can set.

Callers without an Origin (CLI, agent, curl) are not browsers and pass.

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
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

_LOOPBACK_PEERS = ("127.0.0.1", "::1", "localhost")

# Scope key holding the TCP peer as the server reported it, before forwarding.
TRANSPORT_CLIENT_KEY = "xo.transport_client"


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


def origin_allowed(request: Request) -> bool:
    """True for a caller without an Origin, or a browser request rebinding cannot forge."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if any(char.isspace() for char in origin):
        return False
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return False
        origin_port = parsed.port if parsed.port is not None else _default_port(parsed.scheme)
        request_port = request.url.port
    except ValueError:
        return False
    if host != request.url.hostname:
        return False
    if _is_localhost_or_ip(host):
        if request_port is None:
            request_port = _default_port(request.url.scheme)
        return (parsed.scheme, origin_port) == (request.url.scheme, request_port)
    return parsed.scheme == "https" and request_port in (None, origin_port)


def is_local_mutation(request: Request) -> bool:
    """A loopback peer whose request, if it came from a browser, passes :func:`origin_allowed`."""
    return is_loopback_peer(request) and origin_allowed(request)
