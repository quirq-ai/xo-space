"""Readers: one function per source, each answering entries newest-first
straight from that source's own file (design section 8). Nothing is copied
and nothing is written; a reader that fails raises, and the service turns
that into ``sources[name].error`` on the page rather than a failed page.

One entry shape for every source (design section 10)::

    {"key", "ts", "source", "kind", "title", "detail", "project_id", "pid",
     "actor": {"runtime", "session_id"} | None, "ref": {...}, "tone",
     "toolkit"?: for a connection event}

Keys are stable and built from the source's own identifiers, so a page can
dedup, dismiss and promote by them across reads.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from services.connections import store as connections_store
from services.cowork_agent.project_layout import xo_dir
from services.cowork_agent.project_sharing import status as sharing_status
from services.cowork_agent.scopes import resolve_scope
from services.cowork_agent.visualizer import github_mirror
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.storage.reader import read_json
from services.timestamps import parse_ts
from utils.commands import scheduler

from . import store

logger = logging.getLogger(__name__)

READER_NAMES = ("timeline", "issues", "connections", "sharing", "jobs", "posts")
TIMELINE_FETCH = 500
CONNECTIONS_FETCH = 200
RUNS_FETCH = 50
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SAFE = re.compile(r"[^a-z0-9_.:-]")

_WORKITEM_TITLES = {
    "created": "Work item created: {title}", "adopted": "Issue tracked: {title}",
    "assigned": "Assigned: {title}", "claimed": "Work started: {title}", "released": "Work released: {title}",
    "closed": "Closed: {title}", "reopened": "Reopened: {title}", "deleted": "Deleted: {title}",
}
_SHARING_TITLES = {
    "shared_with_you": "{repo} shared with this Space",
    "fetched": "New commits fetched on {repo}",
    "error": "Sharing failed for {repo}",
    "revoked": "Sharing access revoked for {repo}",
    "cloned": "{repo} cloned",
    "clone_failed": "Clone failed for {repo}",
}


def _entry(*, key, ts, source, kind, title, detail="", project_id=None, pid=None, actor=None, ref=None,
           tone="info", **extra) -> dict:
    out = {"key": key, "ts": ts, "source": source, "kind": kind, "title": _one_line(title, store.TITLE_MAX),
           "detail": _one_line(detail, 600), "project_id": project_id if store.is_project_id(project_id) else None,
           "pid": pid if store.is_pid(pid) else None, "actor": actor, "ref": ref or {}, "tone": tone}
    out.update(extra)
    return out


def _one_line(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit]


def _keep(ts, before: Optional[str]) -> bool:
    """The cursor rule every reader shares: a parsable ``ts`` strictly
    older than ``before`` (or any parsable ``ts`` with no cursor)."""
    dt = parse_ts(ts)
    if dt is None:
        return False
    if before is None:
        return True
    cut = parse_ts(before)
    return cut is None or dt < cut


def _sort_key(entry: dict):
    return parse_ts(entry["ts"]) or _EPOCH


def _actor(line: dict) -> Optional[dict]:
    runtime = line.get("runtime") if isinstance(line.get("runtime"), str) and line.get("runtime") else None
    session = line.get("session_id") if isinstance(line.get("session_id"), str) and line.get("session_id") else None
    if runtime is None and session is None:
        return None
    return {"runtime": runtime, "session_id": session}


# ── per-call lookups of the records the timeline names by id ────────────────


class Names:
    """Titles for the ids a timeline line carries (todo content, work item
    title), read once per project per call. The timeline is the log; the
    record is where the words live."""

    def __init__(self) -> None:
        self._todos: dict[str, dict[str, str]] = {}
        self._workitems: dict[str, dict[str, str]] = {}

    def todo(self, project_id: str, todo_id: str) -> Optional[str]:
        if project_id not in self._todos:
            names: dict[str, str] = {}
            doc = read_json(xo_dir(project_id) / "todos.json") if store.is_project_id(project_id) else None
            sessions = doc.get("sessions") if isinstance(doc, dict) else None
            for entry in (sessions.values() if isinstance(sessions, dict) else []):
                todos = entry.get("todos") if isinstance(entry, dict) else None
                for t in todos if isinstance(todos, list) else []:
                    if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("content"), str):
                        names[t["id"]] = t["content"]
            self._todos[project_id] = names
        return self._todos[project_id].get(todo_id)

    def workitem(self, project_id: str, workitem_id: str) -> Optional[str]:
        if project_id not in self._workitems:
            names = {}
            doc = read_json(xo_dir(project_id) / "workitems.json") if store.is_project_id(project_id) else None
            items = doc.get("items") if isinstance(doc, dict) else None
            for wid, rec in (items.items() if isinstance(items, dict) else []):
                if isinstance(rec, dict) and isinstance(rec.get("title"), str):
                    names[str(wid)] = rec["title"]
            self._workitems[project_id] = names
        return self._workitems[project_id].get(workitem_id)


# ── timeline ────────────────────────────────────────────────────────────────


def timeline_entry(line: dict, names: Optional[Names] = None) -> Optional[dict]:
    """One Space timeline line as an entry, or ``None`` for a line the Work
    does not show (no type, no project, unparsable ts)."""
    if not isinstance(line, dict):
        return None
    kind, project_id, ts = line.get("type"), line.get("project_id"), line.get("ts")
    if not isinstance(kind, str) or not kind or not store.is_project_id(project_id) or parse_ts(ts) is None:
        return None
    names = names or Names()
    actor = _actor(line)
    sid = actor["session_id"] if actor and actor["session_id"] else ""
    base = dict(ts=ts, source="timeline", kind=kind, project_id=project_id, pid=line.get("pid"), actor=actor)
    if kind == "session.started":
        return _entry(key=f"timeline:{kind}:{project_id}:{sid or ts}", title=f"Session started in {project_id}",
                      ref={"session_id": sid} if sid else {}, **base)
    if kind == "todo.added":
        todo = line.get("todo") if isinstance(line.get("todo"), dict) else {}
        tid = todo.get("id") if isinstance(todo.get("id"), str) else ""
        return _entry(key=f"timeline:{kind}:{project_id}:{sid}:{tid or ts}",
                      title=f"Todo added: {todo.get('content') or tid}", ref={"todo_id": tid} if tid else {}, **base)
    if kind in ("todo.completed", "todo.status_changed"):
        tid = line.get("todo_id") if isinstance(line.get("todo_id"), str) else ""
        content = names.todo(project_id, tid) if tid else None
        status = "completed" if kind == "todo.completed" else str(line.get("status") or "changed")
        return _entry(key=f"timeline:{kind}:{project_id}:{sid}:{tid or ts}:{status}",
                      title=f"Todo {status}: {content or tid}", detail=status,
                      tone="attention" if status == "blocked" else "info",
                      ref={"todo_id": tid, "status": status} if tid else {"status": status}, **base)
    if kind in ("file.created", "file.edited"):
        path = line.get("path") if isinstance(line.get("path"), str) else ""
        return _entry(key=f"timeline:{kind}:{project_id}:{sid}:{path}:{ts}", title=path or kind,
                      ref={"path": path} if store.is_link_path(path) else {}, **base)
    if kind.startswith("workitem."):
        wid = line.get("workitem_id") if isinstance(line.get("workitem_id"), str) else ""
        action = kind[len("workitem."):]
        title = line.get("title") if isinstance(line.get("title"), str) and line.get("title") \
            else (names.workitem(project_id, wid) if wid else None) or wid or "work item"
        detail = ""
        if action == "assigned":
            detail = f"assigned to {line.get('assignee')}" if line.get("assignee") else "unassigned"
        elif action == "closed":
            detail = str(line.get("state_reason") or "completed")
        issue = line.get("issue") if isinstance(line.get("issue"), dict) else None
        ref = {"workitem_id": wid}
        if issue and isinstance(issue.get("repo"), str) and isinstance(issue.get("number"), int):
            ref["issue"] = {"repo": issue["repo"], "number": issue["number"],
                            "url": f"https://github.com/{issue['repo']}/issues/{issue['number']}"}
        return _entry(key=f"timeline:{kind}:{project_id}:{wid}:{ts}",
                      title=_WORKITEM_TITLES.get(action, "{title}").format(title=title), detail=detail, ref=ref, **base)
    if kind == "project.created":
        return _entry(key=f"timeline:{kind}:{project_id}", title=f"Project created: {project_id}", **base)
    return _entry(key=f"timeline:{kind}:{project_id}:{sid}:{ts}", title=f"{kind} in {project_id}", **base)


def timeline(doc: dict, *, limit: int, before: Optional[str] = None, types: Optional[frozenset[str]] = None) -> list[dict]:
    """The Space timeline (``~/.quirq/projects/timeline.jsonl``), newest first."""
    events = resolve_scope("xo-workspace-visualizer").read_timeline(
        limit=min(max(limit, 1), TIMELINE_FETCH), before=before, types=types)
    names = Names()
    # ``before`` is applied once more by parsed time: the tail reader compares
    # strings, and the log mixes ``.000Z`` and ``Z`` stamps.
    out = [e for e in (timeline_entry(line, names) for line in events) if e is not None and _keep(e["ts"], before)]
    return out[:limit]


# ── issues ──────────────────────────────────────────────────────────────────


def issue_entry(project_id: str, pid: Optional[str], row: dict) -> Optional[dict]:
    if not isinstance(row, dict) or not isinstance(row.get("number"), int) or isinstance(row.get("number"), bool) \
            or not isinstance(row.get("title"), str) or parse_ts(row.get("updated_at")) is None:
        return None
    state = row.get("state") if row.get("state") in ("open", "closed") else "open"
    labels = [lb for lb in row.get("labels") or [] if isinstance(lb, str) and lb] if isinstance(row.get("labels"), list) else []
    people = row.get("assignees") if isinstance(row.get("assignees"), list) else []
    logins = [p["login"] if isinstance(p, dict) else p for p in people]
    logins = [lg for lg in logins if isinstance(lg, str) and lg]
    parts = []
    if labels:
        parts.append("labels: " + ", ".join(labels))
    if logins:
        parts.append("assignees: " + ", ".join(logins))
    if state == "closed" and row.get("state_reason"):
        parts.insert(0, str(row["state_reason"]).replace("_", " "))
    repo = row.get("repo") if isinstance(row.get("repo"), str) else None
    url = row.get("url") if store.is_url(row.get("url")) else None
    if repo is None and url and url.startswith("https://github.com/"):
        bits = url[len("https://github.com/"):].split("/")
        if len(bits) >= 2:
            repo = bits[0] + "/" + bits[1]
    return _entry(key=f"issue:{project_id}:{row['number']}", ts=row["updated_at"], source="issues", kind=f"issue.{state}",
                  title=f"#{row['number']} {row['title']}", detail=" · ".join(parts), project_id=project_id, pid=pid,
                  ref={"issue": {"repo": repo, "number": row["number"], "url": url, "node_id": row.get("node_id")},
                       "url": url, "assignees": logins},
                  tone="info")


def mirror_rows(project_id: str) -> list[dict]:
    """A project's mirrored issues, each row with ``repo`` filled in."""
    doc = github_mirror.read_mirror(project_id)
    if not isinstance(doc, dict):
        return []
    issues = doc.get("issues")
    repo = doc.get("repo") if isinstance(doc.get("repo"), str) else None
    rows = []
    for row in (issues.values() if isinstance(issues, dict) else []):
        if isinstance(row, dict):
            rows.append({**row, "repo": row.get("repo") or repo})
    return rows


def issues(doc: dict, *, limit: int, before: Optional[str] = None) -> list[dict]:
    """Every project's GitHub issue mirror, newest ``updated_at`` first."""
    out = []
    for project_id, pid in sorted(list_project_pids().items()):
        if not store.is_project_id(project_id):
            continue
        for row in mirror_rows(project_id):
            if not _keep(row.get("updated_at"), before):
                continue
            entry = issue_entry(project_id, pid, row)
            if entry is not None:
                out.append(entry)
    out.sort(key=_sort_key, reverse=True)
    return out[:limit]


# ── connections ─────────────────────────────────────────────────────────────


def connection_kind(toolkit: str, event_type: str) -> str:
    return _SAFE.sub("-", f"{toolkit}.{event_type}".lower())[:60]


def connection_entry(toolkit: str, ev: dict, attention_kinds=()) -> Optional[dict]:
    if not isinstance(ev, dict) or not isinstance(ev.get("type"), str) or not ev["type"] \
            or not isinstance(ev.get("key"), str) or not ev["key"] or parse_ts(ev.get("ts")) is None:
        return None
    kind = connection_kind(toolkit, ev["type"])
    title = ev.get("title") if isinstance(ev.get("title"), str) and ev.get("title") else f"{toolkit} {ev['type']}: {ev['key']}"
    url = ev.get("url") if store.is_url(ev.get("url")) else None
    return _entry(key=f"connection:{toolkit}:{ev['type']}:{ev['key']}", ts=ev["ts"], source="connections", kind=kind,
                  title=title, detail=ev.get("body") if isinstance(ev.get("body"), str) else "",
                  ref={"url": url, "toolkit": toolkit, "id": ev["key"], "collector": ev["type"]},
                  tone="attention" if kind in attention_kinds else "info", toolkit=toolkit)


def connections(doc: dict, *, limit: int, before: Optional[str] = None, toolkits=None) -> list[dict]:
    """Every configured connection's ``events.jsonl``, newest first."""
    attention_kinds = frozenset(doc["sources"]["connections"].get("attention") or [])
    out = []
    for toolkit in (toolkits if toolkits is not None else connections_store.list_configured()):
        try:
            events = connections_store.read_events(toolkit, limit=min(max(limit, 1), CONNECTIONS_FETCH))
        except Exception as exc:  # noqa: BLE001 - one toolkit's file must not hide the others
            logger.warning("work connections: could not read events for %s: %s", toolkit, exc)
            continue
        for ev in events:
            if not _keep(ev.get("ts") if isinstance(ev, dict) else None, before):
                continue
            entry = connection_entry(toolkit, ev, attention_kinds)
            if entry is not None:
                out.append(entry)
    out.sort(key=_sort_key, reverse=True)
    return out[:limit]


# ── sharing ─────────────────────────────────────────────────────────────────


def sharing(doc: dict, *, limit: int, before: Optional[str] = None) -> list[dict]:
    """The relay's ``recent`` events (in memory today; lost on restart)."""
    snap = sharing_status.snapshot()
    repos = snap.get("repos") if isinstance(snap.get("repos"), dict) else {}
    out = []
    for e in snap.get("recent") or []:
        if not isinstance(e, dict) or not _keep(e.get("at"), before):
            continue
        repo, kind = str(e.get("repo") or ""), str(e.get("kind") or "")
        if not repo or not kind:
            continue
        project = (repos.get(repo) or {}).get("project") if isinstance(repos.get(repo), dict) else None
        safe_kind = _SAFE.sub("-", kind.lower())[:50]
        out.append(_entry(key=f"sharing:{kind}:{repo}:{e['at']}", ts=e["at"], source="sharing", kind=f"sharing.{safe_kind}",
                          title=_SHARING_TITLES.get(kind, "Sharing: {kind} for {repo}").format(kind=kind, repo=repo),
                          detail=str(e.get("detail") or ""), project_id=project, pid=store.pid_for(project) if project else None,
                          ref={"repo": repo}, tone="error" if kind in ("error", "clone_failed") else "info"))
    out.sort(key=_sort_key, reverse=True)
    return out[:limit]


# ── jobs ────────────────────────────────────────────────────────────────────


def run_entry(job: dict, run: dict) -> Optional[dict]:
    if not isinstance(run, dict):
        return None
    ts = run.get("finished_at") or run.get("ts") or run.get("started_at")
    if parse_ts(ts) is None:
        return None
    status = str(run.get("status") or "")
    ok = status == "ok"
    seconds = run.get("duration_seconds")
    parts = [status or "finished"]
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        parts.append(f"{seconds:.1f} s")
    if not ok:
        rc = run.get("returncode")
        if isinstance(rc, int) and not isinstance(rc, bool):
            parts.append(f"exit {rc}")
        tail = run.get("output_tail") if isinstance(run.get("output_tail"), str) else ""
        last = [ln for ln in tail.splitlines() if ln.strip()]
        if last:
            parts.append(last[-1].strip())
    return _entry(key=f"job:run:{job['id']}:{run.get('started_at') or ts}", ts=ts, source="jobs",
                  kind="job.finished" if ok else "job.failed", title=str(job.get("name") or job["id"]),
                  detail=" · ".join(parts), project_id=job.get("project_id"),
                  pid=store.pid_for(job.get("project_id")) if job.get("project_id") else None,
                  ref={"job": {"id": job["id"], "name": job.get("name")}, "trigger": run.get("trigger"),
                       "status": status, "started_at": run.get("started_at")},
                  tone="info" if ok else "error")


def jobs(doc: dict, *, limit: int, before: Optional[str] = None) -> list[dict]:
    """Every job's run history (``scheduler/runs/<id>.jsonl``), newest first."""
    out = []
    for job in scheduler.list_jobs():
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            continue
        try:
            runs = scheduler.list_runs(job["id"], limit=min(max(limit, 1), RUNS_FETCH))
        except Exception as exc:  # noqa: BLE001 - one job's history must not hide the others
            logger.warning("work jobs: could not read runs for %s: %s", job["id"], exc)
            continue
        for run in runs:
            if not _keep((run.get("finished_at") or run.get("ts") or run.get("started_at")) if isinstance(run, dict) else None,
                         before):
                continue
            entry = run_entry(job, run)
            if entry is not None:
                out.append(entry)
    out.sort(key=_sort_key, reverse=True)
    return out[:limit]


# ── posts ───────────────────────────────────────────────────────────────────


def post_entry(post: dict) -> dict:
    source = post.get("source") or "api"
    kind = post.get("kind") or "note"
    return _entry(key=f"post:{post['id']}", ts=post["ts"], source="posts", kind=f"agent.{kind}", title=post["title"],
                  detail=post.get("body") or "", project_id=post.get("project_id"), pid=post.get("pid"),
                  actor={"runtime": source, "session_id": (post.get("ref") or {}).get("session_id")} if source != "api" else None,
                  ref={**(post.get("ref") or {}), "url": post.get("url"), "link": post.get("link"), "post_id": post["id"]},
                  tone="attention" if kind in ("question", "request") else "info")


def posts(doc: dict, *, limit: int, before: Optional[str] = None) -> list[dict]:
    """The agents' notes: ``history/history.json`` posts."""
    out = [post_entry(p) for p in doc["posts"] if _keep(p.get("ts"), before)]
    out.sort(key=_sort_key, reverse=True)
    return out[:limit]


def reader(name: str) -> Callable[..., list[dict]]:
    return globals()[name]
