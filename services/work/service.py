"""Router-facing facade for the Work. Raises :class:`WorkError`; knows
nothing about HTTP. The only work module the BFF imports.

Reads compose the page from the source logs and current state (nothing to
ingest, nothing to throttle); writes are the person's marks in
the Work files under ``~/.quirq/work/`` and, for promote, one record in a project's
``.xo/workitems.json`` through the existing work item store.
"""

from __future__ import annotations

import asyncio

import logging
from datetime import datetime
from typing import Optional

from services.connections import store as connections_store
from services.cowork_agent import coder_identity
from services.cowork_agent.scopes import VisualizerScope
from services.cowork_agent.visualizer.store_common import StoreError
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.timestamps import parse_ts

from . import attention, inbox, inbox_view, items, readers, runner, store
from .store import WorkError  # re-exported: the router catches service.WorkError

__all__ = ["WorkError", "READER_NAMES", "feed", "inbox_page", "attention_items", "summary", "set_watermark",
           "dismiss", "undismiss", "ack", "unack", "promote", "create_post", "set_pin",
           "inbox_rows", "inbox_sections", "set_section_policy", "inbox_row", "inbox_item", "start_inbox_item",
           "reply_inbox_item", "send_inbox_item", "archive_inbox_item", "reopen_inbox_item"]

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
    return inbox.compose(_doc(), now=now, running=runner.running())


def attention_items() -> dict:
    doc = _doc()
    items = inbox.decisions(doc)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["reason"]] = counts.get(it["reason"], 0) + 1
    return {"items": items, "counts": counts, "total": len(items)}


def summary(*, now: Optional[datetime] = None) -> dict:
    """The small call the badge polls: what awaits a person and what runs."""
    page = inbox.compose(_doc(), now=now, running=runner.running())
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
    """The entry behind a key: an attention item (the usual promote), else
    the newest matching feed entry across every reader."""
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


# ── The Inbox: work items with a session each (section 18) ─────────────────


def inbox_rows(*, section: Optional[str] = None, entity: Optional[str] = None, state: str = "open", limit: int = 100) -> dict:
    """The Inbox as the join over work items, claims and sessions, with the
    sections and their counts, filtered on the tab, the entity and the state."""
    out = inbox_view.build(section=section, entity=entity, state=state, limit=limit, running=runner.running())
    out["runner"] = {"enabled": runner.enabled()}
    return out


def inbox_sections() -> dict:
    """One entry per section: label, counts, entities, policy, what runs."""
    rows = inbox_view.workitem_rows(runner.running())
    sections = inbox_view.sections_of(rows)
    for entry in sections:
        entry["policy"] = items.read_policy(entry["id"])
        entry["running"] = sum(1 for r in rows if r["section"] == entry["id"] and r["state"] == "running")
    return {"sections": sections, "runner": {"enabled": runner.enabled()}}


def set_section_policy(section: str, body) -> dict:
    return items.write_policy(items.check_section(section), body)


def inbox_row(project_id: str, workitem_id: str) -> dict:
    """One work item's Inbox row."""
    items.require_record(project_id, workitem_id)
    for row in inbox_view.workitem_rows(runner.running()):
        if row["project_id"] == project_id and row["id"] == workitem_id:
            return row
    raise WorkError("workitem_not_found", "No such work item.", 404)


def inbox_item(project_id: str, workitem_id: str) -> dict:
    """The row, the fact, the session, the outcome, the claim, the projected
    record, where the transcript is, and what the person may do next."""
    row = inbox_row(project_id, workitem_id)
    record = items.require_record(project_id, workitem_id)
    fact = items.read_fact(project_id, workitem_id)
    session = items.read_session(project_id, workitem_id)
    outcome = items.read_outcome(project_id, workitem_id)
    policy = items.read_policy(row["section"])
    running_now = (project_id, workitem_id) in runner.running()
    return {"key": row["key"], "row": row, "fact": fact, "session": session, "outcome": outcome,
            "claim": row.get("claim"), "workitem": inbox.workitem_view({**record, "_project_id": project_id, "_pid": row.get("pid"),
                                                                       "_in_progress": row["state"] == "running"}),
            "workbench": items.workbench_view(project_id, workitem_id),
            "transcript": {"session_id": (session or {}).get("session_id"), "native_session_id": (session or {}).get("native_session_id")},
            "policy": policy, "running": running_now,
            "can_reply": policy["sessions"]["mode"] != "off" and record.get("status") != "closed",
            "can_send": bool(policy["sessions"]["act"] and (outcome or {}).get("kind") == "reply_drafted"
                             and record.get("status") != "closed" and not running_now)}


async def reply_inbox_item(project_id: str, workitem_id: str, text: str) -> dict:
    return await runner.reply_item(project_id, workitem_id, text)


async def start_inbox_item(project_id: str, workitem_id: str, *, retry: bool = False) -> dict:
    return await runner.start_item(project_id, workitem_id, retry=retry, manual=True)


async def send_inbox_item(project_id: str, workitem_id: str) -> dict:
    return await runner.send_item(project_id, workitem_id)


def archive_inbox_item(project_id: str, workitem_id: str, *, reason: Optional[str] = None) -> dict:
    """Close the work item (``completed`` unless ``not_planned``) and let go
    of any claim. A running session ends by itself; its outcome then lands
    on a closed item and does not reopen it."""
    if reason is not None and reason not in ("completed", "not_planned"):
        raise WorkError("invalid_value", "reason must be completed or not_planned.")
    record = items.require_record(project_id, workitem_id)
    if (project_id, workitem_id) in runner.running():
        raise WorkError("session_running", "wait for the session to end, or open it.", 409)
    if record.get("status") != "closed":
        items.update_record(project_id, workitem_id, status="closed", state_reason=reason or "completed")
    try:
        items.scope_for(project_id).release_workitem_quiet(workitem_id)
    except Exception:  # noqa: BLE001 - a claim that cannot be released stays derived from the live set
        logger.warning("work archive: could not release %s/%s", project_id, workitem_id, exc_info=True)
    return inbox_row(project_id, workitem_id)


def reopen_inbox_item(project_id: str, workitem_id: str) -> dict:
    record = items.require_record(project_id, workitem_id)
    if record.get("status") == "closed":
        items.update_record(project_id, workitem_id, status="open", state_reason="reopened")
    return inbox_row(project_id, workitem_id)
