"""The Work's own files, one folder per page under ``~/.quirq/work/``
(under ``QUIRQ_STATE_ROOT``)::

    ~/.quirq/work/
    ├── inbox/inbox.json        the decisions a person made: dismissed, acked, promoted;
    │                           which connection kinds are decisions
    ├── live/live.json          the Live page's choices: which stream groups show
    └── history/history.json    the feed: which readers run, the watermark, pins,
                                and the posts agents send through POST /api/feed

The Work reads the Space's logs at read time (``readers.py``) and derives
what needs a person from current state (``attention.py``); nothing from a
log is ever copied here. These files hold only the person's decisions,
each keyed by the fact it was made about (design section 16.3):

- ``dismissed``: ``{"<key>@<since>": <when>}``. The pair is what is hidden,
  so the same condition starting again (a new ``since``) comes back.
- ``acked``: ``{"<key>": <when>}``. A Completed row acknowledged, gone for good.
- ``promoted``: ``{"<key>": {"project_id", "workitem_id", "at"}}``. The join
  from an entry to the work item it became. Idempotent by key.
- ``pinned``: ``["<key>", ...]``.
- ``posts``: what agents ``POST /api/feed``. The one list of records the
  Work owns; 500 items or 30 days.
- ``watermark``: seen up to and including this ``ts``.
- ``sources``: which readers run; ``attention`` (inbox) lists the connection
  kinds that are decisions; ``stream`` (live) the groups the stream shows.

In memory the three files are one document (:func:`load_document` merges
them, :func:`modify` splits them back), so every reader sees one shape.
Same rules as the Inbox file this replaces: normalised on every read,
unknown keys survive a rewrite (per file), hand-editable, one locked
read-modify-write per change. A read never creates a file. The single
``work/work.json`` of the first cut is adopted into the three files once,
under the lock, on first use.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from services.cowork_agent.project_layout import load_project
from services.errors import ServiceError
from services.storage.atomic_write import write_json_atomic
from services.storage.flock import locked
from services.storage.layout import work_dir
from services.storage.reader import read_json
from services.timestamps import now_iso, parse_ts

logger = logging.getLogger(__name__)

SCHEMA = 3
PAGES = ("inbox", "live", "history")
POSTS_MAX = 500            # after a write: oldest posts dropped past this
POSTS_TTL_DAYS = 30        # posts older than this are pruned on write
MARKS_TTL_DAYS = 30        # dismissals and acknowledgements older than this are pruned on write
TITLE_MAX, BODY_MAX, PATH_MAX, URL_MAX = 300, 4000, 500, 2000
KEY_MAX = 400
DEFAULT_SOURCES: dict = {
    "timeline": {"enabled": True},
    "issues": {"enabled": True},
    "connections": {"enabled": True, "attention": []},
    "sharing": {"enabled": True},
    "jobs": {"enabled": True},
    "posts": {"enabled": True},
}
SOURCE_NAMES = tuple(DEFAULT_SOURCES)
DEFAULT_STREAM: dict = {"agents": {"enabled": True}, "jobs": {"enabled": True}, "pollers": {"enabled": True},
                        "watcher": {"enabled": True}}
#: What each file owns. Everything else in a file is a hand edit that survives.
INBOX_KEYS = ("attention", "dismissed", "acked", "promoted")
LIVE_KEYS = ("stream",)
HISTORY_KEYS = ("sources", "watermark", "pinned", "posts")

ID_RE = re.compile(r"[0-9a-f]{8}")
SOURCE_RE = re.compile(r"[a-z0-9_:-]{1,40}")
KIND_RE = re.compile(r"[a-z0-9_.:-]{1,60}")
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9_:\-\.]{1,200}")
PID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
VIEW_RE = re.compile(r"[a-z0-9_-]{1,40}")
# A key names a fact: project ids, repos (owner/name), toolkit ids, message
# ids, issue numbers and timestamps all appear in one. Anything printable
# without whitespace, bounded.
KEY_RE = re.compile(r"[^\s\x00-\x1f\x7f]{1,400}")
ASSIGNEE_RE = re.compile(r"[A-Za-z0-9_:\-\.@]{1,200}")


class WorkError(ServiceError):
    """Typed failure the router maps to ``HTTPException(status, {code, message})``."""


# ── Validation helpers ──────────────────────────────────────────────────────


def is_project_id(value) -> bool:
    return isinstance(value, str) and PROJECT_ID_RE.fullmatch(value) is not None


def is_pid(value) -> bool:
    return isinstance(value, str) and PID_RE.fullmatch(value) is not None


def is_key(value) -> bool:
    return isinstance(value, str) and KEY_RE.fullmatch(value) is not None


def is_url(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= URL_MAX
            and (value.startswith("http://") or value.startswith("https://")))


def is_link_path(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= PATH_MAX and not value.startswith("/")
            and "\\" not in value and "\x00" not in value
            and ".." not in value.split("/"))


def pid_for(project_id) -> Optional[str]:
    """The pid in ``<projects root>/<project_id>/.xo/project.json``, or ``None``."""
    if not is_project_id(project_id):
        return None
    try:
        meta = load_project(project_id)
    except Exception:  # noqa: BLE001 - a missing or odd folder just has no pid
        return None
    pid = meta.get("pid") if isinstance(meta, dict) else None
    return pid if is_pid(pid) else None


def check_key(value) -> str:
    if not is_key(value):
        raise WorkError("invalid_key", f"key must be printable without whitespace (1 to {KEY_MAX} chars).")
    return value


def check_since(value) -> str:
    """A ``since`` is the producer's own timestamp, kept verbatim; an empty
    string is allowed (a condition with no known start)."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 64 or any(ch.isspace() for ch in value):
        raise WorkError("invalid_value", "since must be a timestamp string without whitespace (at most 64 chars).")
    return value


def check_ts(value) -> str:
    if parse_ts(value) is None:
        raise WorkError("invalid_value", "ts must be an ISO-8601 timestamp.")
    return value


def validate_link(link, *, strict: bool = False) -> Optional[dict]:
    """Keep only ``view`` / ``project`` / ``path`` that pass their rule."""
    if link is None:
        return None
    if not isinstance(link, dict):
        if strict:
            raise WorkError("invalid_link", "link must be an object.")
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
            raise WorkError("invalid_link", f"link.{name} is not valid.")
    return out or None


def build_post(*, title, body="", kind="note", source="api", project_id=None, link=None,
               url=None, ref=None, ts=None) -> dict:
    """Validate and shape a post (no id yet; :func:`add_post` allocates one
    inside the lock). Raises :class:`WorkError`."""
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        raise WorkError("invalid_value", f"title is required (1 to {TITLE_MAX} chars).")
    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise WorkError("invalid_value", f"body must be a string of at most {BODY_MAX} chars.")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        raise WorkError("invalid_value", "kind must match [a-z0-9_.:-] (1 to 60 chars).")
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        raise WorkError("invalid_value", "source must match [a-z0-9_:-] (1 to 40 chars).")
    if project_id is not None and not is_project_id(project_id):
        raise WorkError("invalid_project_id", "project_id must be a project folder name.")
    if url is not None and not is_url(url):
        raise WorkError("invalid_value", f"url must start with http:// or https:// and be at most {URL_MAX} chars.")
    if ref is not None and not isinstance(ref, dict):
        raise WorkError("invalid_value", "ref must be an object.")
    return {"ts": ts or now_iso(), "source": source, "kind": kind, "title": title.strip(),
            "body": body, "project_id": project_id,
            "pid": pid_for(project_id) if project_id is not None else None,
            "link": validate_link(link, strict=True), "url": url,
            "ref": _clean_ref(ref)}


def _clean_ref(ref) -> Optional[dict]:
    """The keys of a post's ``ref`` the pages understand: a work item, a
    todo, an issue, a session. Strings only, so nothing odd reaches a page."""
    if not isinstance(ref, dict):
        return None
    out = {}
    for name in ("workitem_id", "todo_id", "session_id"):
        value = ref.get(name)
        if isinstance(value, str) and value and len(value) <= 200:
            out[name] = value
    issue = ref.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("repo"), str) and isinstance(issue.get("number"), int) \
            and not isinstance(issue.get("number"), bool):
        out["issue"] = {"repo": issue["repo"], "number": issue["number"]}
    return out or None


# ── Normalisation and retention ─────────────────────────────────────────────


def _normalize_post(raw, now_text: str) -> Optional[dict]:
    if not isinstance(raw, dict):
        logger.warning("work: dropping non-object post")
        return None
    it = dict(raw)
    if not isinstance(it.get("id"), str) or not ID_RE.fullmatch(it["id"]):
        logger.warning("work: dropping post with invalid id %r", it.get("id"))
        return None
    if not isinstance(it.get("title"), str) or not it["title"].strip():
        logger.warning("work: dropping post %s without a title", it["id"])
        return None
    if parse_ts(it.get("ts")) is None:
        logger.warning("work: post %s has an unparsable ts; using now", it["id"])
        it["ts"] = now_text
    it.setdefault("source", "api")
    if not isinstance(it["source"], str) or not SOURCE_RE.fullmatch(it["source"]):
        it["source"] = "api"
    it.setdefault("kind", "note")
    if not isinstance(it["kind"], str) or not KIND_RE.fullmatch(it["kind"]):
        it["kind"] = "note"
    it["body"] = it.get("body") if isinstance(it.get("body"), str) else ""
    it["project_id"] = it.get("project_id") if is_project_id(it.get("project_id")) else None
    it["pid"] = it.get("pid") if is_pid(it.get("pid")) else None
    it["link"] = validate_link(it.get("link"))
    it["url"] = it.get("url") if is_url(it.get("url")) else None
    it["ref"] = _clean_ref(it.get("ref"))
    return it


def _normalize_sources(raw) -> dict:
    out = {name: dict(defaults) for name, defaults in DEFAULT_SOURCES.items()}
    if isinstance(raw, dict):
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            merged = dict(out.get(name, {}))
            merged.update(cfg)
            if "enabled" in merged and not isinstance(merged["enabled"], bool):
                merged["enabled"] = True
            if name == "connections":
                kinds = merged.get("attention")
                merged["attention"] = [k for k in kinds if isinstance(k, str) and k] if isinstance(kinds, list) else []
            out[name] = merged
    return out


def _normalize_marks(raw, *, check_value: Callable[[object], bool]) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if is_key(k) and check_value(v)}


def _normalize_stream(raw) -> dict:
    out = {name: dict(defaults) for name, defaults in DEFAULT_STREAM.items()}
    if isinstance(raw, dict):
        for name, cfg in raw.items():
            if isinstance(cfg, dict):
                merged = dict(out.get(name, {}))
                merged.update(cfg)
                if not isinstance(merged.get("enabled"), bool):
                    merged["enabled"] = True
                out[name] = merged
    return out


def normalize_document(raw, *, now: Optional[datetime] = None) -> dict:
    """The merged document with every key the Work uses, defaults filled,
    unknown keys kept (per file, under ``_extra``). Never raises on a hand
    edit."""
    now_text = now_iso() if now is None else now.strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = dict(raw) if isinstance(raw, dict) else {}
    doc["schema"] = SCHEMA
    doc["stream"] = _normalize_stream(doc.get("stream"))
    extra = doc.get("_extra")
    doc["_extra"] = {page: dict(extra[page]) for page in PAGES if isinstance(extra, dict) and isinstance(extra.get(page), dict)}
    if not isinstance(doc.get("updated_at"), str):
        doc["updated_at"] = None
    doc["watermark"] = doc.get("watermark") if parse_ts(doc.get("watermark")) else None
    doc["sources"] = _normalize_sources(doc.get("sources"))
    doc["dismissed"] = _normalize_marks(doc.get("dismissed"), check_value=lambda v: parse_ts(v) is not None)
    doc["acked"] = _normalize_marks(doc.get("acked"), check_value=lambda v: parse_ts(v) is not None)
    doc["promoted"] = _normalize_marks(
        doc.get("promoted"),
        check_value=lambda v: isinstance(v, dict) and is_project_id(v.get("project_id"))
        and isinstance(v.get("workitem_id"), str) and bool(v.get("workitem_id")))
    pinned = doc.get("pinned")
    doc["pinned"] = list(dict.fromkeys(k for k in pinned if is_key(k))) if isinstance(pinned, list) else []
    posts = doc.get("posts")
    doc["posts"] = [p for p in (_normalize_post(x, now_text) for x in (posts if isinstance(posts, list) else []))
                    if p is not None]
    return doc


def sort_newest_first(posts: list[dict]) -> list[dict]:
    return sorted(posts, key=lambda p: (parse_ts(p["ts"]) or datetime.min.replace(tzinfo=timezone.utc), p["id"]),
                  reverse=True)


def apply_retention(doc: dict, now: Optional[datetime] = None) -> int:
    """Prune old posts, dismissals and acknowledgements in place. Returns how
    many entries were dropped."""
    moment = now or datetime.now(timezone.utc)
    dropped = 0
    floor = moment - timedelta(days=POSTS_TTL_DAYS)
    kept = [p for p in doc["posts"] if (parse_ts(p["ts"]) or moment) >= floor]
    dropped += len(doc["posts"]) - len(kept)
    kept = sort_newest_first(kept)
    if len(kept) > POSTS_MAX:
        dropped += len(kept) - POSTS_MAX
        kept = kept[:POSTS_MAX]
    doc["posts"] = kept
    marks_floor = moment - timedelta(days=MARKS_TTL_DAYS)
    for name in ("dismissed", "acked"):
        before = len(doc[name])
        doc[name] = {k: v for k, v in doc[name].items() if (parse_ts(v) or moment) >= marks_floor}
        dropped += before - len(doc[name])
    return dropped


# ── The files ───────────────────────────────────────────────────────────────


def page_path(page: str) -> Path:
    """``~/.quirq/work/<page>/<page>.json``."""
    if page not in PAGES:
        raise ValueError(f"no such Work page: {page!r}")
    return work_dir() / page / f"{page}.json"


def inbox_path() -> Path:
    return page_path("inbox")


def live_path() -> Path:
    return page_path("live")


def history_path() -> Path:
    return page_path("history")


def legacy_path() -> Path:
    """The single file of the first cut, adopted into the three on first use."""
    return work_dir() / "work.json"


def _read_raw(path: Path) -> tuple[Optional[dict], bool]:
    """``(raw, ok)``: ``None`` when absent; ``ok`` False only when the file
    exists and is not valid JSON."""
    if not path.is_file():
        return None, True
    raw = read_json(path)
    if isinstance(raw, dict):
        return raw, True
    try:
        if path.read_text(encoding="utf-8").strip():
            logger.warning("work: %s is not valid JSON; serving it empty and refusing to write", path)
            return None, False
    except OSError:
        logger.warning("work: %s could not be read", path)
        return None, False
    return None, True


def merge(inbox: Optional[dict], live: Optional[dict], history: Optional[dict]) -> dict:
    """The three raw files as one raw document. Unknown keys in each file
    go under ``_extra[page]`` so :func:`split` can put them back."""
    inbox, live, history = inbox or {}, live or {}, history or {}
    sources = dict(history.get("sources")) if isinstance(history.get("sources"), dict) else {}
    attention = inbox.get("attention") if isinstance(inbox.get("attention"), dict) else {}
    kinds = attention.get("connections")
    conn = dict(sources.get("connections")) if isinstance(sources.get("connections"), dict) else {}
    conn["attention"] = kinds if isinstance(kinds, list) else []
    sources["connections"] = conn
    known = {"inbox": set(INBOX_KEYS), "live": set(LIVE_KEYS), "history": set(HISTORY_KEYS)}
    extra = {page: {k: v for k, v in raw.items() if k not in known[page] and k not in ("schema", "updated_at")}
             for page, raw in (("inbox", inbox), ("live", live), ("history", history))}
    stamps = [d.get("updated_at") for d in (inbox, live, history) if isinstance(d.get("updated_at"), str)]
    return {"updated_at": max(stamps) if stamps else None, "sources": sources,
            "dismissed": inbox.get("dismissed"), "acked": inbox.get("acked"), "promoted": inbox.get("promoted"),
            "stream": live.get("stream"),
            "watermark": history.get("watermark"), "pinned": history.get("pinned"), "posts": history.get("posts"),
            "_extra": extra}


def split(doc: dict) -> dict[str, dict]:
    """The normalised document as the three files, ``{page: content}``."""
    sources = {name: {k: v for k, v in cfg.items() if k != "attention"} for name, cfg in doc["sources"].items()}
    head = {"schema": SCHEMA, "updated_at": doc.get("updated_at")}
    extra = doc.get("_extra") or {}
    return {
        "inbox": {**extra.get("inbox", {}), **head, "attention": {"connections": list(doc["sources"]["connections"]["attention"])},
                  "dismissed": doc["dismissed"], "acked": doc["acked"], "promoted": doc["promoted"]},
        "live": {**extra.get("live", {}), **head, "stream": doc["stream"]},
        "history": {**extra.get("history", {}), **head, "sources": sources, "watermark": doc["watermark"],
                    "pinned": doc["pinned"], "posts": doc["posts"]},
    }


def _write_pages(doc: dict) -> None:
    for page, content in split(doc).items():
        path = page_path(page)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, content)


def _adopt_legacy() -> None:
    """Under the lock: turn the first cut's ``work/work.json`` into the
    three files, once, and remove it. Never raises."""
    old = legacy_path()
    if not old.is_file():
        return
    try:
        raw = read_json(old)
        if not isinstance(raw, dict):
            logger.warning("work: %s is not valid JSON; left in place, not adopted", old)
            return
        if any(page_path(page).is_file() for page in PAGES):
            logger.warning("work: %s and the per-page files both exist; keeping the per-page files and leaving it in place", old)
            return
        _write_pages(normalize_document(raw))
        old.unlink()
        logger.info("work: adopted %s into %s", old, work_dir())
    except Exception:  # noqa: BLE001 - adoption is a courtesy; the read must go on
        logger.warning("work: could not adopt %s", old, exc_info=True)


def load_document() -> tuple[dict, bool]:
    """``(document, ok)``: the three files merged and normalised. A missing
    file is empty; ``ok`` is False when any file exists and is not valid
    JSON (then a write is refused, or it would erase what a person could
    still fix)."""
    if legacy_path().is_file():
        with locked(work_dir()):
            _adopt_legacy()
    raws, ok = [], True
    for page in PAGES:
        raw, fine = _read_raw(page_path(page))
        raws.append(raw)
        ok = ok and fine
    return normalize_document(merge(*raws)), ok


def modify(fn: Callable[[dict], bool], *, now: Optional[datetime] = None) -> dict:
    """Locked read-modify-write over the three files. ``fn`` edits the
    merged document in place and returns whether anything changed; the
    files are written (after retention) only then, so a read never creates
    them."""
    with locked(work_dir()):
        _adopt_legacy()
        doc, ok = load_document()
        if not ok:
            raise WorkError("scope_unavailable", "a Work file under ~/.quirq/work is not valid JSON; fix or remove it.", 500)
        if fn(doc):
            pruned = apply_retention(doc, now)
            if pruned:
                logger.info("work: pruned %d entr%s by retention", pruned, "y" if pruned == 1 else "ies")
            doc["updated_at"] = now_iso()
            _write_pages(doc)
        return doc


# ── The marks ───────────────────────────────────────────────────────────────


def dismissal_key(key: str, since: str) -> str:
    return f"{key}@{since or ''}"


def is_dismissed(doc: dict, key: str, since: str) -> bool:
    return dismissal_key(key, since) in doc["dismissed"]


def dismiss(doc: dict, key: str, since: str) -> bool:
    pair = dismissal_key(check_key(key), check_since(since))
    if pair in doc["dismissed"]:
        return False
    doc["dismissed"][pair] = now_iso()
    return True


def undismiss(doc: dict, key: str) -> bool:
    """Drop every dismissal of ``key``, whatever its ``since``."""
    check_key(key)
    prefix = key + "@"
    gone = [k for k in doc["dismissed"] if k == key or k.startswith(prefix)]
    for k in gone:
        del doc["dismissed"][k]
    return bool(gone)


def ack(doc: dict, key: str) -> bool:
    check_key(key)
    if key in doc["acked"]:
        return False
    doc["acked"][key] = now_iso()
    return True


def unack(doc: dict, key: str) -> bool:
    check_key(key)
    return doc["acked"].pop(key, None) is not None


def set_pin(doc: dict, key: str, pinned: bool) -> bool:
    check_key(key)
    if pinned:
        if key in doc["pinned"]:
            return False
        doc["pinned"].append(key)
        return True
    if key not in doc["pinned"]:
        return False
    doc["pinned"].remove(key)
    return True


def set_watermark(doc: dict, ts: str) -> bool:
    """Only ever moves forward."""
    check_ts(ts)
    current = parse_ts(doc.get("watermark"))
    if current is not None and parse_ts(ts) <= current:
        return False
    doc["watermark"] = ts
    return True


def promote(doc: dict, key: str, *, project_id: str, workitem_id: str) -> bool:
    check_key(key)
    if key in doc["promoted"]:
        return False
    doc["promoted"][key] = {"project_id": project_id, "workitem_id": workitem_id, "at": now_iso()}
    return True


def _new_id(existing: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate


def add_post(doc: dict, post: dict) -> dict:
    """Give the post an id and put it first. Call inside :func:`modify`."""
    stored = {"id": _new_id({p["id"] for p in doc["posts"]}), **post}
    doc["posts"].insert(0, stored)
    return stored


def find_post(doc: dict, post_id: str) -> Optional[dict]:
    return next((p for p in doc["posts"] if p["id"] == post_id), None)
