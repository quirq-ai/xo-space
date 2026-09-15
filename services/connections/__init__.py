"""Connections polling: one folder per Composio toolkit under
``~/.quirq/connections/<toolkit>/`` holding a hand-editable ``config.json``
(on/off, interval, which data to collect), a ``state.json`` (cursors and
the last result) and an append-only ``events.jsonl`` of collected items.
The inbox's ``connections`` feeder turns those events into inbox items.
Beside the folders, ``accounts.json`` remembers which account each
toolkit's session is bound to (the label a poll resolves through the
toolkit's identity tool), separate from the polling state.

Five modules, one router-facing surface:

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
* :mod:`service`     what ``routers/cowork_agent/bff/connections.py``
                     imports: ``list_connections``, ``get_connection``,
                     ``configure``, ``events``, ``remove``, ``poll_now``,
                     ``refresh_account``, ``signed_in``, ``poller_enabled``,
                     ``ConnectionsError`` and ``register_new_events_listener``.

Built on the shared Space modules: ``services.storage`` (the locked,
atomic file primitives and the state root), ``services.timestamps``
(``parse_ts``, ``iso``, ``now_iso``), ``services.errors``
(``ConnectionsError`` is a ``ServiceError``) and ``services.periodic``
(the poller's loop). Imports nothing from the inbox package: the inbox
registers a listener through ``service.register_new_events_listener``.

Core code: names no agent and imports nothing from the adapters tree.
"""
