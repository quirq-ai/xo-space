"""Space inbox: what arrived in the workspace, as work items with a session
each (docs/work-and-workitems.md section 18).

Since 2026-09-21 the Inbox keeps no rows of its own. A fact that arrives
(a mail, a calendar event, a GitHub issue, a share event, an agent's post)
becomes a work item in a project at ingestion, the watcher's session data
and the runner's sidecars are joined to it on read, and the tabs of the
Inbox are facets over that one list.

Four modules, one router-facing surface:

* :mod:`ledger`   ``~/.quirq/inbox/ledger.json``: the feeders' cursors and
                  switches, and ``InboxError``.
* :mod:`facts`    a fact and the work item it becomes: validation, the
                  target project, the dedup by source key, and the
                  ``fact.json`` sidecar under the project's runtime root.
* :mod:`feeders`  best-effort readers that turn the sharing relay, each
                  project's GitHub issue mirror and the per-toolkit
                  ``events.jsonl`` of polled connections into facts (the
                  workspace stream of sessions, todos and files is the
                  Activity page's, never the Inbox's).
* :mod:`service`  what ``routers/cowork_agent/bff/inbox.py`` imports:
                  ``refresh``, ``create_post``, the rows and sections of
                  the Inbox, the policies, one item's detail, and the
                  actions on it (reply, start, send, archive, reopen), the
                  last of which it hands to ``services.work``.

Built on the shared Space modules: ``services.storage`` (the locked,
atomic file primitives and the state root), ``services.timestamps``
(``parse_ts``, ``now_iso``) and ``services.errors`` (``InboxError`` is a
``ServiceError``). Reads ``services.connections`` (the ``connections``
feeder, and ``service`` registers a new-events listener there); that
package never imports this one.

Core code: names no agent and imports nothing from the adapters tree.
"""
