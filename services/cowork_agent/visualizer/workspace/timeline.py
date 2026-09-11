"""``~/.quirq/workspace/timeline.jsonl`` — multiplexed workspace timeline."""

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
