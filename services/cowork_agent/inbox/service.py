"""Router-facing facade for the inbox. Raises :class:`InboxError`; knows
nothing about HTTP. The only inbox module the BFF imports, so the route
handlers stay free of os/pathlib (BFF rule P2).

``refresh`` runs the enabled feeders (all their I/O happens outside the
file lock) and then applies the results in one locked read-modify-write.
It is throttled per process: a run that reached the write step with every
enabled feeder succeeding stamps ``_last_refresh_monotonic``; the next
call within :data:`INGEST_MIN_INTERVAL_S` is skipped unless forced.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from . import feeders, store
from .store import InboxError  # re-exported: the router catches service.InboxError

__all__ = ["InboxError", "refresh", "list_items", "create_item", "update_item", "delete_item"]

logger = logging.getLogger(__name__)

INGEST_MIN_INTERVAL_S = 5.0
LIST_STATUSES = ("open", "done", "all")

_last_refresh_monotonic: Optional[float] = None


def _reset_throttle() -> None:
    """Tests: forget the last run so the next ``refresh()`` ingests."""
    global _last_refresh_monotonic
    _last_refresh_monotonic = None


def _cursor_advances(stored, candidate: str) -> bool:
    """Two refreshes can pass the throttle together (the stamp is set only at
    the end), so a cursor only ever moves forward: the candidate replaces the
    stored value when it parses and is newer, or when the stored value is
    missing or unparsable (a hand edit). Never backwards, or a deleted item
    would be re-ingested."""
    new_dt = store.parse_ts(candidate)
    if new_dt is None:
        return False
    cur_dt = store.parse_ts(stored)
    return cur_dt is None or new_dt > cur_dt


def refresh(force: bool = False) -> bool:
    """Ingest from every enabled feeder. Returns whether the file changed."""
    global _last_refresh_monotonic
    if not force and _last_refresh_monotonic is not None \
            and time.monotonic() - _last_refresh_monotonic < INGEST_MIN_INTERVAL_S:
        return False
    snapshot, ok = store.load_document()
    if not ok:
        return False   # malformed file: load_document already warned; never overwrite it
    results: dict[str, feeders.FeedResult] = {}
    failed = False
    for name in feeders.FEEDER_NAMES:
        if not store.source_config(snapshot, name)["enabled"]:
            continue
        try:
            results[name] = feeders.feeder(name)(snapshot)
        except Exception as exc:
            failed = True
            logger.warning("inbox feeder %s failed: %s", name, exc)

    changed = False

    def apply(doc: dict) -> bool:
        nonlocal changed
        for name, res in results.items():
            changed |= store.upsert_many(doc, res.items)
            if res.cursor is not None and _cursor_advances(doc["cursors"].get(name), res.cursor):
                doc["cursors"][name] = res.cursor
                changed = True
            if res.watched is not None:
                changed |= store.close_missing(doc, res.watched.key_prefix, res.watched.keys)
        return changed

    if results:
        store.modify(apply)
    if not failed:
        _last_refresh_monotonic = time.monotonic()
    return changed


def list_items(status: str = "open", limit: int = 200) -> dict:
    """Counts cover the whole file; ``items`` is the newest-first slice for
    ``status`` (``open`` = new plus seen). A failing ingest never fails the read."""
    if status not in LIST_STATUSES:
        raise InboxError("invalid_status", f"status must be one of {list(LIST_STATUSES)}.")
    try:
        refresh()
    except Exception as exc:
        logger.warning("inbox refresh failed; serving the file as is: %s", exc)
    doc, _ok = store.load_document()
    counts = {s: 0 for s in store.STATUSES}
    for it in doc["items"]:
        counts[it["status"]] += 1
    wanted = {"open": ("new", "seen"), "done": ("done",), "all": store.STATUSES}[status]
    items = [it for it in doc["items"] if it["status"] in wanted]
    return {"schema": store.SCHEMA, "updated_at": doc.get("updated_at"), "counts": counts,
            "items": items[:max(1, int(limit))]}


def create_item(title, body="", kind="note", source="api", project_id=None, link=None) -> dict:
    """Validated (``invalid_value`` / ``invalid_project_id`` / ``invalid_link``,
    all 400), status ``new``, ts now, server-generated id."""
    item = store.build_item(title=title, body=body, kind=kind, source=source,
                            project_id=project_id, link=link)
    created: dict = {}

    def apply(doc: dict) -> bool:
        created.update(store.add_item(doc, item))
        return True

    store.modify(apply)
    return created


def update_item(item_id: str, status) -> dict:
    if status not in store.STATUSES:
        raise InboxError("invalid_status", f"status must be one of {list(store.STATUSES)}.")
    if not isinstance(item_id, str) or not store.ID_RE.fullmatch(item_id):
        raise InboxError("item_not_found", "Inbox item not found.", 404)
    found: dict = {}

    def apply(doc: dict) -> bool:
        it = store.find_item(doc, item_id)
        if it is None:
            raise InboxError("item_not_found", "Inbox item not found.", 404)
        changed = it["status"] != status
        it["status"] = status
        found.update(it)
        return changed

    store.modify(apply)
    return found


def delete_item(item_id: str) -> bool:
    """Idempotent: ``False`` when the id is absent (or malformed)."""
    if not isinstance(item_id, str) or not store.ID_RE.fullmatch(item_id):
        return False
    removed = False

    def apply(doc: dict) -> bool:
        nonlocal removed
        kept = [it for it in doc["items"] if it["id"] != item_id]
        removed = len(kept) != len(doc["items"])
        doc["items"] = kept
        return removed

    store.modify(apply)
    return removed
