"""``~/.quirq/workspace/timeline.jsonl`` — multiplexed workspace
timeline.

Different from the other workspace sinks: timeline is **append-only**
at both tiers, so we don't union the files on every tick (that
would re-append duplicates). Instead the watcher's main loop emits
workspace events alongside per-project events on a per-tick basis,
appending the SAME events to the workspace file with an extra
``project_id`` field.

This module provides the append helper; the watcher loop calls it
directly with the per-tick event list and a project_id tag.

Runtime tier since T20. It is a multiplexed copy of per-project timelines
that T19 had already made machine-local, so leaving it in ``<XO root>/.xo/``
would have shipped this machine's telemetry to every clone and every restore.
Nothing migrates: the same open decision O3 took for the per-project file
applies here — append-only *with rotation* cannot be read through two roots
without reconciling the ``timeline.<stamp>.jsonl`` glob across both, so the
pre-move history is dropped rather than half-merged, and
``views.sweep_abandoned`` removes it.
"""

from __future__ import annotations

from typing import Iterable

from services.cowork_agent.project_layout import workspace_runtime_dir
from services.cowork_agent.visualizer.atomic_write import append_jsonl


_WORKSPACE_TIMELINE = "timeline.jsonl"


def apply(events: Iterable[dict], *, project_id: str) -> bool:
    """Append timeline-schema events tagged with ``project_id`` to
    the workspace timeline.

    ``events`` is a list of already-rendered timeline event dicts
    (the per-project ``timeline`` sink's output — same shape it
    wrote to its own ``timeline.jsonl``). We add ``project_id`` to
    each line, append, and return whether anything was written.

    No rotation at the workspace tier today — workspace traffic is
    smaller (events × projects, but with high overlap on quiet
    periods).
    """
    lines = []
    for ev in events:
        if isinstance(ev, dict):
            tagged = dict(ev)
            tagged["project_id"] = project_id
            lines.append(tagged)
    if not lines:
        return False
    append_jsonl(workspace_runtime_dir() / _WORKSPACE_TIMELINE, lines)
    return True
