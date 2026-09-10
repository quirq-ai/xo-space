"""Durable "someone is looking at this project" marks (issuesplan I2)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.github_mirror import MIRROR_SUBDIR
from services.cowork_agent.visualizer.reader import read_json

logger = logging.getLogger(__name__)

#: Beside ``issues.json`` in the same runtime subdirectory, for the reason the
#: mirror exports its own relative path: the tier decision stays in
#: ``project_layout`` and a reader joins the same path the writer did.
INTEREST_FILENAME = "interest.json"
INTEREST_RELATIVE = Path(MIRROR_SUBDIR) / INTEREST_FILENAME

#: On-disk revision. Bumped only if the shape changes; an unrecognised one
#: reads as "no mark" rather than raising, exactly like the mirror.
INTEREST_SCHEMA = 1


def interest_path(project: str, *, create: bool = False) -> Optional[Path]:
    """Where the mark lives for ``project``, or ``None`` to skip."""
    root = project_layout.runtime_dir_for_project(project, create=create)
    if root is None:
        return None
    target = root / INTEREST_RELATIVE
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def note_interest(project: str) -> bool:
    """Record that ``project`` is being looked at now. ``True`` iff written."""
    name = (project or "").strip()
    if not name:
        return False
    try:
        path = interest_path(name, create=True)
        if path is None:
            return False
        write_json_atomic(path, {
            "schema": INTEREST_SCHEMA,
            "last_viewed_at": time.time(),
        })
        return True
    except Exception:
        # Never a caller's problem: the read path this runs on is serving a
        # browse, and losing a background-refresh hint must not cost it.
        logger.debug("could not record interest for %s", name, exc_info=True)
        return False


def last_viewed_at(project: str) -> Optional[float]:
    """The mark's timestamp, or ``None`` when there is not a usable one."""
    try:
        path = interest_path(project)
        if path is None:
            return None
        doc = read_json(path)
        if not isinstance(doc, dict) or doc.get("schema") != INTEREST_SCHEMA:
            return None
        stamp = doc.get("last_viewed_at")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return None
        stamp = float(stamp)
        if stamp != stamp or stamp in (float("inf"), float("-inf")):
            return None
        return stamp
    except Exception:
        logger.debug("could not read interest for %s", project, exc_info=True)
        return None


def is_interesting(project: str, *, ttl: float, now: Optional[float] = None) -> bool:
    """Whether ``project`` was viewed within ``ttl`` seconds."""
    if ttl <= 0:
        return False
    stamp = last_viewed_at(project)
    if stamp is None:
        return False
    moment = time.time() if now is None else now
    return (moment - stamp) <= ttl


def clear_interest(project: str) -> bool:
    """Remove the mark. ``True`` iff a file was removed. For tests."""
    try:
        path = interest_path(project)
        if path is None or not path.exists():
            return False
        path.unlink()
        return True
    except Exception:
        logger.debug("could not clear interest for %s", project, exc_info=True)
        return False
