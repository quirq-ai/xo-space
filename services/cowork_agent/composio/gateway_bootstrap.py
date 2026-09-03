"""
Boot-time MCP gateway install.

Every agent that exposes an ``mcp_install`` capability gets its config pointed at
this workspace's Composio proxy URL on each start. Before this existed the only
path was a manual ``POST /api/connectors/composio/refresh-gateway``, so a fresh
workspace — or a restart after the proxy token changed — left agents with no
Composio tools and no signal beyond a 401 telling the user to run that endpoint.

Identity at boot: ``install_into_gateway`` wants the same workspace-scoped
principal a request would carry, but there is no request here. The backend already
holds an XO credential, so we resolve it exactly the way ``GET /xo-auth/session/self``
does — ``get_auth_token()`` -> ``/get-user-id`` -> account id -> ``tenancy.scoped_principal``.

Fail closed and quietly: no credential, no workspace identity, or a rejected token
means nothing is installed. A config pointing at an unscoped principal would hand
every workspace of the account one shared Composio tenant, which is the bug the
workspace scoping exists to prevent. Nothing here is fatal to boot.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def install_gateways_at_startup() -> dict[str, dict]:
    """Install the Composio MCP config into every agent that supports it.

    Returns a per-agent result map (also useful in tests); callers at boot ignore
    it and rely on the logs.
    """
    # Local imports: this module is reached from server.py's lifespan, and the
    # composio/auth packages import each other lazily to avoid a load cycle.
    from routers.auth.auth import get_auth_token
    from services import tenancy
    from services.cowork_agent.composio import service as composio_service
    from services.cowork_agent.composio.identity import _validate_token

    agents = composio_service.gateway_install_agents()
    if not agents:
        log.info("gateway_bootstrap: no agent exposes an mcp_install capability; nothing to do.")
        return {}

    token = get_auth_token()
    if not token:
        log.info(
            "gateway_bootstrap: backend holds no XO credential (no XO_API_KEY and no "
            "consumed session); skipping MCP install for %s.", agents,
        )
        return {}

    try:
        account_id = await _validate_token(token)
    except Exception as exc:  # network/JSON faults must not break boot
        log.warning("gateway_bootstrap: token validation failed: %s", exc)
        return {}
    if not account_id:
        log.warning("gateway_bootstrap: XO rejected this backend's credential; skipping install.")
        return {}

    try:
        principal = tenancy.scoped_principal(account_id)
    except tenancy.WorkspaceIdentityUnavailable as exc:
        log.warning(
            "gateway_bootstrap: %s — refusing to install an MCP config bound to the "
            "account-wide bucket. %s is injected by the Coder pod.",
            exc, tenancy.WORKSPACE_ENV,
        )
        return {}

    results: dict[str, dict] = {}
    for agent in agents:
        try:
            result = composio_service.install_into_gateway(principal, agent)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        results[agent] = result

        # print, not log.info: this runs from the lifespan, where the boot summary
        # is the only place anyone looks, and `services.*` loggers are not wired to
        # a handler. Same convention as skill_installer.
        if result.get("ok"):
            if result.get("changed") is False:
                print(f"   Composio MCP: {agent} already current ({result.get('config_path')})")
            else:
                print(f"✅ Composio MCP installed for {agent}: {result.get('config_path')}")
        else:
            # Expected when an agent isn't provisioned on this host — its config
            # file simply doesn't exist. Not an error worth shouting about.
            print(f"⚠️ Composio MCP skipped for {agent}: {result.get('error')}")

    return results
