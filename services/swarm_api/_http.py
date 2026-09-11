"""The one HTTP door to xo-swarm-api.

Everything xo-space says to the swarm goes through `request()`: this module
is the only place that knows the base URL (CHAT_API_BASE_URL), how the
bearer token is obtained, the timeouts, and how a failure is shaped. A
retry policy, a request log line, or a host move is a change here, once.

`request()` never raises. Callers get a SwarmResult and decide what a
failure means for their feature (park, retry next tick, 502 to the UI).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api-swarm-beta.xo.builders"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def base_url() -> str:
    """CHAT_API_BASE_URL without a trailing slash. Read per call so a test or
    a runtime override does not need a re-import."""
    return (os.getenv("CHAT_API_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/") or DEFAULT_BASE_URL


def auth_token() -> str | None:
    """The bearer token for the swarm: XO_API_KEY when set, else the in-memory
    session token. Lazy import: routers.auth owns the token state and pulls in
    the FastAPI app, which unit tests must not load."""
    try:
        from routers.auth.auth import get_auth_token
        return get_auth_token() or None
    except Exception:  # noqa: BLE001 — no auth module == not signed in
        return None


def auth_headers() -> dict[str, str]:
    tok = auth_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


@dataclass
class SwarmResult:
    """What a swarm call came back with, never an exception.

    ok            2xx and a parseable body (or no body expected)
    status        HTTP status; 0 when the request never reached the swarm
    data          parsed JSON body (dict/list) or None
    text          raw body, for callers that pass an upstream message through
    detail        one human sentence: the swarm's `detail` when it sent one,
                  else "swarm returned NNN", else the transport error
    offline       the request never reached the swarm (DNS, refused, timeout)
    unauthenticated  no token was available so the request was not sent
    """
    ok: bool
    status: int = 0
    data: Any = None
    text: str = ""
    detail: str = ""
    offline: bool = False
    unauthenticated: bool = False
    headers: dict = field(default_factory=dict)


def _detail_from(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        d = body.get("detail") if isinstance(body, dict) else None
        if isinstance(d, str) and d:
            return d
        if isinstance(d, dict):
            msg = d.get("message") or d.get("error")
            if isinstance(msg, str) and msg:
                return msg
    except Exception:  # noqa: BLE001
        pass
    return f"swarm returned {resp.status_code}"


async def request(
    method: str,
    path: str,
    *,
    json: Any = None,
    params: dict | None = None,
    auth: bool = True,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> SwarmResult:
    """One call to the swarm. `auth=True` (the default) attaches the bearer
    token and refuses to send at all when there is none, so an unauthenticated
    install can never leak a request; `auth=False` is for the browser-auth
    handshake, which is how a token is obtained in the first place."""
    headers: dict[str, str] = {}
    if auth:
        headers = auth_headers()
        if not headers:
            return SwarmResult(ok=False, detail="not signed in to XO", unauthenticated=True)
    url = f"{base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json, params=params, headers=headers)
    except Exception as exc:  # noqa: BLE001 — network failure is a result, not a crash
        log.debug("swarm_api: %s %s failed: %s", method, path, exc)
        return SwarmResult(ok=False, detail=f"swarm is unreachable: {exc}", offline=True)
    text = resp.text
    data: Any = None
    if text:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON body is still a response
            data = None
    ok = 200 <= resp.status_code < 300
    return SwarmResult(
        ok=ok, status=resp.status_code, data=data, text=text,
        detail="" if ok else _detail_from(resp),
        headers=dict(resp.headers),
    )
