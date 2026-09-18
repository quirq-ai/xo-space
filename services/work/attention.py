"""Attention: what needs a person, derived from current state on every read
(design sections 9 and 16). Nothing here is stored. Each item is a
condition with a stable ``key`` and a ``since``; when the condition clears
the item leaves on its own, and a dismissal hides exactly one ``key@since``
pair, so the same condition starting again comes back.

Reasons, in the order the Inbox lists them::

    assigned_to_me   work item open, mine, nobody on it       since updated_at
    unassigned       work item open, no owner                 since created_at
    todo_blocked     a todo is blocked in any project         since the todo's updated_at
    issue_mine       open issue assigned to me, not tracked   since the issue's updated_at
    connection       a connection event of a listed kind      since the event ts
    agent_question   an agent's post of kind question/request since the post ts
    share_pending    a repo shared with this Space, not cloned since the share
    source_error     a connection or a job whose last run failed   since the last good run
    item_new         an Inbox item with no session yet (section 17)  since the item's updated_at
    item_question    an item whose session asks the person
    item_draft       an item whose session drafted a reply
    item_task        an item whose session proposed a task
    item_failed      an item whose session failed; Retry runs it again

``commits_behind`` (design section 9) needs a git read per shared project
and is not derived here yet; the History page's sharing card carries it.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from services.connections import store as connections_store
from services.cowork_agent import coder_identity
from services.cowork_agent.project_layout import xo_dir
from services.cowork_agent.project_sharing import status as sharing_status
from services.cowork_agent.scopes import VisualizerScope, resolve_scope
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.storage.reader import read_json
from services.timestamps import parse_ts
from utils.commands import scheduler

from . import items, readers, store

logger = logging.getLogger(__name__)

REASONS = ("assigned_to_me", "unassigned", "item_question", "item_draft", "item_task", "todo_blocked", "issue_mine",
           "connection", "agent_question", "item_new", "share_pending", "source_error", "item_failed")
PRIMARY = {"assigned_to_me": "claim", "unassigned": "assign", "todo_blocked": "open_project", "issue_mine": "track",
           "connection": "track", "agent_question": "open_project", "share_pending": "clone", "source_error": "reconnect",
           "item_new": "start", "item_question": "open_session", "item_draft": "open_workbench", "item_task": "track",
           "item_failed": "retry"}
_ITEM_OUTCOME_REASON = {"needs_you": "item_question", "reply_drafted": "item_draft", "task_proposed": "item_task"}
_ORDER = {reason: i for i, reason in enumerate(REASONS)}
_QUESTION_KINDS = frozenset({"question", "request"})


def identities() -> list[str]:
    """The names this Space answers to as ``me``: the signed-in user, the
    Space id, the Coder owner. The GitHub login is added by the caller when
    it has one (it needs the network). Compared case-insensitively."""
    out: list[str] = ["me"]
    for value in (coder_identity.resolve_user_id(), coder_identity.space_id(), coder_identity.owner_name()):
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def _is_me(value, me: frozenset[str]) -> bool:
    return isinstance(value, str) and value.casefold().lstrip("@") in me


def _item(*, key, reason, title, detail="", since=None, project_id=None, pid=None, source=None, ref=None, tone="attention") -> dict:
    return {"key": key, "reason": reason, "title": readers._one_line(title, store.TITLE_MAX),
            "detail": readers._one_line(detail, 600), "since": since if isinstance(since, str) else "",
            "project_id": project_id if store.is_project_id(project_id) else None,
            "pid": pid if store.is_pid(pid) else None, "source": source, "ref": ref or {}, "tone": tone,
            "primary": PRIMARY[reason]}


# ── each reason ─────────────────────────────────────────────────────────────


def workitem_items(rows: Iterable[dict], me: frozenset[str]) -> list[dict]:
    """``assigned_to_me`` and ``unassigned`` over the projected rollup rows."""
    out = []
    for row in rows:
        if row.get("status") != "open" or row.get("deleted_at"):
            continue
        project_id, wid = row.get("_project_id"), row.get("id")
        if not store.is_project_id(project_id) or not isinstance(wid, str):
            continue
        assignee = row.get("assignee")
        github = (row.get("source") or {}).get("github") if isinstance(row.get("source"), dict) else None
        ref = {"workitem_id": wid, "project_id": project_id}
        if isinstance(github, dict):
            ref["issue"] = {"repo": github.get("repo"), "number": github.get("number"), "url": github.get("url")}
        by = row.get("created_by")
        if not assignee:
            out.append(_item(key=f"workitem:{project_id}:{wid}", reason="unassigned", title=row.get("title") or wid,
                             detail=("Opened by " + str(by) + ". " if by else "") + "Give it an owner, you or an agent, or it stays here.",
                             since=row.get("created_at"), project_id=project_id, pid=row.get("_pid"), ref=ref))
        elif _is_me(assignee, me) and not row.get("_in_progress"):
            out.append(_item(key=f"workitem:{project_id}:{wid}", reason="assigned_to_me", title=row.get("title") or wid,
                             detail="Assigned to you" + (" by " + str(by) if by and not _is_me(by, me) else "")
                             + ". Nobody is on it yet; Claim opens the project so a session can start.",
                             since=row.get("updated_at") or row.get("created_at"), project_id=project_id, pid=row.get("_pid"), ref=ref))
    return out


def blocked_todos(projects: dict[str, Optional[str]]) -> list[dict]:
    out = []
    for project_id, pid in sorted(projects.items()):
        if not store.is_project_id(project_id):
            continue
        doc = read_json(xo_dir(project_id) / "todos.json")
        sessions = doc.get("sessions") if isinstance(doc, dict) else None
        if not isinstance(sessions, dict):
            continue
        for session_key, entry in sessions.items():
            todos = entry.get("todos") if isinstance(entry, dict) else None
            runtime = entry.get("runtime") if isinstance(entry, dict) else None
            for t in todos if isinstance(todos, list) else []:
                if not isinstance(t, dict) or t.get("status") != "blocked" or t.get("deleted_at") \
                        or not isinstance(t.get("id"), str) or not t["id"]:
                    continue
                who = f"{runtime} marked this todo blocked" if isinstance(runtime, str) and runtime else "Marked blocked"
                desc = t.get("description") if isinstance(t.get("description"), str) else ""
                out.append(_item(key=f"todo:{project_id}:{t['id']}", reason="todo_blocked", title=t.get("content") or t["id"],
                                 detail=who + (": " + desc if desc else "."),
                                 since=t.get("updated_at") or t.get("created_at"), project_id=project_id, pid=pid,
                                 ref={"todo_id": t["id"], "session_id": session_key, "runtime": runtime}))
    return out


def my_issues(projects: dict[str, Optional[str]], me: frozenset[str], tracked_node_ids: frozenset[str]) -> list[dict]:
    out = []
    for project_id, pid in sorted(projects.items()):
        if not store.is_project_id(project_id):
            continue
        for row in readers.mirror_rows(project_id):
            if row.get("state") != "open" or row.get("node_id") in tracked_node_ids:
                continue
            entry = readers.issue_entry(project_id, pid, row)
            if entry is None or not any(_is_me(lg, me) for lg in entry["ref"].get("assignees") or []):
                continue
            out.append(_item(key=entry["key"], reason="issue_mine", title=entry["title"],
                             detail="Assigned to you on GitHub and not tracked here. Track adopts it as a work item.",
                             since=entry["ts"], project_id=project_id, pid=pid, source="github", ref=entry["ref"]))
    return out


def _item_collectors() -> dict[str, frozenset[str]]:
    """Per connection, the collectors whose events are items (section 17):
    those never surface as raw ``connection`` decisions too."""
    out = {}
    for toolkit in items.list_connections():
        policy = items.read_policy(toolkit)
        if policy:
            out[toolkit] = frozenset(c for c, on in policy["items"].items() if on)
    return out


def connection_items(doc: dict, promoted: dict) -> list[dict]:
    kinds = frozenset(doc["sources"]["connections"].get("attention") or [])
    if not kinds:
        return []
    as_items = _item_collectors()
    out = []
    for entry in readers.connections(doc, limit=readers.CONNECTIONS_FETCH):
        if entry["kind"] not in kinds or entry["key"] in promoted:
            continue
        if entry["ref"].get("collector") in as_items.get(entry["toolkit"], frozenset()):
            continue
        out.append(_item(key=entry["key"], reason="connection", title=entry["title"], detail=entry["detail"],
                         since=entry["ts"], source=entry["toolkit"], ref=entry["ref"]))
    return out


def question_items(doc: dict, promoted: dict, acked: dict) -> list[dict]:
    out = []
    for post in doc["posts"] + readers.legacy_posts():
        if post.get("kind") not in _QUESTION_KINDS or post.get("legacy_status") == "done":
            continue
        entry = readers.post_entry(post)
        if entry["key"] in promoted or entry["key"] in acked:
            continue
        out.append(_item(key=entry["key"], reason="agent_question", title=entry["title"], detail=entry["detail"],
                         since=entry["ts"], project_id=entry["project_id"], pid=entry["pid"], source=post.get("source") or "api",
                         ref={**entry["ref"], "runtime": (entry["actor"] or {}).get("runtime")}))
    return out


def share_items() -> list[dict]:
    snap = sharing_status.snapshot()
    repos = snap.get("repos") if isinstance(snap.get("repos"), dict) else {}
    recent = [e for e in (snap.get("recent") or []) if isinstance(e, dict)]
    out = []
    for repo, r in sorted(repos.items()):
        if not isinstance(r, dict) or not r.get("shared") or r.get("project"):
            continue
        clone = r.get("clone") if isinstance(r.get("clone"), dict) else None
        if clone and clone.get("state") in ("cloning", "cloned"):
            continue
        shared_at = next((e.get("at") for e in reversed(recent) if e.get("repo") == repo and e.get("kind") == "shared_with_you"), None)
        detail = "Shared with this Space. Clone it into the projects root to start receiving its commits."
        if clone and clone.get("detail"):
            detail = f"Clone {clone.get('state')}: {clone['detail']}"
        out.append(_item(key=f"share:{repo}", reason="share_pending", title=f"{repo} shared with this Space", detail=detail,
                         since=shared_at or (clone or {}).get("at") or r.get("last_fetch_at"), source="sharing",
                         ref={"repo": repo, "clone": clone}))
    return out


def source_errors() -> list[dict]:
    out = []
    for toolkit in connections_store.list_configured():
        try:
            config = connections_store.read_config(toolkit)
            state = connections_store.read_state(toolkit)
        except Exception as exc:  # noqa: BLE001 - one bad connection folder must not hide the rest
            logger.warning("work attention: could not read %s: %s", toolkit, exc)
            continue
        if not config or not config.get("enabled") or not state.get("last_error"):
            continue
        out.append(_item(key=f"source:{toolkit}", reason="source_error", title=f"{toolkit}: {state['last_error']}",
                         detail="Every poll since the last good one failed. Reconnect or check it from Setup.",
                         since=state.get("last_ok_at") or state.get("last_poll_at"), source=toolkit,
                         ref={"toolkit": toolkit, "view": "setup/connectors"}, tone="error"))
    try:
        jobs = scheduler.list_jobs()
    except Exception as exc:  # noqa: BLE001 - the scheduler folder may be absent
        logger.warning("work attention: could not list jobs: %s", exc)
        jobs = []
    for job in jobs:
        last = job.get("last_result") if isinstance(job, dict) else None
        if not isinstance(last, dict) or last.get("status") in (None, "ok") or not job.get("enabled", True):
            continue
        tail = [ln for ln in str(last.get("output_tail") or "").splitlines() if ln.strip()]
        out.append(_item(key=f"source:job:{job['id']}", reason="source_error", title=f"{job.get('name') or job['id']}: last run {last.get('status')}",
                         detail=(tail[-1].strip() if tail else "The last run did not finish cleanly.") + " Open Live to see it.",
                         since=last.get("finished_at") or last.get("started_at"), source="scheduler",
                         ref={"job": {"id": job["id"], "name": job.get("name")}, "view": "work/live"}, tone="error"))
    return out


def item_items(doc: dict) -> list[dict]:
    """The Inbox items that need a person (section 17.3): new ones with no
    session, and done ones whose session asked, drafted or proposed, and
    failed ones. Decided items never surface."""
    out = []
    for toolkit in items.list_connections():
        policy = items.read_policy(toolkit) or items.normalize_policy({})
        mode = policy["sessions"]["mode"]
        for item_id, s in items.read_index(toolkit)["items"].items():
            if s.get("decided"):
                continue
            status = s.get("status")
            if status == "new":
                reason = "item_new"
            elif status == "failed":
                reason = "item_failed"
            elif status == "done":
                reason = _ITEM_OUTCOME_REASON.get(s.get("outcome"))
                if reason is None:
                    continue
            else:
                continue
            record = items.read_item(toolkit, item_id)
            if record is None:
                continue
            outcome = items.read_outcome(toolkit, item_id) if status == "done" else None
            session = items.read_session(toolkit, item_id) if status in ("done", "failed") else None
            if reason == "item_new":
                detail = f"Arrived from {toolkit} ({record.get('collector')}). " + (
                    "Start a session to handle it, or Track it as work." if mode != "off"
                    else "Sessions are off for this connection; Track it as work, or dismiss it.")
            elif reason == "item_question":
                detail = (outcome or {}).get("question") or (outcome or {}).get("summary") or "The session has a question."
            elif reason == "item_draft":
                detail = ((outcome or {}).get("summary") or "A reply is drafted.") + (
                    f" Draft: {outcome['draft']}." if (outcome or {}).get("draft") else "")
            elif reason == "item_task":
                task = (outcome or {}).get("task") or {}
                detail = ((outcome or {}).get("summary") or "") + (f" Proposed: {task.get('title')}." if task.get("title") else "")
            else:
                exit_ = (session or {}).get("exit") or {}
                detail = f"The session {exit_.get('status') or 'failed'}: {exit_.get('message') or 'no detail'}. Retry runs it again."
            out.append(_item(key=items.item_key(toolkit, item_id), reason=reason, title=record.get("title") or item_id, detail=detail,
                             since=s.get("updated_at") or record.get("updated_at"), source=toolkit,
                             ref={"toolkit": toolkit, "item_id": item_id, "collector": record.get("collector"), "url": record.get("url"),
                                  "session_id": record.get("session_id"), "native_session_id": (session or {}).get("native_session_id"),
                                  "project_id": items.project_id_for(toolkit), "workbench": f"items/{item_id}", "outcome": outcome,
                                  "task": (outcome or {}).get("task"), "draft": (outcome or {}).get("draft"),
                                  "can_send": bool(policy["sessions"]["act"] and (outcome or {}).get("kind") == "reply_drafted"),
                                  "mode": mode},
                             tone="error" if reason == "item_failed" else "attention"))
    return out


# ── the derivation ──────────────────────────────────────────────────────────


def _tracked_node_ids(rows: Iterable[dict]) -> frozenset[str]:
    found = set()
    for row in rows:
        source = row.get("source") if isinstance(row, dict) else None
        github = source.get("github") if isinstance(source, dict) else None
        if isinstance(github, dict) and isinstance(github.get("node_id"), str) and not row.get("deleted_at"):
            found.add(github["node_id"])
    return frozenset(found)


def rollup_rows(status: Optional[str] = None) -> list[dict]:
    """Every project's work items, projected against the mirror, with
    ``_project_id``, ``_pid`` and ``_in_progress``."""
    return list(resolve_scope("xo-workspace-visualizer").rollup_workitems(status=status).rows)


def claim_of(project_id: str, workitem_id: str) -> Optional[dict]:
    """Who is on a work item now: ``{runtime, session_id, started_at}`` or ``None``."""
    try:
        claim = VisualizerScope(project_id).read_claims().get(workitem_id)
    except Exception:  # noqa: BLE001 - a project without a runtime home has no claims
        return None
    return dict(claim) if isinstance(claim, dict) else None


def derive(doc: dict, *, me: Optional[Iterable[str]] = None, rows: Optional[list[dict]] = None) -> list[dict]:
    """Every attention item, in reason order then newest ``since`` first.
    Dismissals are not applied here; the service does that so the badge and
    the page agree on one rule."""
    who = frozenset(v.casefold() for v in (me if me is not None else identities()))
    rows = rollup_rows() if rows is None else rows
    projects = list_project_pids()
    items: list[dict] = []
    for name, fn in (("workitems", lambda: workitem_items(rows, who)),
                     ("todos", lambda: blocked_todos(projects)),
                     ("issues", lambda: my_issues(projects, who, _tracked_node_ids(rows))),
                     ("connections", lambda: connection_items(doc, doc["promoted"])),
                     ("posts", lambda: question_items(doc, doc["promoted"], doc["acked"])),
                     ("items", lambda: item_items(doc)),
                     ("sharing", share_items),
                     ("sources", source_errors)):
        try:
            items.extend(fn())
        except Exception:  # noqa: BLE001 - one source's failure must not empty the strip
            logger.warning("work attention: %s failed; its items are missing this read", name, exc_info=True)
    items.sort(key=lambda it: (_ORDER[it["reason"]], -(parse_ts(it["since"]) or readers._EPOCH).timestamp()))
    return items
