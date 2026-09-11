"""Feeders: turn what arrived in the workspace into inbox items.

Each feeder takes the loaded document (for its source config and cursor)
and returns a :class:`FeedResult`. They only read; the service applies the
result inside the store lock (keyed upserts are idempotent, so applying
them to a document that changed underneath is still correct). A feeder
that raises is skipped for that run by the service; it never stops the
others. Feeders are looked up by name from :data:`FEEDER_NAMES` so tests
can patch one function on this module.

Timestamps from the three producers differ (``Z``, ``+00:00``, naive), so
every comparison goes through :func:`store.parse_ts`; the cursor is kept
as the producer's original string.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple, Optional

from services.cowork_agent.project_layout import xo_dir
from services.cowork_agent.project_sharing import status as sharing_status
from services.cowork_agent.scopes import resolve_scope
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids

from . import store

logger = logging.getLogger(__name__)

FEEDER_NAMES = ("timeline", "todos", "sharing")
TIMELINE_FETCH_LIMIT = 500
TODO_KEY_PREFIX = "todo."   # keys the todos feeder owns; only these are ever auto-closed
BOOTSTRAP_WINDOW = timedelta(hours=24)   # no cursor: only the last day, never the whole history
_SHARING_TITLES = {
    "shared_with_you": "Repo shared with this workspace: {repo}",
    "fetched": "New commits fetched: {repo}",
    "error": "Sharing error: {repo}",
    "revoked": "Sharing access revoked: {repo}",
}


class Watched(NamedTuple):
    key_prefix: str                      # open items whose key starts with this ...
    keys: frozenset[str]                 # ... and is not in here are marked done (keyless items are never touched)


class FeedResult(NamedTuple):
    items: list[dict]                    # shaped by store.build_item, no id yet
    cursor: Optional[str]                # None: leave the stored cursor alone
    watched: Optional[Watched]           # todos only: the keys still open under its prefix


def _str_list(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _one_line(text, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _safe_item(**fields) -> Optional[dict]:
    try:
        return store.build_item(**fields)
    except store.InboxError as exc:
        logger.warning("inbox feeder skipped an item: %s", exc.message)
        return None


# ── timeline ─────────────────────────────────────────────────────────────────


def _timeline_item(ev: dict) -> Optional[dict]:
    pid, kind, ts = ev.get("project_id"), ev.get("type"), ev.get("ts")
    if not store.is_project_id(pid) or not isinstance(kind, str) or not kind:
        logger.debug("inbox timeline: skipping event without project_id/type")
        return None
    sid = ev.get("session_id") if isinstance(ev.get("session_id"), str) else ""
    base = dict(kind=kind, source="timeline", project_id=pid, ts=ts)
    if kind == "session.started":
        if not sid:
            return None
        runtime = ev.get("runtime") if isinstance(ev.get("runtime"), str) and ev.get("runtime") else "unknown"
        return _safe_item(title=f"Session started in {pid} ({runtime})",
                          link={"view": "sessions"}, key=f"timeline:session.started:{sid}", **base)
    if kind == "todo.added":
        todo = ev.get("todo") if isinstance(ev.get("todo"), dict) else {}
        if not isinstance(todo.get("id"), str) or not todo["id"]:
            return None
        return _safe_item(title=f"Todo added in {pid}: {_one_line(todo.get('content'), 120)}",
                          link={"view": "projects", "project": pid},
                          key=f"timeline:todo.added:{sid}:{todo['id']}", **base)
    if kind == "todo.completed":
        tid = ev.get("todo_id")
        if not isinstance(tid, str) or not tid:
            return None
        return _safe_item(title=f"Todo completed in {pid}: {tid}",
                          key=f"timeline:todo.completed:{sid}:{tid}", **base)
    if kind in ("file.created", "file.edited"):
        path = ev.get("path")
        if not isinstance(path, str) or not path:
            return None
        verb = "created" if kind == "file.created" else "edited"
        link = {"view": "projects", "project": pid, "path": path} if store.is_link_path(path) else None
        return _safe_item(title=_one_line(f"File {verb} in {pid}: {path}", store.TITLE_MAX),
                          link=link, key=f"timeline:{kind}:{pid}:{path}", **base)
    # Any other type a user enabled: a plain, keyed notice.
    return _safe_item(title=f"{kind} in {pid}", key=f"timeline:{kind}:{pid}:{sid}:{ts}", **base)


def timeline(doc: dict) -> FeedResult:
    """Workspace ``timeline.jsonl`` events newer than the cursor (or the
    last 24 h when there is none). The cursor advances to the newest event
    fetched, kept or not."""
    types = frozenset(_str_list(store.source_config(doc, "timeline").get("types")))
    events = resolve_scope("xo-workspace-visualizer").read_timeline(limit=TIMELINE_FETCH_LIMIT, types=types)
    if len(events) >= TIMELINE_FETCH_LIMIT:
        logger.info("inbox timeline: fetch cap of %d reached; older events may be skipped", TIMELINE_FETCH_LIMIT)
    cursor = store.parse_ts(doc["cursors"].get("timeline"))
    floor = cursor or (datetime.now(timezone.utc) - BOOTSTRAP_WINDOW)
    newest: Optional[tuple[datetime, str]] = None
    kept: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        dt = store.parse_ts(ev.get("ts"))
        if dt is None:
            continue
        if newest is None or dt > newest[0]:
            newest = (dt, ev["ts"])
        if dt > floor:
            kept.append(ev)
    kept.reverse()   # read_timeline is newest-first; ingest chronologically
    items = [it for it in map(_timeline_item, kept) if it is not None]
    return FeedResult(items, newest[1] if newest else None, None)


# ── todos ────────────────────────────────────────────────────────────────────


def todos(doc: dict) -> FeedResult:
    """Every todo in a watched status across all projects and sessions. The
    watched key set lets the store close items whose todo moved on or
    vanished (project folder included).

    Todo ids are per session (a runtime may number them 1, 2, ...), so the
    same id can appear in several sessions of one project while the spec
    key carries only ``<pid>:<todo_id>``. Any watched occurrence keeps the
    key (the last one in file order supplies the text); an unwatched
    occurrence never hides a watched one, whatever the session order."""
    statuses = set(_str_list(store.source_config(doc, "todos").get("statuses")))
    watched: dict[tuple[str, str], dict] = {}
    for pid in list_project_ids():
        if not store.is_project_id(pid):
            continue
        raw = read_json(xo_dir(pid) / "todos.json")
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            continue   # missing or malformed todos.json contributes no keys
        for entry in sessions.values():
            todo_list = entry.get("todos") if isinstance(entry, dict) else None
            for t in todo_list if isinstance(todo_list, list) else []:
                if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not t["id"]:
                    continue
                if t.get("status") in statuses:
                    watched[(pid, t["id"])] = t          # last watched occurrence in file order wins
    now = store.now_iso()
    items = []
    for (pid, tid), t in watched.items():
        st = t["status"]
        items.append(_safe_item(
            title=f"Todo {st} in {pid}: {_one_line(t.get('content'), 120)}",
            body=str(t.get("description") or "")[:store.BODY_MAX], kind=f"todo.{st}", source="todos",
            project_id=pid, link={"view": "projects", "project": pid}, ts=now,
            key=f"{TODO_KEY_PREFIX}{st}:{pid}:{tid}"))
    items = [it for it in items if it is not None]
    return FeedResult(items, None, Watched(TODO_KEY_PREFIX, frozenset(it["key"] for it in items)))


# ── sharing ──────────────────────────────────────────────────────────────────


def sharing(doc: dict) -> FeedResult:
    """Relay transitions newer than the cursor. Kinds outside the four
    documented ones (for example ``cloned``, ``clone_failed``) get a generic
    title so a new kind never raises."""
    snap = sharing_status.snapshot()
    repos = snap.get("repos") if isinstance(snap.get("repos"), dict) else {}
    cursor = store.parse_ts(doc["cursors"].get("sharing"))
    newest: Optional[tuple[datetime, str]] = None
    items = []
    for e in snap.get("recent") or []:
        dt = store.parse_ts(e.get("at")) if isinstance(e, dict) else None
        if dt is None:
            continue
        if newest is None or dt > newest[0]:
            newest = (dt, e["at"])
        repo, kind = str(e.get("repo") or ""), str(e.get("kind") or "")
        if (cursor is not None and dt <= cursor) or not repo or not kind:
            continue
        project = (repos.get(repo) or {}).get("project") if isinstance(repos.get(repo), dict) else None
        title = _SHARING_TITLES.get(kind, "Sharing: {kind} for {repo}").format(kind=kind, repo=repo)
        items.append(_safe_item(
            title=_one_line(title, store.TITLE_MAX), body=str(e.get("detail") or "")[:store.BODY_MAX],
            kind="sharing." + re.sub(r"[^a-z0-9_.:-]", "-", kind.lower())[:50], source="sharing",
            project_id=project if store.is_project_id(project) else None, link={"view": "projects"},
            ts=e["at"], key=f"sharing:{kind}:{repo}:{e['at']}"))
    return FeedResult([it for it in items if it is not None], newest[1] if newest else None, None)


def feeder(name: str) -> Callable[[dict], FeedResult]:
    return globals()[name]
