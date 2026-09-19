"""``~/.quirq/projects/timeline.jsonl``: the Space log, owned by
``modules/timeline`` since the logs were unified.

Kept for importers only. Nothing in the tree calls :func:`apply` any more:
the per-project sink hands its lines to ``modules.timeline.service.emit``,
which writes each line once (a line with a pid to its project's log, the
rest to the Space log) and the Space view is merged at read time. Call
``emit`` directly from new code.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from modules.timeline import service as timeline_service

logger = logging.getLogger(__name__)


def apply(events: Iterable[dict], *, project_id: Optional[str] = None) -> bool:
    """A thin call to ``modules.timeline.service.emit``: rendered lines,
    stamped with ``project_id`` when given, written once each (the Space
    log takes the ones without a pid). Returns whether anything was written."""
    logger.warning("workspace/timeline.apply is kept for importers; "
                   "call modules.timeline.service.emit instead")
    return bool(timeline_service.emit(events, project_id=project_id))
