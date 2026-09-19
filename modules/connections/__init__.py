"""Connections polling: one folder per Composio toolkit under
``~/.quirq/connections/<toolkit>/`` holding a hand-editable ``config.json``
(on/off, interval, which data to collect), a ``state.json`` (cursors and
the last result) and an append-only ``events.jsonl`` of collected items.
The Work (and, until it lands, the retired inbox) reads those events in place.
Beside the folders, ``accounts.json`` remembers which account each
toolkit's session is bound to (the label a poll resolves through the
toolkit's identity tool), separate from the polling state.

The contract files of the module (``modules/connections``):

* :mod:`store`       the three files (validation, defaults, locked writes,
                     rotation, newest-first reads, safe removal) and the
                     ``accounts.json`` cache.
* :mod:`collectors`  the data-driven catalog: which read-only tool to call
                     per toolkit and how to map its answer to event lines,
                     plus the identity catalog (which tool names the
                     connected account and where its label sits).
* :mod:`mcp_client`  a minimal streamable-HTTP JSON-RPC client against the
                     Composio MCP upstream: ``McpSession`` runs the handshake
                     once and any number of ``tools/list`` and ``tools/call``
                     (``execute_tool`` routes through Composio's executor
                     tool), plus one-shot ``call_tool`` / ``list_tools`` /
                     ``execute_tool`` wrappers.
* :mod:`poller`      the background loop: opens one session per poll,
                     resolves a stale account label on it, runs each due
                     connection's collectors, dedupes on seen keys, records
                     the outcome; ``refresh_account`` resolves the label
                     on demand.
* :mod:`service`     the only surface ``routes.py``, ``tasks.py``, ``stream.py``,
                     ``commands.py`` and other modules call: ``list_connections``,
                     ``get_connection``, ``configure``, ``events``, ``remove``,
                     ``poll_now``, ``refresh_account``, ``signed_in``,
                     ``poller_enabled``, ``ConnectionsError`` and
                     ``register_new_events_listener``.
* :mod:`routes`      ``/api/connections`` and its six routes; ``stream`` the
                     merged events feed; ``tasks`` the supervised poller;
                     ``commands`` ``python -m quirq connections ...``;
                     ``events`` the ``new_events`` signal; ``store.FILES`` the
                     file table; ``pages/connections.json`` the Inbox page.

Built on the shared Space modules: ``services.storage`` (the locked,
atomic file primitives and the state root), ``services.timestamps``
(``parse_ts``, ``iso``, ``now_iso``), ``services.errors``
(``ConnectionsError`` is a ``ServiceError``) and ``services.periodic``
(the poller's loop) and ``services.signals`` (``connections.new_events``
fires after any poll that collected something, from the tick and from
``poll_now``). Imports nothing from any other module; ``services.connections``
is an alias of this package for one release.

Core code: names no agent and imports nothing from the adapters tree.
"""
