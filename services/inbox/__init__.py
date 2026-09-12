"""Space inbox: one human-readable ``~/.quirq/inbox.json`` holding
information that arrived in the workspace, its seen/done state, and the
feeder cursors.

Three modules, one router-facing surface:

* :mod:`store`    the file (normalisation, validation, retention, locked
                  read-modify-write, keyed upserts).
* :mod:`feeders`  best-effort readers that turn the workspace timeline,
                  per-project todos, the sharing relay, each project's
                  GitHub issue mirror and the per-toolkit ``events.jsonl``
                  of polled connections into items.
* :mod:`service`  what ``routers/cowork_agent/bff/inbox.py`` imports:
                  ``refresh``, ``list_items``, ``create_item``,
                  ``update_item``, ``delete_item`` and ``InboxError``.

Core code: names no agent and imports nothing from the adapters tree.
"""
