"""Connectors: the accounts and tools a Space is connected to.

One package per connector, each owning its whole surface and re-exporting
it from its ``__init__``, so callers import the connector, not its
internals (``from modules.connectors.github import get_github_token``):

* :mod:`composio`   Composio toolkits: the bring-your-own key (``byo_key``),
                    the SDK client, the session and MCP proxy token
                    (``service``), which accounts this workspace uses
                    (``space_scope``), per-toolkit tool preferences
                    (``action_prefs``), the toolkit catalog (``categories``),
                    the agent-side MCP gateway install (``mcp``) and the
                    browser identity gate (``identity``).
* :mod:`gdrive`     Google Drive, rclone-backed (``provider``).
* :mod:`onedrive`   OneDrive, rclone-backed (``provider``).
* :mod:`github`     GitHub: a pasted token (``pat``) or the ``gh`` device flow
                    (``cli_auth``) over one store (``common``); ``issues`` and
                    ``issue_actions`` read and act on issues with that token.
* :mod:`vercel`     Vercel: PKCE OAuth (``oauth``), the REST API (``api``) and
                    the connection state (``connector``).

Two shared pieces sit beside them, deliberately not connectors: :mod:`rclone`
(the engine gdrive and onedrive both drive, with ``oauth_lock`` arbitrating
the one callback port) and :mod:`token_store` (the single owner of
``secrets/token.json``).

The contract files of the module (``modules/connectors``):

* :mod:`service`   the facade: every connector's status (:func:`status`) and
                   the per-connector status the routes answer. The OAuth,
                   device and token flows stay in their packages, called
                   from the routes as before.
* :mod:`routes`    one router over the eight connector routers under
                   ``routers/`` (``/api/connectors/*``, ``/mcp/*``,
                   ``/callback``, ``/.well-known/*``), in the order the
                   broker mounted them for years.
* :mod:`tasks`     the supervised MCP gateway reconcile loop (``mcp_gateway``).
* :mod:`commands`  ``python -m quirq connectors status``.
* :mod:`store`     ``FILES`` is empty: every credential lives outside the
                   state root or in the settings module's ``token.json``.
* :mod:`events`    no types, no signals yet.

Credential stores never live in the checkout: ``~/.quirq/secrets/token.json``
(``token_store``, kept by uninstall), ``~/.config/rclone/rclone.conf``
(rclone's own default) and ``~/.config/composio/`` (``composio/paths.py``).
Files left at older locations are moved into place on first access.

``services.cowork_agent.connectors`` and ``routers.cowork_agent.connectors``
are aliases of this package's modules for one release. Core code: names no
agent and imports nothing from the adapters tree.
"""
