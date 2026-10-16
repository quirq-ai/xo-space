"""Workspace-tier sinks — aggregate per-project ``.xo/`` files into
``<projects root>/.xo/`` workspace state.

Run at the end of each watcher tick, after per-project sinks have
flushed. The aggregation is deterministic — workspace files are
fully derived from per-project files plus, for ``workspace.json``,
the discovered project list. No per-tick state of its own; cheap to
recompute every tick (small JSON files).
"""
