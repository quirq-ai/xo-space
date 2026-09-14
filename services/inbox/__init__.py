"""Space inbox: one human-readable ``~/.quirq/inbox/inbox.json`` holding
information that arrived in the workspace, its seen/done state, and the
feeder cursors.

Four modules, one router-facing surface:

* :mod:`policy`   the declarative loading rules: per source, the cold-start
                  window, rows read per run, cursor future-slack, and the
                  per-source retention quota. Read by ``feeders`` and
                  ``store``; imports nothing from the package.
* :mod:`store`    the file (normalisation, validation, retention, locked
                  read-modify-write, keyed upserts).
* :mod:`feeders`  best-effort readers that turn the workspace timeline,
                  per-project todos, the sharing relay, each project's
                  GitHub issue mirror and the per-toolkit ``events.jsonl``
                  of polled connections into items.
* :mod:`service`  what ``routers/cowork_agent/bff/inbox.py`` imports:
                  ``refresh``, ``list_items``, ``create_item``,
                  ``update_item``, ``update_many`` (the batch
                  ``PATCH /api/inbox``), ``delete_item`` and ``InboxError``.

Built on the shared Space modules: ``services.storage`` (the locked,
atomic file primitives and the state root), ``services.timestamps``
(``parse_ts``, ``now_iso``) and ``services.errors`` (``InboxError`` is a
``ServiceError``). Reads ``services.connections`` (the ``connections``
feeder, and ``service`` registers a new-events listener there); that
package never imports this one.

Core code: names no agent and imports nothing from the adapters tree.
"""
