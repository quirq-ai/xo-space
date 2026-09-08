"""Sinks — apply events to watcher-owned state files.

Each sink is a stateless module exposing one top-level function (or a
small class) that takes an explicit state path plus events/state and
rewrites exactly one owned file. State across ticks lives in the files
themselves; the watcher re-reads on each call. This makes the sinks
restart-safe and trivially testable.

Files owned by each sink, with the tier each one writes into (T19 split
the per-project outputs across two roots — ``<project>/.xo/`` for what a
clone would want, ``~/.quirq/`` for what this machine re-derives):

* :mod:`project_json`     → ``<project>/.xo/project.json``  (one-shot identity fill)
* :mod:`sessions_augment` → ``~/.quirq/projects/<pid>/sessions/sessions-augment.json``
* :mod:`stats`            → ``~/.quirq/projects/<pid>/stats.json``
* :mod:`timeline`         → ``~/.quirq/projects/<pid>/timeline.jsonl``  (append-only, rotated)
* :mod:`activity`         → ``~/.quirq/watcher/activity/projects/<id>.json``

Two files that are NOT written here, and it matters which is which:

* ``sessions/sessionslist.d/`` — the adapter-owned session index, one
  shard per session (docs/watcher-design.md §3.7).
* ``<project>/.xo/todos.json`` — there **is no todos sink** any more
  (syncplan §7, T8). It ingested task events that exactly one runtime
  emits, so todos worked on one backend out of five. The agent-facing
  ``POST/PATCH/DELETE /todos`` endpoints are the file's only writer now,
  and :mod:`services.cowork_agent.visualizer.todos_store` emits the
  ``TaskCreated`` / ``TaskStatusChanged`` events that :mod:`timeline`
  and :mod:`sessions_augment` still consume — from a request thread
  rather than from ingestion, so every backend produces them.
"""
