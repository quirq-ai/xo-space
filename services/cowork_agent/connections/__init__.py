"""Connections polling: one folder per Composio toolkit under
``~/.quirq/connections/<toolkit>/`` holding a hand-editable ``config.json``
(on/off, interval, which data to collect), a ``state.json`` (cursors and
the last result) and an append-only ``events.jsonl`` of collected items.
The inbox's ``connections`` feeder turns those events into inbox items.

Five modules, one router-facing surface:

* :mod:`store`       the three files (validation, defaults, locked writes,
                     rotation, newest-first reads, safe removal).
* :mod:`collectors`  the data-driven catalog: which read-only tool to call
                     per toolkit and how to map its answer to event lines.
* :mod:`mcp_client`  a minimal streamable-HTTP JSON-RPC client for one
                     ``tools/call`` against the Composio MCP upstream.
* :mod:`poller`      the background loop: runs each due connection's
                     collectors, dedupes on seen keys, records the outcome.
* :mod:`service`     what ``routers/cowork_agent/bff/connections.py``
                     imports: ``list_connections``, ``get_connection``,
                     ``configure``, ``events``, ``remove``, ``poll_now``,
                     ``signed_in``, ``poller_enabled`` and ``ConnectionsError``.

Core code: names no agent and imports nothing from the adapters tree.
"""
