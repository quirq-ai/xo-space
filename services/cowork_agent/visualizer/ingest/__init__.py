"""Ingest layer — reads raw runtime logs, produces normalised events.

Three stateless modules:

* :mod:`events` — frozen dataclasses for every event type a sink
  needs. The shape is sink-oriented, not jsonl-shape-oriented.
* :mod:`pii_filter` — the redactor. The only module that turns a raw
  jsonl line into an :class:`events.Event`. Drops every field that
  isn't on the allowlist in docs/watcher-design.md §5.2.
* :mod:`jsonl_tail` — seek-tail with offset persistence. Per-file
  state; survives restarts via ``~/.quirq/projects/offsets.json``.

Stateful coordination (e.g. pairing a ``TaskCreate`` tool_use with
its tool_result to recover the assigned task id) lives in the
**source** layer (``services/cowork_agent/visualizer/sources/``),
not here.
"""
