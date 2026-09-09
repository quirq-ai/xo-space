"""HTTP transport to xo-swarm-api's Composio proxy routes.

xo-swarm-api holds the Composio SDK client and the org-wide API key; this module is the
only place in xo-space that talks to it. There is no local Composio client anymore —
every operation service.py used to run against `Composio(api_key=...)` directly is now
one call here.

Kept synchronous and styled on the retired `credentials.py`'s `_fetch_from_swarm()`/`_get()`
(same timeout, same `get_auth_token()` + `swarm_api.base_url()` pattern) so every caller in
`service.py` keeps calling these functions the same way it called the SDK — no router or
call-site signature changes.

Every message this module raises for an authoritative failure (no key configured on
xo-swarm-api, or this backend's XO credential rejected) contains the literal string
``COMPOSIO_API_KEY`` — load-bearing, see the retired `credentials.py`'s docstring and
``space_ui/js/views/connectors.js``.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_PREFIX = "/connectors/composio"


class SwarmComposioError(RuntimeError):
    """A Composio operation on xo-swarm-api failed.

    ``authoritative`` mirrors the retired ``CredentialsUnavailable``: the owner answered
    and said either "there is no key" (503) or "not you" (401/403) — never masked, never
    retried past it. Anything else (network failure, 5xx, an unreadable body) is transient.
    """

    def __init__(self, message: str, *, authoritative: bool = False) -> None:
        super().__init__(message)
        self.authoritative = authoritative


class SwarmComposioNotFound(SwarmComposioError):
    """The swarm answered 404: not owned by this caller, or does not exist.

    Deliberately not "authoritative" in the credentials sense — this is a per-resource
    outcome, not a statement about whether Composio is configured at all.
    """


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(body.get("detail") or "")
    return ""


def _interpret(resp: httpx.Response, url: str) -> Any:
    if resp.status_code == 503:
        raise SwarmComposioError(
            f"COMPOSIO_API_KEY is not configured on xo-swarm-api (its {url} returned "
            f"503: {_detail(resp)}).",
            authoritative=True,
        )
    if resp.status_code in (401, 403):
        raise SwarmComposioError(
            f"xo-swarm-api rejected this backend's XO credential (HTTP "
            f"{resp.status_code}), so COMPOSIO_API_KEY could not be used. Fix or "
            "replace XO_API_KEY.",
            authoritative=True,
        )
    if resp.status_code == 404:
        raise SwarmComposioNotFound(_detail(resp) or "Not found.")
    if resp.status_code >= 400:
        raise SwarmComposioError(
            f"Composio request to {url} failed (HTTP {resp.status_code}): "
            f"{_detail(resp) or resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:
        raise SwarmComposioError(
            f"xo-swarm-api returned an unreadable response from {url}: "
            f"{type(exc).__name__}."
        ) from exc


def _send(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
) -> httpx.Response:
    """The one HTTP seam, so tests patch a single function (mirrors the retired
    ``credentials.py``'s ``_get``)."""
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        return client.request(method, url, headers=headers, json=json, params=params)


def _request(
    method: str,
    path: str,
    *,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Any:
    # Deferred imports: this package and routers.auth import each other lazily to avoid a
    # load cycle (same reason the retired credentials.py deferred them).
    from routers.auth.auth import get_auth_token
    from services import swarm_api

    token = get_auth_token()
    if not token:
        raise SwarmComposioError(
            "COMPOSIO_API_KEY comes from xo-swarm-api and this backend holds no XO "
            "credential. Set XO_API_KEY, or sign in to XO.",
            authoritative=True,
        )

    url = f"{swarm_api.base_url()}{path}"
    try:
        resp = _send(method, url, {"Authorization": f"Bearer {token}"}, json=json, params=params)
    except Exception as exc:
        raise SwarmComposioError(
            f"COMPOSIO_API_KEY could not be reached at {url}: {exc}. Check the swarm "
            "base URL and that xo-swarm-api is reachable."
        ) from exc

    return _interpret(resp, url)


def connect(
    toolkit_id: str,
    *,
    auth_scheme: str,
    redirect_uri: str,
    alias: Optional[str],
    allow_multiple: bool,
) -> dict[str, Any]:
    return _request(
        "POST",
        f"{_PREFIX}/toolkits/{toolkit_id}/connect",
        json={
            "auth_scheme": auth_scheme,
            "redirect_uri": redirect_uri,
            "alias": alias,
            "allow_multiple": allow_multiple,
        },
    )


def connection_status(connection_request_id: str) -> dict[str, Any]:
    return _request("GET", f"{_PREFIX}/connection-requests/{connection_request_id}")


def list_connections(
    *,
    statuses: Optional[list[str]] = None,
    toolkit_slugs: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if statuses:
        params["statuses"] = statuses
    if toolkit_slugs:
        params["toolkit_slugs"] = toolkit_slugs
    data = _request("GET", f"{_PREFIX}/connections", params=params or None)
    return data.get("connections") or []


def set_alias(connected_account_id: str, alias: Optional[str]) -> Optional[str]:
    data = _request(
        "PUT", f"{_PREFIX}/connections/{connected_account_id}/alias", json={"alias": alias},
    )
    return data.get("alias")


def disconnect(connected_account_id: str) -> None:
    _request("DELETE", f"{_PREFIX}/connections/{connected_account_id}")


def list_tools(toolkit_id: str) -> list[dict[str, Any]]:
    data = _request("GET", f"{_PREFIX}/toolkits/{toolkit_id}/tools")
    return data.get("tools") or []


def create_session(config: dict[str, Any]) -> dict[str, Any]:
    return _request("POST", f"{_PREFIX}/sessions", json=config)


def update_session(session_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return _request("PUT", f"{_PREFIX}/sessions/{session_id}", json=config)


def delete_session(session_id: str) -> None:
    _request("DELETE", f"{_PREFIX}/sessions/{session_id}")
