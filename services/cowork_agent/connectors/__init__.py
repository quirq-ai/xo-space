"""
External-service connectors for the cowork_agent subsystem.

One package per connector — each owns its whole surface and re-exports it from
its ``__init__``, so callers import the connector, not its internals::

    from services.cowork_agent.connectors.github import get_github_token

    gdrive/    Google Drive          (provider.py — rclone-backed)
    onedrive/  OneDrive              (provider.py — rclone-backed)
    github/    GitHub                (common.py + pat.py + cli_auth.py)
    vercel/    Vercel                (oauth.py + api.py + connector.py)
    manus/     Manus                 (connector.py — API key)

Two shared pieces sit alongside them, deliberately not connectors:

    rclone/       the engine gdrive and onedrive both drive
    token_store   the single owner of ``token.json``

Both credential stores live in the user's config directory, never the checkout:
``~/.config/token.json`` (token_store) and ``~/.config/rclone/rclone.conf``
(rclone's own default). Files left at the old ``services/`` locations are moved
on startup.

These are all agent-agnostic; their HTTP surfaces live in the matching
``routers/cowork_agent/connectors/`` modules.
"""
