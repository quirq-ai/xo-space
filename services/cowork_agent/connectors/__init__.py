"""
External-service connectors for the cowork_agent subsystem.

One package per connector — each owns its whole surface and re-exports it from
its ``__init__``, so callers import the connector, not its internals::

    from services.cowork_agent.connectors.github import get_github_token

    gdrive/    Google Drive          (provider.py — rclone-backed)
    onedrive/  OneDrive              (provider.py — rclone-backed)
    github/    GitHub                (common.py + pat.py + cli_auth.py)
    vercel/    Vercel                (oauth.py + api.py + connector.py)
    composio/  Composio              (service.py + identity.py + session_identity.py
                                       + mcp.py + categories.py + action_prefs.py + paths.py;
                                       the swarm HTTP transport lives in
                                       services/swarm_api/composio.py alongside the rest of
                                       the swarm clients)

Two shared pieces sit alongside them, deliberately not connectors:

    rclone/       the engine gdrive and onedrive both drive
    token_store   the single owner of ``token.json``

Credential stores live in the user's config directory, never the checkout:
``~/.config/token.json`` (token_store), ``~/.config/rclone/rclone.conf`` (rclone's
own default), and ``~/.config/composio/{sessions,action_prefs}.json`` (see
``composio/paths.py``). Files left at the old ``services/`` and ``data/``
locations are moved into place on first access.

These are all agent-agnostic; their HTTP surfaces live in the matching
``routers/cowork_agent/connectors/`` modules — for Composio, both
``composio.py`` and ``composio_mcp_proxy.py``.
"""
