"""Swarm calls for the daily usage report."""
from __future__ import annotations

from ._http import SwarmResult, request

REPORT_PATH = "/usage/report"


async def probe_key() -> SwarmResult:
    """Verify the token before any usage data leaves the machine: the same
    endpoint with an empty record list passes through exactly the auth
    dependency the real report does and stores nothing on success."""
    return await request("POST", REPORT_PATH, json={"records": []})


async def report(records: list) -> SwarmResult:
    return await request("POST", REPORT_PATH, json={"records": records})
