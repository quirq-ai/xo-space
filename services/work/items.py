"""Connections as folders inside the Inbox, one folder per item (design
section 17)::

    ~/.quirq/work/inbox/<connection>/
    ├── connection.json          the policy: which collectors become items, whether and
    │                            when a session starts, what it may do
    ├── items.json               the index (one summary per item) and the cursor per collector
    └── <collector>-<key>/       one folder per item
        ├── item.json            the fact as collected, its status, when it was decided
        ├── session.json         the session bound to it, once one started
        ├── outcome.json         what the session concluded, once it ended
        ├── thread.jsonl         the conversation on the item: the agent's turns and the person's replies
        └── run.log              the run's output tail; safe to delete

The item folder is the item: written only by this module and the runner,
never by the agent. The agent works in the connection's project,
``~/xo-projects/inbox-<connection>/items/<id>/`` (the workbench).

The maker (:func:`make_items`) turns the tail of the connection's
``events.jsonl`` into item folders for the collectors the policy lists,
newer than the cursor; with no cursor, only the last day. Idempotent by
folder name. Every state change is one ``inbox.item.*`` line on the
connection project's timeline and the Space timeline (:func:`record_event`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from services.connections import store as connections_store
from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.sinks import timeline as project_timeline
from services.cowork_agent.visualizer.workspace import timeline as space_timeline
from services.storage.atomic_write import append_jsonl, write_json_atomic
from services.storage.flock import locked
from services.storage.layout import work_dir
from services.storage.reader import read_json
from services.timestamps import now_iso, parse_ts

from . import readers, store
from .store import WorkError

logger = logging.getLogger(__name__)

SCHEMA = 1
STATUSES = ("new", "queued", "running", "done", "failed", "skipped")
DECISIONS = ("accepted", "dismissed", "tracked")
OUTCOME_KINDS = ("reply_drafted", "task_proposed", "needs_you", "fyi", "handled")
MODES = ("off", "manual", "auto")
EVENT_TYPES = ("inbox.item.created", "inbox.item.started", "inbox.item.finished", "inbox.item.failed", "inbox.item.decided")
DEFAULT_SESSIONS: dict = {"mode": "manual", "kinds": [], "agent_type": "inbox-item", "runtime": None,
                          "max_concurrent": 2, "max_per_hour": 20, "timeout_s": 300, "act": False}
DEFAULT_RETENTION_DAYS = 30
ITEMS_MAX = 500            # per connection; oldest decided items go first
EVENTS_FETCH = 200         # newest events read per connection per run
BOOTSTRAP = timedelta(hours=24)   # no cursor: only the last day becomes items
BODY_MAX = 4000
PROJECT_PREFIX = "inbox-"
ITEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_LIMITS = {"max_concurrent": (1, 10), "max_per_hour": (1, 500), "timeout_s": (30, 3600)}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ── Names and paths ─────────────────────────────────────────────────────────


def check_toolkit(value) -> str:
    if not isinstance(value, str) or connections_store.TOOLKIT_RE.fullmatch(value) is None:
        raise WorkError("invalid_toolkit", "connection must be a toolkit id ([a-z0-9_], 1 to 40 chars).", 404)
    return value


def check_item_id(value) -> str:
    if not isinstance(value, str) or ITEM_ID_RE.fullmatch(value) is None:
        raise WorkError("item_not_found", "No such item.", 404)
    return value


def item_id_for(collector: str, key: str) -> str:
    """``<collector>-<key>`` as a safe folder name. A key that had to be
    changed to fit, or that is long, gets a short hash so two keys never
    share a folder."""
    c = _UNSAFE.sub("-", str(collector)).strip("-.")[:40] or "item"
    raw = str(key)
    k = _UNSAFE.sub("-", raw).strip("-.")
    if k != raw or len(k) > 60 or not k:
        k = (k[:48] + "-" if k else "") + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{c}-{k}"[:120]


def item_key(toolkit: str, item_id: str) -> str:
    return f"item:{toolkit}:{item_id}"


def parse_item_key(key) -> Optional[tuple[str, str]]:
    if not isinstance(key, str) or not key.startswith("item:"):
        return None
    parts = key.split(":", 2)
    if len(parts) != 3 or connections_store.TOOLKIT_RE.fullmatch(parts[1]) is None \
            or ITEM_ID_RE.fullmatch(parts[2]) is None:
        return None
    return parts[1], parts[2]


def project_id_for(toolkit: str) -> str:
    """The connection's project, where its sessions run."""
    return PROJECT_PREFIX + toolkit


def inbox_dir() -> Path:
    return work_dir() / "inbox"


def connection_dir(toolkit: str) -> Path:
    return inbox_dir() / check_toolkit(toolkit)


def policy_path(toolkit: str) -> Path:
    return connection_dir(toolkit) / "connection.json"


def index_path(toolkit: str) -> Path:
    return connection_dir(toolkit) / "items.json"


def item_dir(toolkit: str, item_id: str) -> Path:
    return connection_dir(toolkit) / check_item_id(item_id)


def item_path(toolkit: str, item_id: str) -> Path:
    return item_dir(toolkit, item_id) / "item.json"


def session_path(toolkit: str, item_id: str) -> Path:
    return item_dir(toolkit, item_id) / "session.json"


def outcome_path(toolkit: str, item_id: str) -> Path:
    return item_dir(toolkit, item_id) / "outcome.json"


def log_path(toolkit: str, item_id: str) -> Path:
    return item_dir(toolkit, item_id) / "run.log"


def thread_path(toolkit: str, item_id: str) -> Path:
    return item_dir(toolkit, item_id) / "thread.jsonl"


def list_connections() -> list[str]:
    """The connections that have a folder with a policy, sorted."""
    root = inbox_dir()
    if not root.is_dir():
        return []
    out = []
    for child in root.iterdir():
        if child.is_dir() and connections_store.TOOLKIT_RE.fullmatch(child.name) and (child / "connection.json").is_file():
            out.append(child.name)
    return sorted(out)


# ── The policy ──────────────────────────────────────────────────────────────


def _int_in(value, name: str, default: int) -> int:
    lo, hi = _LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(hi, max(lo, value))


def normalize_policy(raw) -> dict:
    """The policy with every key present and every value in range. Never
    raises: a hand edit that goes wrong falls back to the safe default."""
    doc = dict(raw) if isinstance(raw, dict) else {}
    out = {k: v for k, v in doc.items() if k not in ("schema", "items", "sessions", "retention_days")}
    out["schema"] = SCHEMA
    items = doc.get("items")
    out["items"] = {k: bool(v) for k, v in items.items() if isinstance(k, str) and k} if isinstance(items, dict) else {}
    raw_s = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
    s = dict(DEFAULT_SESSIONS)
    if raw_s.get("mode") in MODES:
        s["mode"] = raw_s["mode"]
    kinds = raw_s.get("kinds")
    s["kinds"] = [k for k in kinds if isinstance(k, str) and k] if isinstance(kinds, list) else []
    if isinstance(raw_s.get("agent_type"), str) and raw_s["agent_type"].strip():
        s["agent_type"] = raw_s["agent_type"].strip()
    s["runtime"] = raw_s["runtime"] if isinstance(raw_s.get("runtime"), str) and raw_s["runtime"].strip() else None
    for name in _LIMITS:
        s[name] = _int_in(raw_s.get(name), name, DEFAULT_SESSIONS[name])
    s["act"] = raw_s.get("act") is True
    out["sessions"] = s
    days = doc.get("retention_days")
    out["retention_days"] = days if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 365 else DEFAULT_RETENTION_DAYS
    return out


def validate_policy(body) -> dict:
    """A policy from the API: the same shape, but a wrong value is a 400,
    not a silent default."""
    if not isinstance(body, dict):
        raise WorkError("invalid_value", "the policy must be an object.")
    items = body.get("items", {})
    if not isinstance(items, dict) or any(not isinstance(k, str) or not k or not isinstance(v, bool) for k, v in items.items()):
        raise WorkError("invalid_value", "items must map collector ids to true or false.")
    s = body.get("sessions", {})
    if not isinstance(s, dict):
        raise WorkError("invalid_value", "sessions must be an object.")
    if "mode" in s and s["mode"] not in MODES:
        raise WorkError("invalid_value", f"sessions.mode must be one of {list(MODES)}.")
    if "kinds" in s and (not isinstance(s["kinds"], list) or any(not isinstance(k, str) or not k for k in s["kinds"])):
        raise WorkError("invalid_value", "sessions.kinds must be a list of collector ids.")
    if "agent_type" in s and (not isinstance(s["agent_type"], str) or not s["agent_type"].strip()):
        raise WorkError("invalid_value", "sessions.agent_type must be a skill name.")
    if "runtime" in s and s["runtime"] is not None and (not isinstance(s["runtime"], str) or not s["runtime"].strip()):
        raise WorkError("invalid_value", "sessions.runtime must be an agent name or null.")
    for name, (lo, hi) in _LIMITS.items():
        if name in s and (isinstance(s[name], bool) or not isinstance(s[name], int) or not lo <= s[name] <= hi):
            raise WorkError("invalid_value", f"sessions.{name} must be an integer between {lo} and {hi}.")
    if "act" in s and not isinstance(s["act"], bool):
        raise WorkError("invalid_value", "sessions.act must be true or false.")
    days = body.get("retention_days", DEFAULT_RETENTION_DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise WorkError("invalid_value", "retention_days must be an integer between 1 and 365.")
    unknown = sorted(set(body) - {"schema", "items", "sessions", "retention_days"})
    if unknown:
        raise WorkError("invalid_value", f"unknown policy key(s) {unknown}.")
    return normalize_policy(body)


def read_policy(toolkit: str) -> Optional[dict]:
    """The connection's policy, or ``None`` when it has no folder."""
    raw = read_json(policy_path(toolkit))
    return normalize_policy(raw) if isinstance(raw, dict) else None


def write_policy(toolkit: str, body) -> dict:
    """Validate and write the policy; makes the connection's folder."""
    policy = validate_policy(body)
    path = policy_path(toolkit)
    with locked(connection_dir(toolkit)):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, policy)
    return policy


# ── The index ───────────────────────────────────────────────────────────────


def _empty_index() -> dict:
    return {"schema": SCHEMA, "updated_at": None, "cursors": {}, "items": {}}


def normalize_index(raw) -> dict:
    idx = _empty_index()
    if not isinstance(raw, dict):
        return idx
    idx["updated_at"] = raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None
    cursors = raw.get("cursors")
    idx["cursors"] = {k: v for k, v in cursors.items() if isinstance(k, str) and parse_ts(v)} if isinstance(cursors, dict) else {}
    items = raw.get("items")
    if isinstance(items, dict):
        for item_id, summary in items.items():
            if isinstance(item_id, str) and ITEM_ID_RE.fullmatch(item_id) and isinstance(summary, dict):
                idx["items"][item_id] = dict(summary)
    return idx


def read_index(toolkit: str) -> dict:
    return normalize_index(read_json(index_path(toolkit)))


def _write_index(toolkit: str, idx: dict) -> None:
    idx["updated_at"] = now_iso()
    index_path(toolkit).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(index_path(toolkit), idx)


def summary_of(record: dict) -> dict:
    """What the index keeps per item: enough to list and to count, never the body."""
    decided = record.get("decided") if isinstance(record.get("decided"), dict) else None
    return {"status": record.get("status") or "new", "ts": record.get("ts"), "collector": record.get("collector"),
            "kind": record.get("kind"), "title": record.get("title"), "outcome": record.get("outcome"),
            "decided": decided.get("action") if decided else None, "decided_at": decided.get("at") if decided else None,
            "session_id": record.get("session_id"), "updated_at": record.get("updated_at")}


# ── The item's files ────────────────────────────────────────────────────────


def _read(path: Path) -> Optional[dict]:
    doc = read_json(path)
    return doc if isinstance(doc, dict) else None


def read_item(toolkit: str, item_id: str) -> Optional[dict]:
    return _read(item_path(toolkit, item_id))


def read_session(toolkit: str, item_id: str) -> Optional[dict]:
    return _read(session_path(toolkit, item_id))


def read_outcome(toolkit: str, item_id: str) -> Optional[dict]:
    return _read(outcome_path(toolkit, item_id))


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, doc)


def write_session(toolkit: str, item_id: str, session: dict) -> None:
    _write(session_path(toolkit, item_id), {"schema": SCHEMA, **session})


def write_outcome(toolkit: str, item_id: str, outcome: dict) -> None:
    _write(outcome_path(toolkit, item_id), {"schema": SCHEMA, **outcome})


THREAD_ROLES = ("agent", "person", "system")
THREAD_TEXT_MAX = 16000
THREAD_READ_MAX = 200


def append_thread(toolkit: str, item_id: str, role: str, text: str, *, session_id: Optional[str] = None,
                  attempt: Optional[int] = None, outcome: Optional[str] = None) -> dict:
    """One turn on the item's thread: what the agent said (its answer without
    the outcome block), what the person replied, or a system note. The
    thread is the item's own record of the exchange; the runtime's transcript
    stays where the runtime keeps it."""
    if role not in THREAD_ROLES:
        raise ValueError(f"thread role must be one of {THREAD_ROLES}")
    line: dict = {"ts": now_iso(), "type": role, "text": (text or "").strip()[:THREAD_TEXT_MAX]}
    if session_id:
        line["session_id"] = session_id
    if attempt is not None:
        line["attempt"] = attempt
    if outcome:
        line["outcome"] = outcome
    path = thread_path(toolkit, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, [line])
    return line


def read_thread(toolkit: str, item_id: str, *, limit: int = THREAD_READ_MAX) -> list[dict]:
    """The thread in order, oldest first; a torn line is skipped."""
    path = thread_path(toolkit, item_id)
    if not path.is_file():
        return []
    out = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(line, dict) and line.get("type") in THREAD_ROLES and isinstance(line.get("text"), str):
                out.append(line)
    except OSError:
        logger.warning("work items: could not read %s", path)
    return out[-limit:]


def write_log(toolkit: str, item_id: str, text: str) -> None:
    try:
        log_path(toolkit, item_id).write_text(text[-8000:], encoding="utf-8")
    except OSError:
        logger.warning("work items: could not write %s", log_path(toolkit, item_id))


def require_item(toolkit: str, item_id: str) -> dict:
    record = read_item(toolkit, item_id)
    if record is None:
        raise WorkError("item_not_found", "No such item.", 404)
    return record


def update_item(toolkit: str, item_id: str, **fields) -> dict:
    """Change fields on the record and its index summary in one locked write."""
    with locked(connection_dir(toolkit)):
        record = require_item(toolkit, item_id)
        record.update(fields)
        record["updated_at"] = now_iso()
        _write(item_path(toolkit, item_id), record)
        idx = read_index(toolkit)
        idx["items"][item_id] = summary_of(record)
        _write_index(toolkit, idx)
    return record


def build_item(toolkit: str, ev: dict, item_id: str, now_text: str) -> dict:
    url = ev.get("url") if store.is_url(ev.get("url")) else None
    body = ev.get("body") if isinstance(ev.get("body"), str) else ""
    title = ev.get("title") if isinstance(ev.get("title"), str) and ev.get("title") else f"{toolkit} {ev['type']}: {ev['key']}"
    return {"schema": SCHEMA, "id": item_id, "connection": toolkit, "collector": ev["type"], "key": ev["key"], "ts": ev["ts"],
            "kind": readers.connection_kind(toolkit, ev["type"]), "title": readers._one_line(title, store.TITLE_MAX),
            "body": body[:BODY_MAX], "url": url, "status": "new", "created_at": now_text, "updated_at": now_text,
            "decided": None, "session_id": None, "outcome": None}


# ── The maker ───────────────────────────────────────────────────────────────


def make_items(toolkit: str, *, now: Optional[datetime] = None) -> list[str]:
    """Item folders for the listed collectors' events newer than the
    cursor (the last day when there is none). Returns the new ids."""
    policy = read_policy(toolkit)
    if policy is None:
        return []
    collectors = {c for c, on in policy["items"].items() if on}
    if not collectors:
        return []
    try:
        events = connections_store.read_events(toolkit, limit=EVENTS_FETCH)
    except Exception as exc:  # noqa: BLE001 - a bad events file makes no items
        logger.warning("work items: could not read events for %s: %s", toolkit, exc)
        return []
    moment = now or datetime.now(timezone.utc)
    now_text = now_iso() if now is None else moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    created: list[dict] = []
    with locked(connection_dir(toolkit)):
        idx = read_index(toolkit)
        cursors = dict(idx["cursors"])
        newest = dict(cursors)
        for ev in reversed(events):   # oldest first, so a partial run leaves a sane cursor
            if not isinstance(ev, dict) or ev.get("type") not in collectors \
                    or not isinstance(ev.get("key"), str) or not ev["key"]:
                continue
            ts = parse_ts(ev.get("ts"))
            if ts is None:
                continue
            collector = ev["type"]
            if parse_ts(newest.get(collector)) is None or ts > parse_ts(newest[collector]):
                newest[collector] = ev["ts"]
            floor = parse_ts(cursors.get(collector)) or (moment - BOOTSTRAP)
            if ts <= floor:
                continue
            item_id = item_id_for(collector, ev["key"])
            if item_id in idx["items"] or item_dir(toolkit, item_id).exists():
                continue
            record = build_item(toolkit, ev, item_id, now_text)
            _write(item_path(toolkit, item_id), record)
            idx["items"][item_id] = summary_of(record)
            created.append(record)
        if created or newest != cursors:
            idx["cursors"] = newest
            _write_index(toolkit, idx)
    for record in created:
        record_event(toolkit, "inbox.item.created", record, status="new")
    return [r["id"] for r in created]


# ── Decisions, listing, retention ───────────────────────────────────────────


def mark_decided(toolkit: str, item_id: str, action: str, *, extra: Optional[dict] = None) -> dict:
    if action not in DECISIONS:
        raise WorkError("invalid_value", f"action must be one of {list(DECISIONS)}.")
    record = update_item(toolkit, item_id, decided={"action": action, "at": now_iso(), **(extra or {})})
    record_event(toolkit, "inbox.item.decided", record, status=action)
    return record


def list_items(toolkit: Optional[str] = None, *, status: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Index rows across connections, newest first."""
    if status is not None and status not in STATUSES:
        raise WorkError("invalid_value", f"status must be one of {list(STATUSES)}.")
    rows = []
    for name in ([check_toolkit(toolkit)] if toolkit else list_connections()):
        for item_id, summary in read_index(name)["items"].items():
            if status is not None and summary.get("status") != status:
                continue
            rows.append({"id": item_id, "connection": name, "key": item_key(name, item_id), **summary})
    rows.sort(key=lambda r: (parse_ts(r.get("ts")) or _EPOCH, r["id"]), reverse=True)
    return rows[:max(1, limit)]


def workbench_dir(toolkit: str, item_id: str) -> Path:
    return project_layout.project_dir(project_id_for(toolkit)) / "items" / check_item_id(item_id)


def item_detail(toolkit: str, item_id: str) -> dict:
    record = require_item(toolkit, item_id)
    bench = workbench_dir(toolkit, item_id)
    files = sorted(p.name for p in bench.iterdir() if p.is_file()) if bench.is_dir() else []
    return {"item": record, "session": read_session(toolkit, item_id), "outcome": read_outcome(toolkit, item_id),
            "thread": read_thread(toolkit, item_id), "policy": read_policy(toolkit), "key": item_key(toolkit, item_id),
            "workbench": {"project_id": project_id_for(toolkit), "path": f"items/{item_id}", "exists": bench.is_dir(), "files": files}}


def sweep(toolkit: str, *, now: Optional[datetime] = None) -> int:
    """Remove item folders decided or failed longer ago than the policy's
    retention, then the oldest decided ones past the cap. Returns how many
    went. The workbench folder in the project goes with the item."""
    policy = read_policy(toolkit)
    if policy is None:
        return 0
    moment = now or datetime.now(timezone.utc)
    floor = moment - timedelta(days=policy["retention_days"])
    gone: list[str] = []
    with locked(connection_dir(toolkit)):
        idx = read_index(toolkit)
        for item_id, s in list(idx["items"].items()):
            done_at = parse_ts(s.get("decided_at")) if s.get("decided") else (parse_ts(s.get("updated_at")) if s.get("status") == "failed" else None)
            if done_at is not None and done_at < floor:
                gone.append(item_id)
        if len(idx["items"]) - len(gone) > ITEMS_MAX:
            decided = sorted((item_id for item_id, s in idx["items"].items() if s.get("decided") and item_id not in gone),
                             key=lambda i: parse_ts(idx["items"][i].get("decided_at")) or _EPOCH)
            gone.extend(decided[: len(idx["items"]) - len(gone) - ITEMS_MAX])
        for item_id in gone:
            idx["items"].pop(item_id, None)
            for folder in (item_dir(toolkit, item_id), workbench_dir(toolkit, item_id)):
                try:
                    if folder.is_dir():
                        shutil.rmtree(folder)
                except OSError:
                    logger.warning("work items: could not remove %s", folder)
        if gone:
            _write_index(toolkit, idx)
    return len(gone)


# ── The entry the rest of the Work sees, and the timeline lines ─────────────


def item_entry(toolkit: str, record: dict, outcome: Optional[dict] = None) -> dict:
    """An item as a feed entry (section 10), so promote and the pages treat
    it like any other fact."""
    detail = (outcome or {}).get("summary") or (record.get("body") or "")
    return readers._entry(key=item_key(toolkit, record["id"]), ts=record.get("ts") or record.get("created_at"), source="inbox",
                          kind=record.get("kind") or f"{toolkit}.item", title=record.get("title") or record["id"], detail=detail,
                          project_id=None, actor=None,
                          ref={"url": record.get("url"), "toolkit": toolkit, "item_id": record["id"], "collector": record.get("collector"),
                               "session_id": record.get("session_id"), "project_id": project_id_for(toolkit),
                               "workbench": f"items/{record['id']}", "outcome": outcome},
                          tone="attention" if record.get("status") in ("new", "done", "failed") and not record.get("decided") else "info",
                          toolkit=toolkit, status=record.get("status"))


def record_event(toolkit: str, event_type: str, record: dict, *, session_id: Optional[str] = None,
                 runtime: Optional[str] = None, status: Optional[str] = None) -> None:
    """One ``inbox.item.*`` line on the connection project's timeline (when
    the project exists) and on the Space timeline. Never raises."""
    if event_type not in EVENT_TYPES:
        logger.warning("work items: unknown event type %s", event_type)
        return
    project_id = project_id_for(toolkit)
    line: dict = {"ts": now_iso(), "type": event_type}
    try:
        meta = project_layout.load_project(project_id)
    except Exception:  # noqa: BLE001 - a project that is not there yet has no pid
        meta = None
    pid = meta.get("pid") if isinstance(meta, dict) else None
    if store.is_pid(pid):
        line["pid"] = pid
    if session_id:
        line["session_id"] = session_id
    if runtime:
        line["runtime"] = runtime
    line["kind"] = toolkit
    line["title"] = readers._one_line(record.get("title") or record.get("id") or "", store.TITLE_MAX)
    if status:
        line["status"] = status
    line["item_id"] = record.get("id")
    try:
        # The project's runtime home is made on first use, as the todo and
        # work item stores rely on the watcher to do; the item log must not
        # wait for a tick.
        root = project_layout.runtime_dir_for_project(project_id, create=True) if meta is not None else None
        if root is not None and root.is_dir():
            project_timeline._rotate_if_needed(root)
            append_jsonl(root / "timeline.jsonl", [line])
        space_timeline.apply([line], project_id=project_id)
    except Exception:  # noqa: BLE001 - a log line must never fail the change it records
        logger.warning("work items: could not record %s for %s/%s", event_type, toolkit, record.get("id"), exc_info=True)
