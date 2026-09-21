"""Router-facing facade for the Inbox. Raises :class:`ServiceError`
subclasses (``InboxError`` here, ``WorkError`` from the work package);
knows nothing about HTTP. The only inbox module the BFF imports, so the
route handlers stay free of os/pathlib (BFF rule P2).

``refresh`` runs the enabled feeders (all their I/O happens outside any
lock) and ingests their facts as work items (:mod:`facts`), then advances
the cursors in the ledger. It is throttled per process: every run that
reaches the feeders stamps ``_last_refresh_monotonic``, whether or not a
feeder then fails, so a broken feeder cannot turn each ``GET /api/inbox``
into a full ingest; the next call within :data:`INGEST_MIN_INTERVAL_S` is
skipped unless forced.

Importing this module registers :func:`_ingest_after_poll` with
``services.connections.service``, so a "poll now" that collected something
ingests at once. The dependency points one way: the inbox knows about
connections; connections never imports the inbox.

The rows of the Inbox, the sections, the policies and the actions on an
item are the work package's (``services.work``: the join over work items,
claims and sessions, and the runner). They are imported at call time: the
runner imports this module for ``refresh``, and a module-level import here
would close a cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from services.connections import service as connections_service
from services.errors import ServiceError

from . import facts, feeders, ledger
from .ledger import InboxError  # re-exported: the router catches ServiceError, tests may want the type

__all__ = ["InboxError", "ServiceError", "refresh", "create_post", "list_rows", "sections", "set_policy",
           "item_detail", "reply", "start", "send", "archive", "reopen"]

logger = logging.getLogger(__name__)

INGEST_MIN_INTERVAL_S = 5.0

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
    unparsable (a hand edit). Never backwards, or a fact would be
    ingested twice (harmless, keyed) and the floor would slip."""
    new_dt = ledger.parse_ts(candidate)
    if new_dt is None:
        return False
    cur_dt = ledger.parse_ts(stored)
    return cur_dt is None or new_dt > cur_dt


def refresh(force: bool = False) -> int:
    """Ingest from every enabled feeder. Returns how many work items were
    created (0 when throttled). The throttle is stamped as soon as the
    feeders are reached: a run whose feeder fails still counts, or a
    persistent failure would be retried on every read."""
    global _last_refresh_monotonic
    if not force and _last_refresh_monotonic is not None \
            and time.monotonic() - _last_refresh_monotonic < INGEST_MIN_INTERVAL_S:
        return 0
    snapshot, ok = ledger.load_document()
    if not ok:
        return 0   # malformed ledger: load_document already warned; never overwrite it
    _last_refresh_monotonic = time.monotonic()
    results: dict[str, feeders.FeedResult] = {}
    for name in feeders.FEEDER_NAMES:
        if not ledger.source_config(snapshot, name)["enabled"]:
            continue
        try:
            results[name] = feeders.feeder(name)(snapshot)
        except Exception as exc:  # noqa: BLE001 - one feeder never stops the others
            logger.warning("inbox feeder %s failed: %s", name, exc)

    created = 0
    for name, res in results.items():
        for fact in res.items:
            try:
                _record, was_created, _project = facts.ingest(fact)
            except ServiceError as exc:
                logger.warning("inbox %s: a fact was not ingested: %s", name, exc.message)
                continue
            except Exception:  # noqa: BLE001 - a bad project file must not stop the run
                logger.warning("inbox %s: a fact was not ingested", name, exc_info=True)
                continue
            created += 1 if was_created else 0

    def advance(doc: dict) -> bool:
        changed = False
        for name, res in results.items():
            if res.cursor is not None and _cursor_advances(doc["cursors"].get(name), res.cursor):
                doc["cursors"][name] = res.cursor
                changed = True
        return changed

    if results:
        ledger.modify(advance)
    if created:
        logger.info("inbox: %d work item(s) created", created)
    return created


async def _ingest_after_poll(toolkit: str) -> None:
    """Forced ingest off the event loop, awaited by
    ``connections.service.poll_now`` once a poll collected something: the
    Inbox reloads right after that POST, and the read's own ingest is
    throttled, so without this the fresh events would wait for the next
    tick. ``refresh`` is looked up at call time, so a test patching it on
    this module is honoured."""
    await asyncio.to_thread(refresh, force=True)


# inbox -> connections, never the other way round (see the module docstring)
connections_service.register_new_events_listener(_ingest_after_poll)


# ── The rows, the sections, the policies ────────────────────────────────────


def _work():
    from services.work import service as work_service
    return work_service


def create_post(title, body="", kind="note", source="api", project_id=None, link=None, url=None) -> dict:
    """A note posted through the API (by an agent or a person) becomes a
    work item of the ``agents`` section in ``project_id``, else in the
    section's own project. Answers the item's Inbox row."""
    if not isinstance(source, str) or not facts.KIND_RE.fullmatch(source):
        raise InboxError("invalid_value", "source must match [a-z0-9_.:-] (1 to 60 chars).")
    fact = facts.build_fact(title=title, body=body, kind=kind, section="agents", entity=source, project_id=project_id,
                            link=link, url=url, source={"kind": "post", "post": {"agent": source, "kind": kind}})
    if project_id is not None and facts.target_project(fact) != project_id:
        raise InboxError("invalid_project_id", "project_id must name a project under the projects root.")
    record, _created, project = facts.ingest(fact)
    return _work().inbox_row(project, record["id"])


def list_rows(*, section: Optional[str] = None, entity: Optional[str] = None, state: str = "open", limit: int = 100) -> dict:
    """The Inbox: the sections with their counts, and the rows. A failing
    ingest never fails the read."""
    try:
        refresh()
    except Exception as exc:  # noqa: BLE001 - the page is served from what is on disk
        logger.warning("inbox refresh failed; serving what is on disk: %s", exc)
    return _work().inbox_rows(section=section, entity=entity, state=state, limit=limit)


def sections() -> dict:
    return _work().inbox_sections()


def set_policy(section: str, body) -> dict:
    return _work().set_section_policy(section, body)


def item_detail(project_id: str, workitem_id: str) -> dict:
    return _work().inbox_item(project_id, workitem_id)


async def reply(project_id: str, workitem_id: str, text: str) -> dict:
    return await _work().reply_inbox_item(project_id, workitem_id, text)


async def start(project_id: str, workitem_id: str, *, retry: bool = False) -> dict:
    return await _work().start_inbox_item(project_id, workitem_id, retry=retry)


async def send(project_id: str, workitem_id: str) -> dict:
    return await _work().send_inbox_item(project_id, workitem_id)


def archive(project_id: str, workitem_id: str, *, reason: Optional[str] = None) -> dict:
    return _work().archive_inbox_item(project_id, workitem_id, reason=reason)


def reopen(project_id: str, workitem_id: str) -> dict:
    return _work().reopen_inbox_item(project_id, workitem_id)
