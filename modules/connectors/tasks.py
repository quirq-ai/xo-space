"""The MCP gateway reconcile loop, supervised.

``composio.service.gateway_reconcile_loop`` points every agent that supports
it at this workspace's Composio MCP proxy and checks again every
``COMPOSIO_MCP_RECONCILE_INTERVAL`` seconds; a page load can kick a sweep
sooner through ``kick_gateway_sweep``. Which agents take the gateway is
resolved inside ``composio.mcp`` through the agent registry; no agent is
named here. The switch in ``module.json`` (``tasks.mcp_gateway``) is the
documented way to turn the loop off.
"""

from __future__ import annotations

from services.supervisor import Task

from .composio import service as composio_service


async def _mcp_gateway() -> None:
    await composio_service.gateway_reconcile_loop()


TASKS = [
    Task("mcp_gateway", _mcp_gateway,
         description="Points every agent that supports it at this workspace's Composio MCP proxy and keeps it that way."),
]
