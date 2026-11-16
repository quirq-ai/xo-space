"""``/api/advisory``: the space advisory's snapshot.

  GET /api/advisory                    every fact now: each account (its agent
                                       and account label) with its usage
                                       windows and paid facts, the machine,
                                       the live sessions; each fact with its
                                       value, source, observed_at, age_s and
                                       known (false when stale or never seen)
  GET /api/advisory?after=<version>    the first snapshot newer than
                                       ``version``, waiting up to ``wait``
                                       seconds (default 30, at most 60), else
                                       the current one

Facts only: it never says go or wait. The service owns the facts; this
router only shapes the wire.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Query

from services import advisory

router = APIRouter(prefix="/api/advisory", tags=["advisory"])

WAIT_DEFAULT_S = 30.0
WAIT_MAX_S = 60.0


@router.get("")
async def snapshot(after: Optional[int] = Query(None, ge=0),
                   wait: float = Query(WAIT_DEFAULT_S, ge=0, le=WAIT_MAX_S)) -> dict:
    found = advisory.current()
    current = await found.wait_newer(after, wait) if after is not None else found.snapshot()
    return current.as_dict(time.time())
