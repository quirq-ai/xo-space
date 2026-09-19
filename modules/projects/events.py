"""What the projects module emits and raises.

The record stores and the watcher's sinks write these lines to a project's
timeline (``modules.timeline.service.emit``): the todo transitions
(``todos_store``), the workitem lifecycle (``workitems_store``,
``workitem_claims``), the files the watcher sees touched, a project's
creation. ``session.*`` belongs to the sessions module. The timeline
schema's ``type`` enum still lists them; the registry's union keeps both in
step until the enum is trimmed.
"""

from __future__ import annotations

import logging

from services import signals

logger = logging.getLogger(__name__)

TYPES = (
    "project.created",
    "todo.added",
    "todo.completed",
    "todo.status_changed",
    "file.created",
    "file.edited",
    "plan.written",
    "episode.written",
    "peer.sync.started",
    "peer.sync.applied",
    "peer.sync.conflict",
    "workitem.created",
    "workitem.adopted",
    "workitem.assigned",
    "workitem.claimed",
    "workitem.released",
    "workitem.closed",
    "workitem.reopened",
    "workitem.deleted",
)

#: Signals raised through ``services.signals`` as ``projects.<name>``.
SIGNALS = ("workitem_changed",)

WORKITEM_CHANGED = "projects.workitem_changed"

#: The workitem actions that raise :data:`WORKITEM_CHANGED`.
WORKITEM_CHANGE_ACTIONS: frozenset[str] = frozenset({"created", "claimed", "released", "closed", "assigned"})


def workitem_changed(project_id: str, workitem_id: str, action: str) -> None:
    """Raise ``projects.workitem_changed`` for one of
    :data:`WORKITEM_CHANGE_ACTIONS`; any other action is ignored. Never
    raises: a listener's failure is the bus's to log, and a bus failure must
    not fail the write it follows."""
    if action not in WORKITEM_CHANGE_ACTIONS or not workitem_id:
        return
    try:
        signals.notify_soon(WORKITEM_CHANGED, project_id=project_id, workitem_id=workitem_id, action=action)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("projects: could not raise %s for %s/%s", WORKITEM_CHANGED, project_id, workitem_id, exc_info=True)
