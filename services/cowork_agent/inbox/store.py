"""The inbox file: ``<XO root>/.xo/inbox.json``.

Every write goes through :func:`modify` (``flock.locked`` around one
read-modify-write, ``write_json_atomic`` for the swap), so the API and
the feeders never clobber each other. A person may hand-edit the file, so
:func:`normalize_document` runs on every read: missing keys get defaults,
unknown keys (top level and per item) survive, items without a valid id or
title are dropped with a WARN, a bad status becomes ``new`` and an
unparsable ``ts`` becomes now (logged).

Timestamps are stored as the producer wrote them (``Z``, ``+00:00`` or
naive) and are only ever compared through :func:`parse_ts`.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from services.cowork_agent.project_layout import workspace_xo_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json

logger = logging.getLogger(__name__)

SCHEMA = 1
MAX_ITEMS = 500          # after a write: drop oldest done first, then oldest of the rest
DONE_TTL_DAYS = 30       # done items older than this are pruned on write
STATUSES = ("new", "seen", "done")
TITLE_MAX, BODY_MAX, PATH_MAX = 300, 4000, 500
DEFAULT_SOURCES: dict = {
    "timeline": {"enabled": True, "types": ["session.started", "todo.added"]},
    "todos": {"enabled": True, "statuses": ["blocked"]},
    "sharing": {"enabled": True},
}

ID_RE = re.compile(r"[0-9a-f]{8}")
SOURCE_RE = re.compile(r"[a-z0-9_:-]{1,40}")
KIND_RE = re.compile(r"[a-z0-9_.:-]{1,60}")
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9_:\-\.]{1,200}")
VIEW_RE = re.compile(r"[a-z0-9_-]{1,40}")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class InboxError(Exception):
    """Typed failure the router maps to ``HTTPException(status, {code, message})``."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ── Time and validation helpers ─────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value) -> Optional[datetime]:
    """ISO-8601 string (``Z``, offset or naive, treated as UTC) to an aware
    UTC datetime; ``None`` on anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def is_project_id(value) -> bool:
    return isinstance(value, str) and PROJECT_ID_RE.fullmatch(value) is not None


def is_link_path(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= PATH_MAX and not value.startswith("/")
            and "\\" not in value and "\x00" not in value
            and ".." not in value.split("/"))


def validate_link(link, *, strict: bool = False) -> Optional[dict]:
    """Keep only ``view`` / ``project`` / ``path`` that pass their rule; an
    empty result is ``None``. ``strict`` raises ``invalid_link`` instead of
    dropping a bad value (API input); lenient mode is for hand-edited files."""
    if link is None:
        return None
    if not isinstance(link, dict):
        if strict:
            raise InboxError("invalid_link", "link must be an object.")
        return None
    rules = (("view", lambda v: isinstance(v, str) and VIEW_RE.fullmatch(v) is not None),
             ("project", is_project_id), ("path", is_link_path))
    out = {}
    for name, ok in rules:
        if name not in link:
            continue
        if ok(link[name]):
            out[name] = link[name]
        elif strict:
            raise InboxError("invalid_link", f"link.{name} is not valid.")
    return out or None


def build_item(*, title, body="", kind="note", source="api", project_id=None, link=None,
               ts=None, key=None) -> dict:
    """Validate and shape an item (no id yet; :func:`upsert_many` and
    :func:`add_item` allocate ids inside the lock). Raises ``InboxError``."""
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        raise InboxError("invalid_value", f"title is required (1 to {TITLE_MAX} chars).")
    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise InboxError("invalid_value", f"body must be a string of at most {BODY_MAX} chars.")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        raise InboxError("invalid_value", "kind must match [a-z0-9_.:-] (1 to 60 chars).")
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        raise InboxError("invalid_value", "source must match [a-z0-9_:-] (1 to 40 chars).")
    if project_id is not None and not is_project_id(project_id):
        raise InboxError("invalid_project_id", "project_id must be a project folder name.")
    return {"ts": ts or now_iso(), "source": source, "kind": kind, "title": title.strip(),
            "body": body, "project_id": project_id, "link": validate_link(link, strict=True),
            "status": "new", "key": key if isinstance(key, str) else None}


# ── Normalisation, ordering, retention ──────────────────────────────────────


def _normalize_item(raw, now_text: str) -> Optional[dict]:
    if not isinstance(raw, dict):
        logger.warning("inbox: dropping non-object item")
        return None
    it = dict(raw)
    if not isinstance(it.get("id"), str) or not ID_RE.fullmatch(it["id"]):
        logger.warning("inbox: dropping item with invalid id %r", it.get("id"))
        return None
    if not isinstance(it.get("title"), str) or not it["title"].strip():
        logger.warning("inbox: dropping item %s without a title", it["id"])
        return None
    it["title"] = it["title"].strip()[:TITLE_MAX]
    it["body"] = it["body"][:BODY_MAX] if isinstance(it.get("body"), str) else ""
    if it.get("status") not in STATUSES:
        it["status"] = "new"
    if parse_ts(it.get("ts")) is None:
        logger.warning("inbox: item %s has an unparsable ts %r; set to now", it["id"], it.get("ts"))
        it["ts"] = now_text
    if not isinstance(it.get("source"), str) or not SOURCE_RE.fullmatch(it["source"]):
        it["source"] = "api"
    if not isinstance(it.get("kind"), str) or not KIND_RE.fullmatch(it["kind"]):
        it["kind"] = "note"
    it["project_id"] = it.get("project_id") if is_project_id(it.get("project_id")) else None
    it["link"] = validate_link(it.get("link"))
    it["key"] = it.get("key") if isinstance(it.get("key"), str) else None
    return it


def source_config(doc: dict, name: str) -> dict:
    """Defaults merged with whatever the file says for one source."""
    cfg = dict(DEFAULT_SOURCES.get(name, {"enabled": True}))
    sources = doc.get("sources")
    if isinstance(sources, dict) and isinstance(sources.get(name), dict):
        cfg.update(sources[name])
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def sort_newest_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda it: parse_ts(it.get("ts")) or _EPOCH, reverse=True)


def normalize_document(raw, *, now: Optional[datetime] = None) -> dict:
    """Any parsed JSON (or ``None``) to a well-formed document. Unknown
    keys are preserved; see the module docstring for the item rules."""
    now_text = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = dict(raw) if isinstance(raw, dict) else {}
    doc["schema"] = SCHEMA
    doc.setdefault("updated_at", None)
    if not isinstance(doc.get("sources"), dict):
        doc["sources"] = {k: dict(v) for k, v in DEFAULT_SOURCES.items()}
    if not isinstance(doc.get("cursors"), dict):
        doc["cursors"] = {}
    items, seen = [], set()
    for raw_item in doc.get("items") if isinstance(doc.get("items"), list) else []:
        it = _normalize_item(raw_item, now_text)
        if it is None:
            continue
        if it["id"] in seen:
            logger.warning("inbox: dropping duplicate item id %s", it["id"])
            continue
        seen.add(it["id"])
        items.append(it)
    doc["items"] = sort_newest_first(items)
    return doc


def apply_retention(items: list[dict], now: Optional[datetime] = None) -> tuple[list[dict], int]:
    """TTL first, then the cap (done first, oldest first). Pure."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DONE_TTL_DAYS)
    kept = [it for it in items
            if not (it.get("status") == "done" and (parse_ts(it.get("ts")) or _EPOCH) < cutoff)]
    pruned = len(items) - len(kept)
    if len(kept) > MAX_ITEMS:
        order = sorted(kept, key=lambda it: (it.get("status") != "done", parse_ts(it.get("ts")) or _EPOCH))
        drop = {id(it) for it in order[:len(kept) - MAX_ITEMS]}
        kept = [it for it in kept if id(it) not in drop]
        pruned += len(drop)
    return kept, pruned


# ── File access ──────────────────────────────────────────────────────────────


def inbox_path() -> Path:
    return workspace_xo_dir() / "inbox.json"


def load_document(path: Optional[Path] = None) -> tuple[dict, bool]:
    """``(document, ok)``. ``ok`` is False when the file exists with content
    that is not JSON; callers must not overwrite it in that case."""
    path = path or inbox_path()
    raw = read_json(path)
    ok = True
    if raw is None and path.is_file():
        try:
            ok = not path.read_text(encoding="utf-8").strip()
        except OSError:
            ok = False
        if not ok:
            logger.warning("inbox: %s is not valid JSON; leaving it untouched", path)
    return normalize_document(raw), ok


def modify(fn: Callable[[dict], bool], *, now: Optional[datetime] = None) -> dict:
    """Locked read-modify-write. ``fn`` edits the normalised document in
    place and returns whether anything changed; the file is written (after
    retention and re-sorting) only then, so a read never creates it."""
    path = inbox_path()
    with locked(path):
        doc, ok = load_document(path)
        if not ok:
            # path-free on purpose: load_document already logged the full path,
            # and the router forwards this message to the browser
            raise InboxError("scope_unavailable", "inbox.json is not valid JSON; fix or remove it.", 500)
        if fn(doc):
            doc["items"], pruned = apply_retention(doc["items"], now)
            if pruned:
                logger.info("inbox: pruned %d item(s) by retention", pruned)
            doc["items"] = sort_newest_first(doc["items"])
            doc["updated_at"] = now_iso()
            write_json_atomic(path, doc)
        return doc


# ── In-document edits (call inside ``modify``) ───────────────────────────────


def _new_id(existing: set[str]) -> str:
    for _ in range(20):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
    raise InboxError("scope_unavailable", "could not allocate a unique inbox id; retry.", 500)


def add_item(doc: dict, item: dict) -> dict:
    ids = {it["id"] for it in doc["items"]}
    new = {"id": _new_id(ids), **item}
    doc["items"].append(new)
    return new


def upsert_many(doc: dict, items: list[dict]) -> bool:
    """Keyed, status-preserving ingest: an existing key gets title/body/link
    refreshed in place; a new key is appended with a fresh id. Items without
    a key are always appended. Returns whether the document changed."""
    ids = {it["id"] for it in doc["items"]}
    by_key = {it["key"]: it for it in doc["items"] if it.get("key")}
    changed = False
    for item in items:
        cur = by_key.get(item.get("key")) if item.get("key") else None
        if cur is None:
            new = {"id": _new_id(ids), **item}
            ids.add(new["id"])
            doc["items"].append(new)
            if new.get("key"):
                by_key[new["key"]] = new
            changed = True
            continue
        for field in ("title", "body", "link"):
            if cur.get(field) != item.get(field):
                cur[field] = item.get(field)
                changed = True
    return changed


def close_missing(doc: dict, key_prefix: str, watched: frozenset[str]) -> bool:
    """Mark open items whose key starts with ``key_prefix`` but is no longer
    watched as done. Selection is by key, never by source: a keyless item
    (API-created, whatever ``source`` it declares) is never touched."""
    if not isinstance(key_prefix, str) or not key_prefix:
        raise ValueError("close_missing needs a non-empty key prefix")
    changed = False
    for it in doc["items"]:
        key = it.get("key")
        if isinstance(key, str) and key.startswith(key_prefix) and it["status"] != "done" and key not in watched:
            it["status"] = "done"
            changed = True
    return changed


def find_item(doc: dict, item_id: str) -> Optional[dict]:
    return next((it for it in doc["items"] if it["id"] == item_id), None)
