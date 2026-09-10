"""Swarm calls for project sharing (the commit relay). Same five operations
the relay poller and the BFF need; each returns plain values, never raises,
so a swarm outage cannot break the loop."""
from __future__ import annotations

from ._http import request


async def report_commits(repo: str, workspace_id: str, hashes: list[str]) -> bool:
    """Announce hashes this workspace has seen. 403 (not a member / revoked)
    is an expected steady state, reported as False like any other failure."""
    if not hashes:
        return True
    res = await request("POST", "/commits", json={"repo": repo, "workspace_id": workspace_id, "commits": hashes})
    return res.ok


async def poll(workspace_id: str, cursors: dict[str, int]) -> dict | None:
    """One poll covers every repo. None on any failure (the caller records a
    failed poll and retries next tick)."""
    res = await request("POST", "/commits/poll", json={"workspace_id": workspace_id, "cursors": cursors or {}})
    return res.data if res.ok and isinstance(res.data, dict) else None


async def share(repo: str, owner_workspace_id: str, shared_workspace_id: str) -> tuple[bool, int, str]:
    res = await request("POST", "/commits/share", json={
        "repo": repo, "owner_workspace_id": owner_workspace_id, "shared_workspace_id": shared_workspace_id})
    return res.ok, res.status, "" if res.ok else res.detail


async def revoke(repo: str, shared_workspace_id: str) -> tuple[bool, int, str]:
    res = await request("POST", "/commits/revoke", json={"repo": repo, "shared_workspace_id": shared_workspace_id})
    return res.ok, res.status, "" if res.ok else res.detail


async def members(repo: str) -> tuple[bool, int, dict | str]:
    res = await request("GET", "/commits/members", params={"repo": repo})
    if not res.ok:
        return False, res.status, res.detail
    if not isinstance(res.data, dict):
        return False, res.status, "bad response body"
    return True, res.status, res.data
