"""Durable "someone is looking at this project" marks (issuesplan I2).

The poller only refreshes a project that has a reason to be refreshed (D9):
adopted workitems, a live agent session, or *interest* — someone is looking
at it. The first two are read from disk on every tick and therefore survive a
restart. Interest was not: it lived in a process-local dict in
:mod:`~services.cowork_agent.github_poller`, so every restart forgot it.

That gap is not cosmetic, because of how the other two reasons behave on a
**fresh** Space:

* ``.xo/workitems.json`` is synced, so a *restored* project arrives with its
  adopted items and enrols itself on the first tick. A fresh one has no
  workitems file at all — the template does not ship one — so it has no
  durable reason, ever.
* A live session is transient by definition.

So on a fresh workspace the only enrolment signal was a mark that died with
the process, and the one screen that sets it (``GET …/github/issues``) is the
screen you cannot populate until something has been polled. Measured before
this module existed: a poller ticking for 18 hours across four projects,
polling nothing, while two mirrors sat untouched — behaving exactly as
designed and looking completely broken.

**Tier.** ``~/.quirq/projects/<pid>/github/interest.json`` — the runtime tier
(rule R-TIER), machine-local and disposable. Never ``.xo/``: "this machine's
user was recently looking at this project" is the definition of a
machine-scoped fact, and syncing it would enrol every Space a project is
restored into.

**Deliberately its own file, beside the mirror rather than inside it.**
``issues.json`` has exactly one writer — the poller — and
``github_mirror``'s merge rules are written on that assumption. This mark is
written by a *request thread*, so giving it its own document keeps that
invariant intact and means a corrupt or truncated mark can never cost the
mirror a row.

**Expiry is on read, not on write.** The file holds an absolute timestamp and
:func:`is_interesting` compares it to the TTL at the moment it is asked. So
lowering ``XO_GITHUB_POLL_INTEREST_TTL_S`` takes effect immediately for marks
already on disk, rather than only for marks written after the change.

Every function here is total: a mark that cannot be written, read or parsed
degrades to "not interested", which costs the project a background refresh
and nothing else. Interest is an optimisation hint, never correctness.
"""

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

#: Beside ``issues.json`` in the same runtime subdirectory, for the reason
#: the mirror exports its own relative path: the tier decision stays in
#: ``project_layout`` and a reader joins the same path the writer did.
INTEREST_FILENAME = "interest.json"
INTEREST_RELATIVE = Path(MIRROR_SUBDIR) / INTEREST_FILENAME

#: On-disk revision. Bumped only if the shape changes; an unrecognised one
#: reads as "no mark" rather than raising, exactly like the mirror.
INTEREST_SCHEMA = 1


def interest_path(project: str, *, create: bool = False) -> Optional[Path]:
    """Where the mark lives for ``project``, or ``None`` to skip.

    ``None`` means there is nothing to resolve — no project folder, or a pid
    that is not usable as a path segment. Callers treat it as "no mark" on a
    read and as "nothing to write" on a write, never as an error.
    """
    root = project_layout.runtime_dir_for_project(project, create=create)
    if root is None:
        return None
    target = root / INTEREST_RELATIVE
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def note_interest(project: str) -> bool:
    """Record that ``project`` is being looked at now. ``True`` iff written.

    Called from a request thread, so it must be cheap and must never raise:
    one small atomic write, and any failure is logged at debug and swallowed.
    A project that cannot record its interest still serves the request it was
    serving; it just will not be refreshed in the background.
    """
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
    """The mark's timestamp, or ``None`` when there is not a usable one.

    Absent, unreadable, malformed, a schema this revision does not write, or
    a timestamp that is not a finite number all read as ``None`` — the same
    "a cache whose loss costs one poll" degradation the mirror takes.
    """
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
    """Whether ``project`` was viewed within ``ttl`` seconds.

    A ``ttl`` of zero or less switches the signal off entirely, which is what
    ``XO_GITHUB_POLL_INTEREST_TTL_S=0`` is for.

    A mark timestamped in the *future* — a clock that moved backwards, or a
    file restored from elsewhere — is honoured rather than discarded. It
    expires on its own once the clock passes it, and the cost of being wrong
    is one polled repository, whereas discarding it would silently un-enrol a
    project on a machine whose clock is merely skewed.
    """
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
