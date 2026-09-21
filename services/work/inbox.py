"""The Inbox page's four groups, composed on every read (design section
16.5): decisions (attention minus dismissals), calendar (meetings from now
to the end of tomorrow), completed (what finished in the last day and was
not acknowledged) and work (the open work items with who is on them).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.connections import store as connections_store
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.timestamps import iso, parse_ts

from services.inbox import facts

from . import attention, inbox_view, items, readers, store

logger = logging.getLogger(__name__)

CALENDAR_TOOLKIT = "googlecalendar"
MEETING_MINUTES = 60          # the collector stamps a start and no end
COMPLETED_WINDOW = timedelta(days=1)
CLOSED_WINDOW = timedelta(days=2)
_EPOCH = readers._EPOCH


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


# ── decisions ───────────────────────────────────────────────────────────────


def decisions(doc: dict, *, me=None, rows=None) -> list[dict]:
    return [it for it in attention.derive(doc, me=me, rows=rows) if not store.is_dismissed(doc, it["key"], it["since"])]


# ── calendar ────────────────────────────────────────────────────────────────


def meeting(entry: dict) -> dict:
    """A calendar entry as the page shows it: ``starts`` and ``ends``."""
    starts = parse_ts(entry["ts"]) or _EPOCH
    return {**entry, "starts": entry["ts"], "ends": iso(starts + timedelta(minutes=MEETING_MINUTES))}


def meetings(doc: dict, *, now: Optional[datetime] = None, horizon_days: int = 1) -> list[dict]:
    """Meetings from now to the end of tomorrow, dismissable, soonest first."""
    moment = _now(now)
    end = (moment + timedelta(days=horizon_days)).replace(hour=23, minute=59, second=59, microsecond=0)
    if CALENDAR_TOOLKIT not in connections_store.list_configured():
        return []
    out = []
    for entry in readers.connections(doc, limit=readers.CONNECTIONS_FETCH, toolkits=[CALENDAR_TOOLKIT]):
        m = meeting(entry)
        starts, ends = parse_ts(m["starts"]), parse_ts(m["ends"])
        if starts is None or ends is None or ends <= moment or starts > end:
            continue
        if store.is_dismissed(doc, m["key"], m["starts"]):
            continue
        out.append(m)
    out.sort(key=lambda m: parse_ts(m["starts"]) or _EPOCH)
    return out


# ── completed ───────────────────────────────────────────────────────────────


def _row(*, key, kind, ts, title, detail, tone, project_id=None, pid=None, actor=None, ref=None, primary="") -> dict:
    return {"key": key, "kind": kind, "ts": ts, "title": readers._one_line(title, store.TITLE_MAX),
            "detail": readers._one_line(detail, 600), "tone": tone,
            "project_id": project_id if store.is_project_id(project_id) else None,
            "pid": pid if store.is_pid(pid) else None, "actor": actor, "ref": ref or {}, "primary": primary}


def job_rows(doc: dict, *, now: Optional[datetime] = None) -> list[dict]:
    """One row per job that ran in the last day, however often it ran."""
    floor = _now(now) - COMPLETED_WINDOW
    by_job: dict[str, dict] = {}
    for entry in readers.jobs(doc, limit=readers.RUNS_FETCH * 4):
        ts = parse_ts(entry["ts"])
        if ts is None or ts < floor:
            continue
        job = entry["ref"]["job"]
        g = by_job.setdefault(job["id"], {"job": job, "runs": [], "failed": 0, "latest": entry, "project_id": entry["project_id"],
                                          "pid": entry["pid"]})
        g["runs"].append(entry)
        if entry["kind"] == "job.failed":
            g["failed"] += 1
        if (parse_ts(entry["ts"]) or _EPOCH) > (parse_ts(g["latest"]["ts"]) or _EPOCH):
            g["latest"] = entry
    out = []
    for g in by_job.values():
        latest, n = g["latest"], len(g["runs"])
        day = (parse_ts(latest["ts"]) or _EPOCH).strftime("%Y-%m-%d")
        title = g["job"]["name"] or g["job"]["id"]
        if n == 1:
            title += " finished" if latest["kind"] == "job.finished" else " failed"
            detail = latest["detail"]
        else:
            title += f" · {n} runs"
            detail = (f"{g['failed']} failed" if g["failed"] else "all ok") + (" · " + latest["detail"] if latest["detail"] else "")
        out.append(_row(key=f"job:runs:{g['job']['id']}:{day}", kind="job", ts=latest["ts"], title=title, detail=detail,
                        tone="error" if g["failed"] else "closed", project_id=g["project_id"], pid=g["pid"],
                        ref={"job": g["job"], "runs": n, "failed": g["failed"], "latest": latest["key"]}, primary="output"))
    return out


def closed_rows(rows: list[dict], *, now: Optional[datetime] = None) -> list[dict]:
    """Work items closed in the last two days."""
    floor = _now(now) - CLOSED_WINDOW
    out = []
    for row in rows:
        if row.get("status") != "closed" or row.get("deleted_at"):
            continue
        ts = parse_ts(row.get("updated_at"))
        if ts is None or ts < floor:
            continue
        project_id, wid = row.get("_project_id"), row.get("id")
        if not store.is_project_id(project_id) or not isinstance(wid, str):
            continue
        github = (row.get("source") or {}).get("github") if isinstance(row.get("source"), dict) else None
        reason = row.get("state_reason")
        detail = "closed" + (" as not planned" if reason == "not_planned" else "")
        if isinstance(github, dict):
            detail += f" · {github.get('repo')} #{github.get('number')}"
        ref = {"workitem_id": wid, "project_id": project_id}
        if isinstance(github, dict):
            ref["issue"] = {"repo": github.get("repo"), "number": github.get("number"), "url": github.get("url")}
        out.append(_row(key=f"workitem:closed:{project_id}:{wid}:{row.get('updated_at')}", kind="workitem", ts=row["updated_at"],
                        title=row.get("title") or wid, detail=detail, tone="closed", project_id=project_id, pid=row.get("_pid"),
                        actor={"runtime": row.get("assignee"), "session_id": None} if row.get("assignee") else None,
                        ref=ref, primary="accept"))
    return out


def done_todo_rows(doc: dict, *, now: Optional[datetime] = None) -> list[dict]:
    """Todos agents completed in the last day, from the Space timeline."""
    floor = _now(now) - COMPLETED_WINDOW
    out = []
    for entry in readers.timeline(doc, limit=readers.TIMELINE_FETCH, types=frozenset({"todo.completed"})):
        ts = parse_ts(entry["ts"])
        if ts is None or ts < floor:
            continue
        out.append(_row(key=entry["key"], kind="todo", ts=entry["ts"], title=entry["title"].replace("Todo completed: ", "", 1),
                        detail="todo done", tone="closed", project_id=entry["project_id"], pid=entry["pid"],
                        actor=entry["actor"], ref=entry["ref"], primary=""))
    return out


def item_rows(rows=None) -> list[dict]:
    """Inbox work items a session handled or found merely worth a glance
    (section 18): closed by their outcome, until the person acknowledges
    them."""
    out = []
    for row in (inbox_view.workitem_rows() if rows is None else rows):
        outcome = row.get("outcome") or {}
        if row["state"] != "closed" or outcome.get("kind") not in items.CLOSING_OUTCOMES:
            continue
        session = row.get("session") or {}
        acted = outcome.get("acted") or []
        out.append(_row(key=f"{row['key']}:{outcome.get('at')}", kind="item", ts=outcome.get("at") or row.get("updated_at"),
                        title=row["title"],
                        detail=(outcome.get("summary") or "") + (" · " + "; ".join(acted) if acted else ""),
                        tone="closed", project_id=row["project_id"], pid=row.get("pid"),
                        actor={"runtime": session.get("runtime"), "session_id": session.get("native_session_id")} if session else None,
                        ref={"section": row["section"], "entity": row.get("entity"), "workitem_id": row["id"],
                             "project_id": row["project_id"], "session_id": session.get("session_id"),
                             "native_session_id": session.get("native_session_id"), "outcome": outcome.get("kind")},
                        primary=""))
    return out


def running_items(running=()) -> list[dict]:
    """Work items whose session runs now (for the Live row and the header)."""
    out = []
    for row in inbox_view.workitem_rows(running):
        if row["state"] != "running":
            continue
        session = row.get("session") or {}
        claim = row.get("claim") or {}
        out.append({"key": row["key"], "section": row["section"], "entity": row.get("entity"), "workitem_id": row["id"],
                    "title": row["title"], "status": row["status"], "session_id": session.get("session_id") or claim.get("session_id"),
                    "runtime": session.get("runtime") or claim.get("runtime"),
                    "started_at": session.get("resumed_at") or session.get("started_at") or claim.get("started_at"),
                    "project_id": row["project_id"]})
    out.sort(key=lambda r: parse_ts(r.get("started_at")) or _EPOCH, reverse=True)
    return out


def sections_summary(rows=None) -> list[dict]:
    """One row per section: the policy's mode and the counts."""
    rows = inbox_view.workitem_rows() if rows is None else rows
    out = []
    for entry in inbox_view.sections_of(rows, with_entities=False):
        policy = items.read_policy(entry["id"])
        out.append({"section": entry["id"], "label": entry["label"], "mode": policy["sessions"]["mode"],
                    "act": policy["sessions"]["act"], "kinds": list(policy["sessions"]["kinds"]), "counts": entry["counts"],
                    "waiting": entry["counts"].get("waiting", 0) + entry["counts"].get("new", 0) + entry["counts"].get("failed", 0),
                    "project_id": facts.project_id_for(entry["id"])})
    return out


def completed(doc: dict, *, now: Optional[datetime] = None, rows=None, item_rows_source=None) -> list[dict]:
    rows = attention.rollup_rows() if rows is None else rows
    out: list[dict] = []
    for name, fn in (("jobs", lambda: job_rows(doc, now=now)),
                     ("workitems", lambda: closed_rows(rows, now=now)),
                     ("todos", lambda: done_todo_rows(doc, now=now)),
                     ("items", lambda: item_rows(item_rows_source))):
        try:
            out.extend(fn())
        except Exception:  # noqa: BLE001 - one source must not empty the group
            logger.warning("work completed: %s failed; its rows are missing this read", name, exc_info=True)
    out = [r for r in out if r["key"] not in doc["acked"]]
    out.sort(key=lambda r: parse_ts(r["ts"]) or _EPOCH, reverse=True)
    return out


# ── work ────────────────────────────────────────────────────────────────────


def workitem_view(row: dict) -> Optional[dict]:
    """One projected rollup row as the page shows it."""
    project_id, wid = row.get("_project_id"), row.get("id")
    if not store.is_project_id(project_id) or not isinstance(wid, str):
        return None
    github = (row.get("source") or {}).get("github") if isinstance(row.get("source"), dict) else None
    in_progress = bool(row.get("_in_progress"))
    claim = attention.claim_of(project_id, wid) if in_progress else None
    return {"id": wid, "title": row.get("title") or wid, "project_id": project_id, "pid": row.get("_pid"),
            "status": row.get("status") or "open", "state_reason": row.get("state_reason"),
            "origin": "github" if isinstance(github, dict) else "space",
            "issue": {"repo": github.get("repo"), "number": github.get("number"), "url": github.get("url")} if isinstance(github, dict) else None,
            "stale": bool(row.get("stale")), "assignee": row.get("assignee") or None,
            "github_assignees": list(row.get("github_assignees") or []), "in_progress": in_progress,
            "claim": {"runtime": claim.get("runtime"), "session_id": claim.get("session_id"), "started_at": claim.get("started_at")} if claim else None,
            "labels": list(row.get("labels") or []), "body": row.get("body"),
            "links": row.get("links") if isinstance(row.get("links"), dict) else {"todo_ids": [], "session_ids": []},
            "created_at": row.get("created_at"), "updated_at": row.get("updated_at"), "created_by": row.get("created_by")}


def open_work(rows=None) -> list[dict]:
    rows = attention.rollup_rows() if rows is None else rows
    out = [w for w in (workitem_view(r) for r in rows if r.get("status") == "open" and not r.get("deleted_at")) if w is not None]
    out.sort(key=lambda w: parse_ts(w["updated_at"]) or parse_ts(w["created_at"]) or _EPOCH, reverse=True)
    return out


# ── the page ────────────────────────────────────────────────────────────────


def projects() -> list[dict]:
    return [{"id": pid_name, "pid": pid} for pid_name, pid in sorted(list_project_pids().items()) if store.is_project_id(pid_name)]


def compose(doc: dict, *, me=None, now: Optional[datetime] = None, running=()) -> dict:
    """Everything the Inbox page paints, in one read. ``running`` is the set
    of ``(project_id, workitem_id)`` the runner drives in this process."""
    who = list(me) if me is not None else attention.identities()
    try:
        rows = attention.rollup_rows()
    except Exception:  # noqa: BLE001 - the projects root may be unreadable
        logger.warning("work inbox: the work item rollup failed; the page shows no work items this read", exc_info=True)
        rows = []
    try:
        item_view_rows = inbox_view.workitem_rows(running)
    except Exception:  # noqa: BLE001 - a bad sidecar must not fail the page
        logger.warning("work inbox: could not join the Inbox items", exc_info=True)
        item_view_rows = []
    groups = {"decisions": decisions(doc, me=who, rows=rows), "calendar": meetings(doc, now=now),
              "completed": completed(doc, now=now, rows=rows, item_rows_source=item_view_rows), "work": open_work(rows)}
    counts = {name: len(group) for name, group in groups.items()}
    try:
        running_now = [r for r in running_items(running)] if item_view_rows else []
        sections = sections_summary(item_view_rows)
    except Exception:  # noqa: BLE001 - a bad policy file must not fail the page
        logger.warning("work inbox: could not summarise the sections", exc_info=True)
        running_now, sections = [], []
    running = running_now
    counts["running"] = len(running)
    return {"schema": store.SCHEMA, "updated_at": doc.get("updated_at"), "generated_at": iso(_now(now)),
            "me": who, "projects": projects(), "counts": counts,
            "badge": counts["decisions"] + counts["completed"], "running": running, "sections": sections, **groups}
