"""The Inbox's policies and the runner's sidecars (docs/work-and-workitems.md
section 18).

The record of an Inbox item is a work item (``<project>/.xo/workitems.json``);
the fact it was made from is ``fact.json`` beside the project's claims file
(:mod:`services.inbox.facts`). This module owns the rest of that folder and
the per-section policy::

    ~/.quirq/inbox/policy/<section>.json         whether and when a session starts, what it may do
    ~/.quirq/projects/<pid>/workitems/<id>/
    ├── fact.json                                the fact (services/inbox/facts.py)
    ├── session.json                             the runner's session state, once one started
    └── outcome.json                             what the session concluded, once it ended

The section of a work item follows its source kind (``facts.section_of_kind``):
``connection`` -> connections, ``sharing`` -> projects, ``github`` -> issues,
``post`` and ``local`` -> agents. Every transition of a session is one
``inbox.item.*`` line on the project's timeline and the Space timeline
(:func:`record_event`); the work item store logs the item's own life
(``workitem.*``). ``retention_days`` removes the sidecars of closed items,
never the work item.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent import project_layout
from services.cowork_agent.scopes import VisualizerScope
from services.cowork_agent.visualizer import workitems_store
from services.cowork_agent.visualizer.sinks import timeline as project_timeline
from services.cowork_agent.visualizer.workspace import timeline as space_timeline
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.inbox import facts
from services.storage.atomic_write import append_jsonl, write_json_atomic
from services.storage.flock import locked
from services.storage.layout import inbox_dir
from services.storage.reader import read_json
from services.timestamps import now_iso, parse_ts

from . import readers, store
from .store import WorkError

logger = logging.getLogger(__name__)

SCHEMA = 1
SECTIONS = facts.SECTIONS
SECTION_LABELS = facts.SECTION_LABELS
STATES = ("new", "running", "waiting", "failed", "closed")
OUTCOME_KINDS = ("reply_drafted", "task_proposed", "needs_you", "fyi", "handled")
WAITING_OUTCOMES = frozenset({"needs_you", "reply_drafted", "task_proposed"})
CLOSING_OUTCOMES = frozenset({"fyi", "handled"})
MODES = ("off", "manual", "auto")
EVENT_TYPES = ("inbox.item.started", "inbox.item.finished", "inbox.item.failed")
#: Connections start a session by themselves; the other sections wait for a
#: person (docs section 18: the safe default until the outcomes look right).
DEFAULT_MODE = {"connections": "auto", "projects": "manual", "issues": "manual", "agents": "manual"}
DEFAULT_SESSIONS: dict = {"mode": "manual", "kinds": [], "agent_type": "inbox-item", "runtime": None,
                          "max_concurrent": 2, "max_per_hour": 20, "timeout_s": 300, "act": False}
DEFAULT_RETENTION_DAYS = 30
_LIMITS = {"max_concurrent": (1, 10), "max_per_hour": (1, 500), "timeout_s": (30, 3600)}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

section_of_kind = facts.section_of_kind


def check_section(value) -> str:
    try:
        return facts.check_section(value)
    except facts.InboxError as exc:
        raise WorkError(exc.code, exc.message, exc.status) from exc


def check_workitem_id(value) -> str:
    try:
        return facts.check_workitem_id(value)
    except facts.InboxError as exc:
        raise WorkError(exc.code, exc.message, exc.status) from exc


def section_of(record: dict, fact: Optional[dict] = None) -> str:
    """The section a work item belongs to: its fact's, else its source kind's."""
    if isinstance(fact, dict) and fact.get("section") in SECTIONS:
        return fact["section"]
    return section_of_kind(workitems_store.source_kind(record))


def item_key(project_id: str, workitem_id: str) -> str:
    return f"workitem:{project_id}:{workitem_id}"


# ── The policy ──────────────────────────────────────────────────────────────


def policy_path(section: str) -> Path:
    return inbox_dir() / "policy" / f"{check_section(section)}.json"


def _int_in(value, name: str, default: int) -> int:
    lo, hi = _LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(hi, max(lo, value))


def normalize_policy(raw, section: str = "connections") -> dict:
    """The policy with every key present and every value in range. Never
    raises: a hand edit that goes wrong falls back to the safe default."""
    doc = dict(raw) if isinstance(raw, dict) else {}
    out = {k: v for k, v in doc.items() if k not in ("schema", "sessions", "retention_days")}
    out["schema"] = SCHEMA
    raw_s = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
    s = dict(DEFAULT_SESSIONS)
    s["mode"] = DEFAULT_MODE.get(section, "manual")
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


def validate_policy(body, section: str) -> dict:
    """A policy from the API: the same shape, but a wrong value is a 400,
    not a silent default."""
    if not isinstance(body, dict):
        raise WorkError("invalid_value", "the policy must be an object.")
    s = body.get("sessions", {})
    if not isinstance(s, dict):
        raise WorkError("invalid_value", "sessions must be an object.")
    if "mode" in s and s["mode"] not in MODES:
        raise WorkError("invalid_value", f"sessions.mode must be one of {list(MODES)}.")
    if "kinds" in s and (not isinstance(s["kinds"], list) or any(not isinstance(k, str) or not k for k in s["kinds"])):
        raise WorkError("invalid_value", "sessions.kinds must be a list of fact kinds.")
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
    unknown = sorted(set(body) - {"schema", "sessions", "retention_days"})
    if unknown:
        raise WorkError("invalid_value", f"unknown policy key(s) {unknown}.")
    return normalize_policy(body, section)


def read_policy(section: str) -> dict:
    """The section's policy; the defaults when it has none written yet."""
    return normalize_policy(read_json(policy_path(section)), section)


def write_policy(section: str, body) -> dict:
    """Validate and write the policy; makes the policy folder."""
    policy = validate_policy(body, section)
    path = policy_path(section)
    with locked(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, policy)
    return policy


# ── The sidecars ────────────────────────────────────────────────────────────


def sidecar_dir(project_id: str, workitem_id: str, *, create: bool = False) -> Optional[Path]:
    return facts.sidecar_dir(project_id, workitem_id, create=create)


def session_path(project_id: str, workitem_id: str) -> Optional[Path]:
    folder = sidecar_dir(project_id, workitem_id)
    return folder / "session.json" if folder is not None else None


def outcome_path(project_id: str, workitem_id: str) -> Optional[Path]:
    folder = sidecar_dir(project_id, workitem_id)
    return folder / "outcome.json" if folder is not None else None


def _read(path: Optional[Path]) -> Optional[dict]:
    doc = read_json(path) if path is not None else None
    return doc if isinstance(doc, dict) else None


def read_fact(project_id: str, workitem_id: str) -> Optional[dict]:
    return facts.read_fact(project_id, workitem_id)


def read_session(project_id: str, workitem_id: str) -> Optional[dict]:
    return _read(session_path(project_id, workitem_id))


def read_outcome(project_id: str, workitem_id: str) -> Optional[dict]:
    return _read(outcome_path(project_id, workitem_id))


def _write(project_id: str, workitem_id: str, name: str, doc: dict) -> None:
    folder = sidecar_dir(project_id, workitem_id, create=True)
    if folder is None:
        raise WorkError("scope_unavailable", f"project {project_id} has no runtime home to keep {name} in.", 500)
    folder.mkdir(parents=True, exist_ok=True)
    write_json_atomic(folder / name, {"schema": SCHEMA, **doc})


def write_session(project_id: str, workitem_id: str, session: dict) -> None:
    _write(project_id, workitem_id, "session.json", session)


def write_outcome(project_id: str, workitem_id: str, outcome: dict) -> None:
    _write(project_id, workitem_id, "outcome.json", outcome)


def workbench_dir(project_id: str, workitem_id: str) -> Path:
    """Where the agent writes: ``<project>/items/<workitem-id>/``."""
    return project_layout.project_dir(project_id) / "items" / check_workitem_id(workitem_id)


def workbench_view(project_id: str, workitem_id: str) -> dict:
    bench = workbench_dir(project_id, workitem_id)
    files = sorted(p.name for p in bench.iterdir() if p.is_file()) if bench.is_dir() else []
    return {"project_id": project_id, "path": f"items/{workitem_id}", "exists": bench.is_dir(), "files": files}


# ── The record ──────────────────────────────────────────────────────────────


def scope_for(project_id: str) -> VisualizerScope:
    if not facts.is_project_id(project_id):
        raise WorkError("invalid_project_id", "project_id must be a project folder name.", 404)
    if project_layout.load_project(project_id) is None:
        raise WorkError("project_not_found", f"project {project_id} does not exist.", 404)
    return VisualizerScope(project_id)


def require_record(project_id: str, workitem_id: str) -> dict:
    """The stored (unprojected) work item, or a 404."""
    check_workitem_id(workitem_id)
    try:
        record = scope_for(project_id).get_workitem(workitem_id)
    except workitems_store.WorkitemsStoreError as exc:
        raise WorkError(exc.code, str(exc), 500 if exc.code == "corrupt_document" else 400) from exc
    if record is None:
        raise WorkError("workitem_not_found", "No such work item.", 404)
    return record


def update_record(project_id: str, workitem_id: str, **fields) -> dict:
    try:
        return scope_for(project_id).update_workitem(workitem_id, **fields)
    except workitems_store.WorkitemsStoreError as exc:
        raise WorkError(exc.code, str(exc), 404 if exc.code == "workitem_not_found" else 400) from exc


def link_session(project_id: str, workitem_id: str, session_id: str) -> dict:
    """Append the session to the work item's ``links.session_ids``."""
    record = require_record(project_id, workitem_id)
    links = record.get("links") if isinstance(record.get("links"), dict) else {}
    ids = [s for s in (links.get("session_ids") or []) if isinstance(s, str)]
    if session_id in ids:
        return record
    return update_record(project_id, workitem_id, session_ids=ids + [session_id])


# ── The timeline lines ──────────────────────────────────────────────────────


def record_event(project_id: str, event_type: str, *, workitem_id: str, title: str, section: str,
                 session_id: Optional[str] = None, runtime: Optional[str] = None, status: Optional[str] = None) -> None:
    """One ``inbox.item.*`` line on the project's timeline and on the Space
    timeline. Never raises."""
    if event_type not in EVENT_TYPES:
        logger.warning("work items: unknown event type %s", event_type)
        return
    line: dict = {"ts": now_iso(), "type": event_type}
    try:
        meta = project_layout.load_project(project_id)
    except Exception:  # noqa: BLE001 - a project that is not there has no pid
        meta = None
    pid = meta.get("pid") if isinstance(meta, dict) else None
    if store.is_pid(pid):
        line["pid"] = pid
    if session_id:
        line["session_id"] = session_id
    if runtime:
        line["runtime"] = runtime
    line["kind"] = section
    line["title"] = readers._one_line(title or workitem_id, store.TITLE_MAX)
    if status:
        line["status"] = status
    line["workitem_id"] = workitem_id
    try:
        root = project_layout.runtime_dir_for_project(project_id, create=True) if meta is not None else None
        if root is not None and root.is_dir():
            project_timeline._rotate_if_needed(root)
            append_jsonl(root / "timeline.jsonl", [line])
        space_timeline.apply([line], project_id=project_id)
    except Exception:  # noqa: BLE001 - a log line must never fail the change it records
        logger.warning("work items: could not record %s for %s/%s", event_type, project_id, workitem_id, exc_info=True)


# ── Retention ───────────────────────────────────────────────────────────────


def _sidecar_ids(project_id: str) -> list[str]:
    root = project_layout.runtime_dir_for_project(project_id)
    folder = root / "workitems" if root is not None else None
    if folder is None or not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_dir() and facts.WORKITEM_ID_RE.fullmatch(p.name))


def sweep(*, now: Optional[datetime] = None) -> int:
    """Remove the sidecars (and the workbench) of work items closed longer
    ago than their section's retention. The work item itself stays.
    Returns how many folders went."""
    moment = now or datetime.now(timezone.utc)
    floors = {section: moment - timedelta(days=read_policy(section)["retention_days"]) for section in SECTIONS}
    gone = 0
    for project_id in sorted(list_project_pids()):
        ids = _sidecar_ids(project_id)
        if not ids:
            continue
        try:
            records = {r["id"]: r for r in VisualizerScope(project_id).list_workitems(include_deleted=True) if isinstance(r.get("id"), str)}
        except Exception:  # noqa: BLE001 - a project whose file cannot be read keeps its sidecars
            logger.warning("work sweep: could not read the work items of %s", project_id, exc_info=True)
            continue
        for workitem_id in ids:
            record = records.get(workitem_id)
            closed_at = None
            if record is None or record.get("deleted_at"):
                closed_at = parse_ts((record or {}).get("deleted_at")) or _EPOCH
            elif record.get("status") == "closed":
                closed_at = parse_ts(record.get("updated_at")) or _EPOCH
            if closed_at is None:
                continue
            section = section_of(record or {}, read_fact(project_id, workitem_id))
            if closed_at >= floors[section]:
                continue
            for folder in (sidecar_dir(project_id, workitem_id), workbench_dir(project_id, workitem_id)):
                try:
                    if folder is not None and folder.is_dir():
                        shutil.rmtree(folder)
                except OSError:
                    logger.warning("work sweep: could not remove %s", folder)
            gone += 1
    return gone
