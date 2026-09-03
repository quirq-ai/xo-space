"""HTTP client for swarm's workspace-anchored relay endpoints. Never raises:
a swarm outage must not break the poller. Auth via config.auth_token()."""
from __future__ import annotations

import logging
import os

import httpx

from . import config

log = logging.getLogger(__name__)


def _base_url() -> str:
    return os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders").rstrip("/")


def _headers() -> dict[str, str]:
    tok = config.auth_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _detail(resp: httpx.Response) -> str:
    try:
        d = resp.json().get("detail")
        if isinstance(d, str) and d:
            return d
    except Exception:  # noqa: BLE001
        pass
    return f"swarm returned {resp.status_code}"


async def _post(path: str, payload: dict) -> httpx.Response | None:
    headers = _headers()
    if not headers:
        log.debug("commit_relay: no swarm auth token for %s", path)
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            return await client.post(f"{_base_url()}{path}", json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.debug("commit_relay: %s failed: %s", path, exc)
        return None


async def report_commits(repo: str, workspace_id: str, hashes: list[str]) -> bool:
    if not hashes:
        return True
    resp = await _post("/commits", {"repo": repo, "workspace_id": workspace_id, "commits": hashes})
    if resp is None:
        return False
    if resp.status_code == 403:
        # Not shared / revoked: an expected steady state, not an error.
        return False
    return resp.status_code < 400


async def poll(workspace_id: str, cursors: dict[str, int]) -> dict | None:
    resp = await _post("/commits/poll", {"workspace_id": workspace_id, "cursors": cursors or {}})
    if resp is None or resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


async def share(repo: str, owner_workspace_id: str, shared_workspace_id: str) -> tuple[bool, int, str]:
    resp = await _post("/commits/share", {"repo": repo, "owner_workspace_id": owner_workspace_id,
                                          "shared_workspace_id": shared_workspace_id})
    if resp is None:
        return False, 0, "swarm is unreachable"
    ok = resp.status_code < 400
    return ok, resp.status_code, "" if ok else _detail(resp)


async def revoke(repo: str, shared_workspace_id: str) -> tuple[bool, int, str]:
    resp = await _post("/commits/revoke", {"repo": repo, "shared_workspace_id": shared_workspace_id})
    if resp is None:
        return False, 0, "swarm is unreachable"
    ok = resp.status_code < 400
    return ok, resp.status_code, "" if ok else _detail(resp)


async def members(repo: str) -> tuple[bool, int, dict | str]:
    headers = _headers()
    if not headers:
        return False, 0, "not signed in to XO"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_base_url()}/commits/members", params={"repo": repo}, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return False, 0, f"swarm is unreachable: {exc}"
    if resp.status_code >= 400:
        return False, resp.status_code, _detail(resp)
    try:
        return True, resp.status_code, resp.json()
    except Exception:  # noqa: BLE001
        return False, resp.status_code, "bad response body"
