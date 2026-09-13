"""Router-facing facade for the inbox. Raises :class:`InboxError`; knows
nothing about HTTP. The only inbox module the BFF imports, so the route
handlers stay free of os/pathlib (BFF rule P2).

``refresh`` runs the enabled feeders (all their I/O happens outside the
file lock) and then applies the results in one locked read-modify-write.
It is throttled per process: every run that reaches the feeders stamps
``_last_refresh_monotonic``, whether or not a feeder (or the write) then
fails, so a broken feeder cannot turn each ``GET /api/inbox`` into a full
ingest; the next call within :data:`INGEST_MIN_INTERVAL_S` is skipped
unless forced.

Importing this module registers :func:`_ingest_after_poll` with
``services.connections.service``, so a "poll now" that collected something
ingests at once. The dependency points one way: the inbox knows about
connections; connections never imports the inbox.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from services.connections import service as connections_service

from . import feeders, store
from .store import ID_RE, InboxError  # re-exported: the router catches service.InboxError and checks ids

__all__ = ["InboxError", "ID_RE", "LIST_STATUSES", "refresh", "list_items", "create_item", "update_item",
           "update_many", "delete_item"]

logger = logging.getLogger(__name__)

INGEST_MIN_INTERVAL_S = 5.0
LIST_STATUSES = ("open", "done", "all")
UPDATE_MANY_MAX = 500   # ids per batch PATCH; also the file's item cap

_last_refresh_monotonic: Optional[float] = None


def _reset_throttle() -> None:
    """Tests: forget the last run so the next ``refresh()`` ingests."""
    global _last_refresh_monotonic
    _last_refresh_monotonic = None


def _cursor_advances(stored, candidate: str) -> bool:
    """Two refreshes can run at once (a forced one beside a throttled one,
    or two that passed the check before either stamped it), so a cursor
    only ever moves forward: the candidate replaces the stored value when
    it parses and is newer, or when the stored value is missing or
    unparsable (a hand edit). Never backwards, or a deleted item would be
    re-ingested."""
    new_dt = store.parse_ts(candidate)
    if new_dt is None:
        return False
    cur_dt = store.parse_ts(stored)
    return cur_dt is None or new_dt > cur_dt


def refresh(force: bool = False) -> bool:
    """Ingest from every enabled feeder. Returns whether the file changed.
    The throttle is stamped as soon as the feeders are reached: a run whose
    feeder or write fails still counts, or a persistent failure would be
    retried on every read."""
    global _last_refresh_monotonic
    if not force and _last_refresh_monotonic is not None \
            and time.monotonic() - _last_refresh_monotonic < INGEST_MIN_INTERVAL_S:
        return False
    snapshot, ok = store.load_document()
    if not ok:
        return False   # malformed file: load_document already warned; never overwrite it
    _last_refresh_monotonic = time.monotonic()
    results: dict[str, feeders.FeedResult] = {}
    for name in feeders.FEEDER_NAMES:
        if not store.source_config(snapshot, name)["enabled"]:
            continue
        try:
            results[name] = feeders.feeder(name)(snapshot)
        except Exception as exc:
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
    return changed


async def _ingest_after_poll(toolkit: str) -> None:
    """Forced ingest off the event loop, awaited by
    ``connections.service.poll_now`` once a poll collected something: the
    Inbox tab reloads right after that POST, and the read's own ingest is
    throttled, so without this the fresh events would wait for the next
    tick. ``refresh`` is looked up at call time, so a test patching it on
    this module is honoured."""
    await asyncio.to_thread(refresh, force=True)


# inbox -> connections, never the other way round (see the module docstring)
connections_service.register_new_events_listener(_ingest_after_poll)


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


def create_item(title, body="", kind="note", source="api", project_id=None, link=None, url=None) -> dict:
    """Validated (``invalid_value`` / ``invalid_project_id`` / ``invalid_link``,
    all 400), status ``new``, ts now, server-generated id. ``url`` is optional
    and must be an http(s) address when given (``invalid_value`` otherwise)."""
    item = store.build_item(title=title, body=body, kind=kind, source=source,
                            project_id=project_id, link=link, url=url)
    created: dict = {}

    def apply(doc: dict) -> bool:
        created.update(store.add_item(doc, item))
        return True

    store.modify(apply)
    return created


def update_item(item_id: str, status) -> dict:
    """A person's status. Clears ``auto_closed`` (see ``store.set_status``)."""
    if status not in store.STATUSES:
        raise InboxError("invalid_status", f"status must be one of {list(store.STATUSES)}.")
    if not isinstance(item_id, str) or not store.ID_RE.fullmatch(item_id):
        raise InboxError("item_not_found", "Inbox item not found.", 404)
    found: dict = {}

    def apply(doc: dict) -> bool:
        it = store.find_item(doc, item_id)
        if it is None:
            raise InboxError("item_not_found", "Inbox item not found.", 404)
        changed = store.set_status(it, status)
        found.update(it)
        return changed

    store.modify(apply)
    return found


def update_many(ids, status) -> dict:
    """Batch of :func:`update_item` in one locked read-modify-write:
    ``{"updated": n, "missing": [...]}`` where ``updated`` counts items whose
    status actually changed and ``missing`` lists, in request order, the ids
    that are malformed or not in the file. Idempotent: an id already at
    ``status`` is neither updated nor missing. ``ids`` must hold 1 to
    :data:`UPDATE_MANY_MAX` entries (``invalid_value`` otherwise); a bad
    ``status`` is ``invalid_status``. Clears ``auto_closed`` like a PATCH."""
    if status not in store.STATUSES:
        raise InboxError("invalid_status", f"status must be one of {list(store.STATUSES)}.")
    if not isinstance(ids, list) or not 1 <= len(ids) <= UPDATE_MANY_MAX:
        raise InboxError("invalid_value", f"ids must hold 1 to {UPDATE_MANY_MAX} item ids.")
    result: dict = {"updated": 0, "missing": []}

    def apply(doc: dict) -> bool:
        by_id = {it["id"]: it for it in doc["items"]}
        changed = False
        for item_id in ids:
            it = by_id.get(item_id) if isinstance(item_id, str) and store.ID_RE.fullmatch(item_id) else None
            if it is None:
                result["missing"].append(item_id)
                continue
            if it["status"] != status:
                result["updated"] += 1
            changed |= store.set_status(it, status)
        return changed

    store.modify(apply)
    return result


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
