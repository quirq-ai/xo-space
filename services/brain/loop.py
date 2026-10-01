"""The brain's background tick, on :func:`services.periodic.run_forever`.

Off unless ``BRAIN_ENABLED`` is true (the API works either way). Each tick,
every ``BRAIN_TICK_S`` seconds (default 600):

1. re-learns each enabled source whose folder changed since it was last
   learned (git HEAD and working-tree status, or file times outside git);
2. fades links nobody used (``BRAIN_FADE_PER_DAY``);
3. runs discovery when anything was learned, or at least once a day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from services.brain import config, files, service, store
from services.periodic import run_forever
from services.timestamps import now_iso, parse_ts

logger = logging.getLogger(__name__)

NAME = "brain"
STARTUP_DELAY_S = 60.0
DISCOVER_AT_LEAST_EVERY = timedelta(days=1)


def _changed_sources() -> list[str]:
    with store.connect() as conn:
        rows = conn.execute("SELECT id, location, signature, status FROM sources WHERE enabled = 1").fetchall()
    changed = []
    for row in rows:
        root = Path(row["location"])
        if not root.is_dir():
            continue
        if row["status"] != "ready" or files.signature(root) != row["signature"]:
            changed.append(row["id"])
    return changed


def _discovery_due() -> bool:
    with store.connect() as conn:
        last = conn.execute("SELECT MAX(started_at) FROM runs WHERE kind = 'discover' AND status = 'done'").fetchone()[0]
    stamp = parse_ts(last)
    now = parse_ts(now_iso())
    return stamp is None or now is None or now - stamp >= DISCOVER_AT_LEAST_EVERY


async def tick() -> dict:
    learned, failed = [], []
    for source_id in await asyncio.to_thread(_changed_sources):
        try:
            await service.learn_now(source_id)
            learned.append(source_id)
        except Exception as exc:  # noqa: BLE001 - one broken source never stops the others
            failed.append(source_id)
            logger.warning("brain: learning %s failed: %s", source_id, exc)
    faded = await asyncio.to_thread(service.fade_now)
    discovered = None
    if learned or await asyncio.to_thread(_discovery_due):
        try:
            discovered = await service.discover_now()
        except store.BrainError as exc:
            logger.info("brain: discovery skipped: %s", exc.message)
    return {"learned": learned, "failed": failed, "faded": faded, "discovered": discovered}


async def start_brain_loop() -> None:
    await run_forever(NAME, tick, interval_s=config.tick_seconds, startup_delay_s=STARTUP_DELAY_S,
                      enabled=config.loop_enabled, logger=logger)
