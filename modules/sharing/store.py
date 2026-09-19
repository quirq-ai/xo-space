"""What the relay keeps on disk: ``~/.quirq/sharing/``.

* ``<repo>-<hash>.json``   one bookmark per repo identity (``state.py``):
                           ``last_reported`` (publish step), ``cursor``
                           (poll step) and ``cloned_at``. A fact: the swarm
                           and git hold the truth it points into.
* ``removed/<digest>.json`` a person removed their local copy of that repo
                           under that projects root (``state.py``); the
                           relay never clones it there again by itself.
* ``state.json``           the relay's last snapshot (``status.py``): parked
                           reason, cadence, last poll, one entry per repo.
                           Rewritten through :class:`Document` on every
                           ``record_*`` call, so a restart starts from it.
* ``events.jsonl``         the transitions (``status.py``): ``{ts, type:
                           "sharing.<kind>", repo, project, detail}``; the
                           stream follows it and ``recent`` reads it.

The placeholders in :data:`FILES` stand for one path component each
(``services/storage/files.py``): ``<bookmark>`` is ``<repo>-<hash>.json``
as :func:`state.state_path` names it, ``<marker>`` the sha256 digest
:func:`state.removed_path` names.
"""
from __future__ import annotations

from pathlib import Path

from services.storage.document import Document
from services.storage.eventlog import EventLog
from services.storage.files import File
from services.storage.layout import sharing_dir

#: On-disk revision of ``state.json``.
SCHEMA = 1
#: Every event line's ``type`` starts with this; the kind follows.
EVENT_PREFIX = "sharing."
_ROTATE_BYTES = 2 * 1024 * 1024      # tests patch this; never write 2 MB to exercise it
_MAX_ROTATIONS_KEEP = 3

#: Every file this module writes (services/storage/files.py): the layout
#: test, the fixture README and the "delete it and you lose" column derive
#: from this table.
FILES = [
    File("sharing/state.json", role="fact", schema="sharing-state",
         note="the relay's last snapshot: parked reason, last poll, each repo's status"),
    File("sharing/events.jsonl", role="record", log=True, rotate="2 MB, keep 3",
         note="what happened: shared with you, fetched, cloned, revoked, errors"),
    File("sharing/<bookmark>", role="fact", schema="sharing-bookmark",
         note="<repo>-<hash>.json: where the relay stopped reading and reporting, and when it cloned"),
    File("sharing/removed/<marker>", role="decision", schema="sharing-removed",
         note="you removed your local copy; it is never cloned here again by itself"),
]


# ── paths ────────────────────────────────────────────────────────────────


def state_path() -> Path:
    return sharing_dir() / "state.json"


def events_path() -> Path:
    return sharing_dir() / "events.jsonl"


# ── state.json ───────────────────────────────────────────────────────────


def repo_defaults() -> dict:
    """One repo's entry as the relay first sees it."""
    return {
        "project": None, "shared": False, "available": False,
        "members": None,  # active rows at the swarm, owner included; None = not reported
        "last_fetch_at": None, "fetched": 0,
        "pending_github": False, "last_error": None,
        "clone": None,  # None | {state, detail, at, attempts, had_token, next_retry_at}
    }


def empty() -> dict:
    return {
        "enabled": True,
        "workspace_configured": True,
        "reason": None,               # disabled | no_workspace_id | no_auth | None
        "cadence": "parked",          # parked | running
        "last_poll_at": None,
        "last_poll_ok": None,
        "repos": {},
    }


def normalize(doc: dict) -> dict:
    """Defaults for missing keys, a dict of dicts for ``repos`` (an entry
    that is not one reads as a fresh entry), unknown keys kept."""
    for key, value in empty().items():
        doc.setdefault(key, value)
    repos = doc.get("repos")
    if not isinstance(repos, dict):
        repos = {}
    clean: dict = {}
    for repo, entry in repos.items():
        if not isinstance(repo, str):
            continue
        merged = repo_defaults()
        if isinstance(entry, dict):
            merged.update(entry)
        fetched = merged.get("fetched")
        if isinstance(fetched, bool) or not isinstance(fetched, int) or fetched < 0:
            merged["fetched"] = 0
        if merged.get("clone") is not None and not isinstance(merged.get("clone"), dict):
            merged["clone"] = None
        clean[repo] = merged
    doc["repos"] = clean
    return doc


def state_document() -> Document:
    return Document(state_path(), schema=SCHEMA, empty=empty, normalize=normalize,
                    name="sharing/state.json")


# ── events.jsonl ─────────────────────────────────────────────────────────


def events_log() -> EventLog:
    return EventLog(events_path(), rotate_bytes=_ROTATE_BYTES, keep=_MAX_ROTATIONS_KEEP)
