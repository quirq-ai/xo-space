"""Router-facing facade for the Work. Raises :class:`WorkError`; knows
nothing about HTTP. The only work module the BFF imports.

Reads compose the page from the source logs and current state (nothing to
ingest, nothing to throttle); writes are the person's marks in
the Work files under ``~/.quirq/work/`` and, for promote, one record in a project's
``.xo/workitems.json`` through the existing work item store.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from services.connections import store as connections_store
from services.cowork_agent import coder_identity
from services.cowork_agent.scopes import VisualizerScope
from services.cowork_agent.visualizer.store_common import StoreError
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.timestamps import parse_ts

from . import attention, inbox, items, readers, runner, store
from .store import WorkError  # re-exported: the router catches service.WorkError

__all__ = ["WorkError", "READER_NAMES", "feed", "inbox_page", "attention_items", "summary", "set_watermark",
           "dismiss", "undismiss", "ack", "unack", "promote", "create_post", "set_pin",
           "inbox_connections", "set_connection_policy", "list_inbox_items", "inbox_item", "item_thread", "start_inbox_item",
           "reply_inbox_item", "decide_inbox_item", "send_inbox_item"]

logger = logging.getLogger(__name__)

READER_NAMES = readers.READER_NAMES
FEED_LIMIT_MAX = 500
PROMOTE_SEARCH = 500
_SELF = frozenset({"me", "@me", "self"})
_SOURCE_LABELS = {"post": "posts", "connection": "connections", "issue": "issues", "todo": "todos", "workitem": "workitems", "item": "inbox",
                  "timeline": "timeline", "sharing": "sharing", "share": "sharing", "job": "jobs", "source": "sources"}


def _doc() -> dict:
    doc, _ok = store.load_document()
    return doc


def _tracked(doc: dict, entry: dict) -> Optional[dict]:
    hit = doc["promoted"].get(entry["key"])
    if hit:
        return {"project_id": hit["project_id"], "workitem_id": hit["workitem_id"]}
    wid = entry.get("ref", {}).get("workitem_id")
    if isinstance(wid, str) and wid and entry.get("project_id"):
        return {"project_id": entry["project_id"], "workitem_id": wid}
    return None


# ── reads ───────────────────────────────────────────────────────────────────


def feed(*, limit: int = 100, before: Optional[str] = None, sources=None, kinds=None, project: Optional[str] = None,
         since: Optional[str] = None) -> dict:
    """The merged stream, newest first (design section 8)."""
    if not 1 <= limit <= FEED_LIMIT_MAX:
        raise WorkError("invalid_value", f"limit must be between 1 and {FEED_LIMIT_MAX}.")
    if before is not None and parse_ts(before) is None:
        raise WorkError("invalid_value", "before must be an ISO-8601 timestamp.")
    if since is not None and parse_ts(since) is None:
        raise WorkError("invalid_value", "since must be an ISO-8601 timestamp.")
    wanted = set(sources) if sources else set(READER_NAMES)
    unknown = sorted(wanted - set(READER_NAMES))
    if unknown:
        raise WorkError("invalid_value", f"unknown source(s) {unknown}; choose from {list(READER_NAMES)}.")
    if project is not None and not store.is_project_id(project):
        raise WorkError("invalid_project_id", "project must be a project folder name.")
    doc = _doc()
    status: dict[str, dict] = {}
    merged: list[dict] = []
    floor = parse_ts(since) if since else None
    for name in READER_NAMES:
        if name not in wanted:
            continue
        if not doc["sources"].get(name, {}).get("enabled", True):
            status[name] = {"ok": True, "error": None, "enabled": False}
            continue
        try:
            entries = readers.reader(name)(doc, limit=limit, before=before)
            status[name] = {"ok": True, "error": None, "enabled": True}
        except Exception as exc:  # noqa: BLE001 - a failing reader contributes nothing, never a failed page
            logger.warning("work feed: reader %s failed: %s", name, exc, exc_info=True)
            status[name] = {"ok": False, "error": str(exc)[:300], "enabled": True}
            continue
        merged.extend(entries)
    kind_set = set(kinds) if kinds else None
    pinned = set(doc["pinned"])
    out = []
    for e in merged:
        if kind_set and e["kind"] not in kind_set:
            continue
        if project and e.get("project_id") != project:
            continue
        if floor is not None and (parse_ts(e["ts"]) or readers._EPOCH) < floor:
            continue
        e["tracked"] = _tracked(doc, e)
        e["pinned"] = e["key"] in pinned
        out.append(e)
    out.sort(key=lambda e: (parse_ts(e["ts"]) or readers._EPOCH, e["key"]), reverse=True)
    page = out[:limit]
    return {"entries": page, "next_cursor": page[-1]["ts"] if len(out) > limit else None,
            "watermark": doc.get("watermark"), "sources": status}


def inbox_page(*, now: Optional[datetime] = None) -> dict:
    return inbox.compose(_doc(), now=now)


def attention_items() -> dict:
    doc = _doc()
    items = inbox.decisions(doc)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["reason"]] = counts.get(it["reason"], 0) + 1
    return {"items": items, "counts": counts, "total": len(items)}


def summary(*, now: Optional[datetime] = None) -> dict:
    """The small call the badge polls: what awaits a person and what runs."""
    page = inbox.compose(_doc(), now=now)
    return {"badge": page["badge"], "decisions": page["counts"]["decisions"], "completed": page["counts"]["completed"],
            "calendar": page["counts"]["calendar"], "work": page["counts"]["work"],
            "in_progress": sum(1 for w in page["work"] if w["in_progress"]),
            "errors": sum(1 for d in page["decisions"] if d["reason"] == "source_error"),
            "generated_at": page["generated_at"]}


# ── the person's marks ──────────────────────────────────────────────────────


def set_watermark(ts: str) -> dict:
    doc = store.modify(lambda d: store.set_watermark(d, ts))
    return {"watermark": doc["watermark"]}


def dismiss(key: str, since: Optional[str]) -> None:
    store.modify(lambda d: store.dismiss(d, key, since or ""))


def undismiss(key: str) -> None:
    store.modify(lambda d: store.undismiss(d, key))


def ack(key: str) -> None:
    store.modify(lambda d: store.ack(d, key))


def unack(key: str) -> None:
    store.modify(lambda d: store.unack(d, key))


def set_pin(key: str, pinned: bool) -> None:
    store.modify(lambda d: store.set_pin(d, key, pinned))


def create_post(*, title, body="", kind="note", source="api", project_id=None, link=None, url=None, ref=None) -> dict:
    post = store.build_post(title=title, body=body, kind=kind, source=source, project_id=project_id, link=link, url=url, ref=ref)
    stored: dict = {}

    def add(doc: dict) -> bool:
        stored.update(store.add_post(doc, post))
        return True

    store.modify(add)
    return stored


# ── promote: an entry becomes a work item ───────────────────────────────────


def _find_entry(doc: dict, key: str) -> Optional[dict]:
    """The entry behind a key: an Inbox item by its folder, else an
    attention item (the usual promote), else the newest matching feed
    entry across every reader."""
    parsed = items.parse_item_key(key)
    if parsed:
        record = items.read_item(*parsed)
        return items.item_entry(parsed[0], record, items.read_outcome(*parsed)) if record else None
    for it in attention.derive(doc):
        if it["key"] == key:
            return {**it, "ts": it["since"], "source": it.get("source") or "attention", "kind": it["reason"]}
    for name in READER_NAMES:
        try:
            for entry in readers.reader(name)(doc, limit=PROMOTE_SEARCH):
                if entry["key"] == key:
                    return entry
        except Exception:  # noqa: BLE001 - a failing reader is skipped, as in feed()
            logger.warning("work promote: reader %s failed while looking for %s", name, key, exc_info=True)
    return None


def _resolve_assignee(value: Optional[str]) -> Optional[str]:
    wanted = (value or "").strip()
    if not wanted:
        return None
    if wanted.casefold() in _SELF:
        return coder_identity.resolve_user_id()
    wanted = wanted.lstrip("@").strip()
    if not wanted or not store.ASSIGNEE_RE.fullmatch(wanted):
        raise WorkError("invalid_value", "assignee must be me, an agent name or a login.")
    return wanted


def promote(*, key: str, project_id: str, title: Optional[str] = None, assignee: Optional[str] = None) -> dict:
    """Turn the entry behind ``key`` into a work item in ``project_id``
    (design section 7.2). Idempotent on the key: a second call answers
    the same item with ``created: false``."""
    store.check_key(key)
    if not store.is_project_id(project_id) or project_id not in list_project_pids():
        raise WorkError("invalid_project_id", "project_id must name a project under the projects root.")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > store.TITLE_MAX):
        raise WorkError("invalid_value", f"title must be 1 to {store.TITLE_MAX} chars.")
    resolved_assignee = _resolve_assignee(assignee)
    doc = _doc()
    already = doc["promoted"].get(key)
    if already:
        record = None
        try:
            record = VisualizerScope(already["project_id"]).get_workitem(already["workitem_id"])
        except Exception:  # noqa: BLE001 - the project may be gone; the join still says where it went
            pass
        return {"created": False, "project_id": already["project_id"], "workitem_id": already["workitem_id"], "workitem": record}
    entry = _find_entry(doc, key)
    if entry is None:
        raise WorkError("entry_not_found", "No entry with that key is in the logs any more.", 404)
    return _promote_entry(key, entry, project_id=project_id, title=title, assignee=resolved_assignee)


def _promote_entry(key: str, entry: dict, *, project_id: str, title: Optional[str], assignee: Optional[str]) -> dict:
    """Write the work item for an entry and remember the join."""
    scope = VisualizerScope(project_id)
    resolved_assignee = assignee
    runtime = coder_identity.resolve_user_id()
    issue = entry.get("ref", {}).get("issue") if isinstance(entry.get("ref"), dict) else None
    body_parts = [entry.get("detail") or ""]
    url = entry.get("ref", {}).get("url") if isinstance(entry.get("ref"), dict) else None
    if url:
        body_parts.append(url)
    body = "\n\n".join(p for p in body_parts if p) or None
    # The label names where the entry came from, by its key prefix: the one
    # stable word every reader and the attention derivation agree on.
    source_label = _SOURCE_LABELS.get(key.split(":", 1)[0], "work")
    try:
        if isinstance(issue, dict) and issue.get("node_id") and issue.get("repo") and issue.get("number") and issue.get("url"):
            record, created = scope.adopt_workitem(
                runtime=runtime, title=title or entry["title"], labels=["work", source_label],
                github={"repo": issue["repo"], "number": issue["number"], "node_id": issue["node_id"], "url": issue["url"]})
            if resolved_assignee and record.get("assignee") != resolved_assignee:
                record = scope.update_workitem(record["id"], assignee=resolved_assignee)
        else:
            record = scope.create_workitem(runtime=runtime, title=title or entry["title"], body=body,
                                           labels=["work", source_label], assignee=resolved_assignee)
            created = True
    except StoreError as exc:
        raise WorkError(exc.code, str(exc), 409 if exc.code in ("already_adopted", "corrupt_document") else 400) from exc
    store.modify(lambda d: store.promote(d, key, project_id=project_id, workitem_id=record["id"]))
    return {"created": created, "project_id": project_id, "workitem_id": record["id"], "workitem": record}


# ── Inbox items: the connection folders and their sessions (section 17) ─────


def inbox_connections() -> dict:
    """The connection folders, and the configured connections that have none yet."""
    rows = inbox.connections_summary()
    have = {r["toolkit"] for r in rows}
    try:
        configured = [tk for tk in connections_store.list_configured() if tk not in have]
    except Exception:  # noqa: BLE001 - the connections folder may be unreadable
        configured = []
    for row in rows:
        row["policy"] = items.read_policy(row["toolkit"])
        row["running"] = len(runner.running(row["toolkit"]))
        row["cursors"] = items.read_index(row["toolkit"])["cursors"]
    return {"connections": rows, "available": configured, "runner": {"enabled": runner.enabled()}}


def set_connection_policy(toolkit: str, body) -> dict:
    items.check_toolkit(toolkit)
    if toolkit not in connections_store.list_configured():
        raise WorkError("connection_not_configured", f"{toolkit} is not a configured connection; add it under Setup first.", 404)
    return items.write_policy(toolkit, body)


def list_inbox_items(*, connection: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> dict:
    rows = items.list_items(connection, status=status, limit=limit)
    return {"items": rows, "count": len(rows)}


def inbox_item(toolkit: str, item_id: str) -> dict:
    return items.item_detail(toolkit, item_id)


def item_thread(toolkit: str, item_id: str) -> dict:
    """The item as a conversation: the fact, every turn, the outcome, and
    whether the agent is answering right now."""
    detail = items.item_detail(toolkit, item_id)
    return {"key": detail["key"], "item": detail["item"], "thread": detail["thread"], "outcome": detail["outcome"],
            "session": detail["session"], "workbench": detail["workbench"],
            "running": (toolkit, item_id) in runner.running(toolkit),
            "can_reply": bool(detail["policy"] and detail["policy"]["sessions"]["mode"] != "off"),
            "can_send": bool(detail["policy"] and detail["policy"]["sessions"]["act"]
                             and (detail["outcome"] or {}).get("kind") == "reply_drafted" and detail["item"].get("status") == "done")}


async def reply_inbox_item(toolkit: str, item_id: str, text: str) -> dict:
    return await runner.reply_item(toolkit, item_id, text)


async def start_inbox_item(toolkit: str, item_id: str, *, retry: bool = False) -> dict:
    return await runner.start_item(toolkit, item_id, retry=retry, manual=True)


async def send_inbox_item(toolkit: str, item_id: str) -> dict:
    return await runner.send_item(toolkit, item_id)


def decide_inbox_item(toolkit: str, item_id: str, action: str, *, project_id: Optional[str] = None,
                      title: Optional[str] = None, assignee: Optional[str] = None) -> dict:
    """``accept`` and ``dismiss`` mark the item; ``track`` promotes it into
    a work item first (the connection's project unless another is named)."""
    if action not in ("accept", "dismiss", "track"):
        raise WorkError("invalid_value", "action must be accept, dismiss or track.")
    record = items.require_item(toolkit, item_id)
    if (toolkit, item_id) in runner.running():
        raise WorkError("session_running", "wait for the session to end, or open it.", 409)
    if record.get("decided"):
        return record
    if action != "track":
        return items.mark_decided(toolkit, item_id, "accepted" if action == "accept" else "dismissed")
    outcome = items.read_outcome(toolkit, item_id) or {}
    task = outcome.get("task") or {}
    target = project_id or items.project_id_for(toolkit)
    if not store.is_project_id(target) or target not in list_project_pids():
        raise WorkError("invalid_project_id", "project_id must name a project under the projects root; the connection's project exists once a session ran.")
    key = items.item_key(toolkit, item_id)
    doc = _doc()
    already = doc["promoted"].get(key)
    if already:
        result = {"created": False, "project_id": already["project_id"], "workitem_id": already["workitem_id"]}
    else:
        entry = items.item_entry(toolkit, record, outcome or None)
        result = _promote_entry(key, entry, project_id=target, title=title or task.get("title"),
                                assignee=_resolve_assignee(assignee or task.get("assignee")))
    return items.mark_decided(toolkit, item_id, "tracked", extra={"project_id": result["project_id"], "workitem_id": result["workitem_id"]})
