"""The Inbox as a join (docs/work-and-workitems.md section 18): every
project's work items, each with its fact, its claim, its session sidecars
and the live set the watcher keeps, plus the runtime sessions no work item
owns, as one list of rows. The tabs of the Inbox (Connections, Projects,
Issues, Agents) are facets over that list, never separate stores.

Nothing here writes. The runner (:mod:`services.work.runner`) owns the
sessions; this module only reads what it and the watcher left, so a read
can never start, stop or change anything. The set of items the runner is
driving right now is passed in by the caller (the service), because the
runner imports this module for its candidates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from services.connections import store as connections_store
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine import sessions_io
from services.cowork_agent.scopes import VisualizerScope, resolve_scope
from services.cowork_agent.visualizer import workitem_claims, workitems_store
from services.cowork_agent.visualizer.state import workspace_activity_path
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.storage.reader import read_json
from services.timestamps import iso, parse_ts

from . import items
from .store import WorkError

logger = logging.getLogger(__name__)

STATE_FILTERS = {
    "open": frozenset({"new", "running", "waiting", "failed"}),
    "active": frozenset({"running"}),
    "waiting": frozenset({"waiting"}),
    "closed": frozenset({"closed"}),
    "all": frozenset(items.STATES),
}
LIMIT_MAX = 500
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def check_state(value) -> str:
    if value is None:
        return "open"
    if not isinstance(value, str) or value not in STATE_FILTERS:
        raise WorkError("invalid_value", f"state must be one of {list(STATE_FILTERS)}.")
    return value


# ── What the watcher and the runner left ────────────────────────────────────


def live_sessions() -> frozenset[str]:
    """The session ids the watcher sees open right now, across projects."""
    return workitem_claims.live_session_ids(read_json(workspace_activity_path()))


def rollup_rows() -> list[dict]:
    """Every project's work items, projected against the mirror, with
    ``_project_id``, ``_pid`` and ``_in_progress`` (a live claim)."""
    return list(resolve_scope("xo-workspace-visualizer").rollup_workitems().rows)


def claims_of(project_id: str) -> dict:
    try:
        return VisualizerScope(project_id).read_claims()
    except Exception:  # noqa: BLE001 - a project without a runtime home has no claims
        return {}


# ── One row ─────────────────────────────────────────────────────────────────


def state_of(record: dict, *, session: Optional[dict], outcome: Optional[dict], in_progress: bool, running: bool) -> str:
    """The row's state, in the order the contract fixes: closed, running,
    failed, waiting, new."""
    if record.get("status") == "closed":
        return "closed"
    if running or in_progress:
        return "running"
    if session:
        exit_ = session.get("exit") if isinstance(session.get("exit"), dict) else None
        if session.get("ended_at") is None or (exit_ and exit_.get("status") not in (None, "ok")):
            return "failed"
    if outcome and outcome.get("kind") in items.WAITING_OUTCOMES:
        return "waiting"
    return "new"


def entity_of(record: dict, fact: Optional[dict], section: str, project_id: str) -> Optional[str]:
    """The group a work item sits under inside a tab."""
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    kind = source.get("kind")
    block = source.get(kind) if isinstance(source.get(kind), dict) else {}
    if section == "projects":
        return project_id
    if section == "connections":
        return (fact or {}).get("entity") or block.get("toolkit") or None
    if section == "issues":
        return block.get("repo") or (fact or {}).get("entity") or None
    if section == "agents":
        assignee = record.get("assignee")
        if isinstance(assignee, str) and assignee:
            return assignee
        if kind == "post" and block.get("agent"):
            return block["agent"]
        created_by = record.get("created_by")
        return created_by if isinstance(created_by, str) and created_by else None
    return None


def _fact_view(fact: Optional[dict]) -> Optional[dict]:
    if not isinstance(fact, dict):
        return None
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    kind = source.get("kind")
    block = source.get(kind) if isinstance(source.get(kind), dict) else {}
    return {"ts": fact.get("ts"), "kind": fact.get("kind"), "url": fact.get("url"), "link": fact.get("link"),
            "toolkit": block.get("toolkit") if kind == "connection" else None, "entity": fact.get("entity"),
            "project_id": fact.get("project_id"), "ingested_at": fact.get("ingested_at")}


def _session_view(session: Optional[dict]) -> Optional[dict]:
    if not isinstance(session, dict):
        return None
    return {"session_id": session.get("session_id"), "native_session_id": session.get("native_session_id"),
            "runtime": session.get("runtime"), "attempt": session.get("attempt"), "agent_type": session.get("agent_type"),
            "started_at": session.get("started_at"), "resumed_at": session.get("resumed_at"),
            "ended_at": session.get("ended_at"), "exit": session.get("exit"), "manual": session.get("manual")}


def _outcome_view(outcome: Optional[dict]) -> Optional[dict]:
    if not isinstance(outcome, dict):
        return None
    return {k: outcome.get(k) for k in ("kind", "summary", "draft", "task", "question", "acted", "at")}


def _claim_view(claim: Optional[dict], live: frozenset[str], in_progress: bool) -> Optional[dict]:
    if not isinstance(claim, dict):
        return None
    return {"session_id": claim.get("session_id"), "runtime": claim.get("runtime"), "started_at": claim.get("started_at"),
            "live": bool(in_progress or claim.get("session_id") in live)}


def workitem_row(record: dict, *, project_id: str, pid: Optional[str], in_progress: bool, running: bool,
                 claim: Optional[dict], live: frozenset[str], fact: Optional[dict], session: Optional[dict],
                 outcome: Optional[dict]) -> dict:
    section = items.section_of(record, fact)
    state = state_of(record, session=session, outcome=outcome, in_progress=in_progress, running=running)
    stamps = [record.get("updated_at"), record.get("created_at"), (fact or {}).get("ts"),
              (session or {}).get("ended_at"), (session or {}).get("resumed_at"), (session or {}).get("started_at"),
              (outcome or {}).get("at")]
    latest = max((parse_ts(s) for s in stamps if isinstance(s, str) and parse_ts(s)), default=None)
    github = (record.get("source") or {}).get("github") if isinstance(record.get("source"), dict) else None
    links = record.get("links") if isinstance(record.get("links"), dict) else {}
    return {
        "kind": "workitem", "key": items.item_key(project_id, record["id"]), "id": record["id"],
        "project_id": project_id, "pid": pid, "title": record.get("title") or record["id"],
        "section": section, "entity": entity_of(record, fact, section, project_id),
        "entities": {name: entity_of(record, fact, name, project_id) for name in items.SECTIONS},
        "state": state, "status": record.get("status"), "state_reason": record.get("state_reason"),
        "stale": bool(record.get("stale")), "assignee": record.get("assignee") or None,
        "labels": list(record.get("labels") or []), "source": dict(record.get("source") or {"kind": "local"}),
        "issue": {"repo": github.get("repo"), "number": github.get("number"), "url": github.get("url")} if isinstance(github, dict) else None,
        "fact": _fact_view(fact), "claim": _claim_view(claim, live, in_progress),
        "session": _session_view(session), "outcome": _outcome_view(outcome),
        "session_ids": [s for s in (links.get("session_ids") or []) if isinstance(s, str)],
        "created_at": record.get("created_at"), "updated_at": iso(latest) if latest else record.get("updated_at"),
    }


def session_row(session: dict, *, live: frozenset[str]) -> Optional[dict]:
    """A runtime session no work item owns, as a row of the Projects and
    Agents tabs."""
    session_id = session.get("id")
    project_id = session.get("project_id")
    if not isinstance(session_id, str) or not session_id or not isinstance(project_id, str) or not project_id:
        return None
    native = session.get("native_session_id")
    is_live = session_id in live or (isinstance(native, str) and native in live)
    return {
        "kind": "session", "key": f"session:{project_id}:{session_id}", "id": session_id, "project_id": project_id,
        "pid": None, "title": session.get("title") or f"Session {session_id[:8]}", "section": "projects",
        "entity": project_id, "entities": {"projects": project_id, "agents": session.get("backend") or session.get("agent")},
        "state": "running" if is_live else "closed", "runtime": session.get("backend") or session.get("agent"),
        "native_id": native, "live": is_live, "started_at": session.get("time_created"),
        "updated_at": session.get("time_updated"),
    }


# ── The whole list ──────────────────────────────────────────────────────────


def workitem_rows(running: Iterable[tuple[str, str]] = ()) -> list[dict]:
    """Every work item of every project as a row (unfiltered)."""
    running_keys = set(running)
    live = live_sessions()
    claims_by_project: dict[str, dict] = {}
    out: list[dict] = []
    for record in rollup_rows():
        project_id, wid = record.get("_project_id"), record.get("id")
        if not isinstance(project_id, str) or not isinstance(wid, str) or record.get("deleted_at"):
            continue
        if project_id not in claims_by_project:
            claims_by_project[project_id] = claims_of(project_id)
        claim = claims_by_project[project_id].get(wid)
        try:
            fact = items.read_fact(project_id, wid)
            session = items.read_session(project_id, wid)
            outcome = items.read_outcome(project_id, wid)
        except Exception:  # noqa: BLE001 - a torn sidecar hides only itself
            logger.warning("work inbox: could not read the sidecars of %s/%s", project_id, wid, exc_info=True)
            fact = session = outcome = None
        out.append(workitem_row(record, project_id=project_id, pid=record.get("_pid"), in_progress=bool(record.get("_in_progress")),
                                running=(project_id, wid) in running_keys, claim=claim if isinstance(claim, dict) else None,
                                live=live, fact=fact, session=session, outcome=outcome))
    return out


def orphan_session_rows(linked: set[str]) -> list[dict]:
    """Runtime sessions that no work item links or claims."""
    live = live_sessions()
    out = []
    try:
        sessions = sessions_io.load_all_sessions()
    except Exception:  # noqa: BLE001 - a broken index hides only the sessions
        logger.warning("work inbox: could not list the sessions", exc_info=True)
        return out
    for session in sessions:
        if session.get("id") in linked or session.get("native_session_id") in linked:
            continue
        row = session_row(session, live=live)
        if row is not None:
            out.append(row)
    return out


def _linked_session_ids(rows: list[dict]) -> set[str]:
    linked: set[str] = set()
    for row in rows:
        linked.update(row.get("session_ids") or [])
        for block in (row.get("session"), row.get("claim")):
            if isinstance(block, dict):
                for key in ("session_id", "native_session_id"):
                    if isinstance(block.get(key), str):
                        linked.add(block[key])
    return linked


def _agents() -> list[dict]:
    """Every agent the active runtime knows, as ``{id, label}``."""
    mod = try_load_capability("agents")
    fn = getattr(mod, "list_agents", None) if mod else None
    out = []
    try:
        for agent in (fn() if fn else []):
            if not isinstance(agent, dict):
                continue
            agent_id = agent.get("id") or agent.get("name")
            meta = agent.get("metadata") if isinstance(agent.get("metadata"), dict) else {}
            if isinstance(agent_id, str) and agent_id:
                out.append({"id": agent_id, "label": meta.get("display_name") or agent_id})
    except Exception:  # noqa: BLE001 - a broken agents capability hides only the list
        logger.warning("work inbox: could not list the agents", exc_info=True)
    return out


def _entity_seed(section: str) -> list[dict]:
    """The entities a tab lists even with no rows: every project, every
    agent, every configured connection."""
    if section == "projects":
        return [{"id": name, "label": name} for name in sorted(list_project_pids())]
    if section == "agents":
        return _agents()
    if section == "connections":
        try:
            return [{"id": tk, "label": tk} for tk in connections_store.list_configured()]
        except Exception:  # noqa: BLE001 - no connections folder yet
            return []
    return []


def _count(rows: Iterable[dict]) -> dict:
    counts = {state: 0 for state in items.STATES}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return counts


def sections_of(rows: list[dict], *, with_entities: bool = True) -> list[dict]:
    """One entry per section with the counts of the rows that belong to
    it (a work item can belong to several tabs) and the entities inside."""
    out = []
    for section in items.SECTIONS:
        mine = [r for r in rows if _belongs(r, section)]
        entities: dict[str, dict] = {}
        if with_entities:
            for seed in _entity_seed(section):
                entities.setdefault(seed["id"], {"id": seed["id"], "label": seed["label"], "rows": []})
            for row in mine:
                entity = (row.get("entities") or {}).get(section) or row.get("entity")
                if not isinstance(entity, str) or not entity:
                    continue
                entities.setdefault(entity, {"id": entity, "label": entity, "rows": []})["rows"].append(row)
        out.append({"id": section, "label": items.SECTION_LABELS[section], "counts": _count(mine),
                    "entities": [{"id": e["id"], "label": e["label"], "counts": _count(e["rows"])} for e in entities.values()]})
    return out


def _belongs(row: dict, section: str) -> bool:
    if row["kind"] == "session":
        return section in ("projects", "agents") and bool((row.get("entities") or {}).get(section))
    if section == "projects":
        return True
    entity = (row.get("entities") or {}).get(section)
    return row.get("section") == section or (section == "agents" and bool(entity)) or (section in ("connections", "issues") and bool(entity))


def build(*, section: Optional[str] = None, entity: Optional[str] = None, state: str = "open", limit: int = 100,
          running: Iterable[tuple[str, str]] = ()) -> dict:
    """The Inbox: the sections with their counts and entities, and the rows
    that pass the filters, newest first."""
    if section is not None:
        items.check_section(section)
    state = check_state(state)
    if not 1 <= limit <= LIMIT_MAX:
        raise WorkError("invalid_value", f"limit must be between 1 and {LIMIT_MAX}.")
    rows = workitem_rows(running)
    if section in ("projects", "agents"):
        rows = rows + orphan_session_rows(_linked_session_ids(rows))
    sections = sections_of(rows)
    wanted = STATE_FILTERS[state]
    chosen = []
    for row in rows:
        if section is not None and not _belongs(row, section):
            continue
        if entity is not None and ((row.get("entities") or {}).get(section or row["section"]) or row.get("entity")) != entity:
            continue
        if row["state"] not in wanted:
            continue
        chosen.append(row)
    chosen.sort(key=lambda r: (parse_ts(r.get("updated_at")) or _EPOCH, r["key"]), reverse=True)
    return {"schema": items.SCHEMA, "generated_at": iso(datetime.now(timezone.utc)), "sections": sections,
            "rows": chosen[:limit], "count": len(chosen), "state": state, "section": section, "entity": entity}


def candidates(section: str, *, kinds: Iterable[str] = ()) -> list[tuple[str, str, dict]]:
    """Open work items of the section with no session yet, oldest first:
    ``(project_id, workitem_id, fact)``. ``kinds`` narrows to fact kinds."""
    wanted = set(kinds)
    out = []
    for row in workitem_rows():
        if row["section"] != section or row["state"] != "new" or row.get("session") is not None or row.get("fact") is None:
            continue   # a work item nobody fed (made by hand, or adopted by a person) is not the runner's
        fact = items.read_fact(row["project_id"], row["id"]) or {}
        if wanted and fact.get("kind") not in wanted:
            continue
        stamp = parse_ts(fact.get("ts")) or parse_ts(row.get("created_at")) or _EPOCH
        out.append((stamp, row["project_id"], row["id"], fact))
    out.sort(key=lambda entry: (entry[0], entry[2]))
    return [(p, w, f) for _s, p, w, f in out]


def running_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["kind"] == "workitem" and r["state"] == "running"]
