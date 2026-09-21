"""Feeders: turn what arrived in the workspace into facts
(:func:`services.inbox.facts.build_fact`), which the service ingests as
work items.

Each feeder takes the ledger (for its switches and cursor) and returns a
:class:`FeedResult`. They only read; the service ingests the facts and
advances the cursor. A feeder that raises is skipped for that run by the
service; it never stops the others. Feeders are looked up by name from
:data:`FEEDER_NAMES` so tests can patch one function on this module.

Timestamps from the producers differ (``Z``, ``+00:00``, naive), so every
comparison goes through :func:`ledger.parse_ts`; the cursor is kept as the
producer's original string.

Three feeders, none of which talks to the network or imports a router:
``sharing`` reads the relay's recent events (section ``projects``),
``issues`` reads each project's GitHub issue mirror (section ``issues``,
adopted work items), ``connections`` reads the per-toolkit ``events.jsonl``
the connections poller appends to (section ``connections``). The workspace
stream (sessions starting, todos, files) is the Activity page's, never the
Inbox's.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple, Optional

from services.connections import store as connections_store
from services.cowork_agent.project_sharing import status as sharing_status
from services.cowork_agent.scopes import VisualizerScope
from services.cowork_agent.visualizer import github_mirror, workspace_index

from . import facts, ledger

logger = logging.getLogger(__name__)

FEEDER_NAMES = ("sharing", "issues", "connections")
BOOTSTRAP_WINDOW = timedelta(hours=24)   # no cursor: only the last day, never the whole history
ISSUES_BOOTSTRAP_WINDOW = timedelta(days=7)   # issues move slower than mail; a week is the first read
CONNECTIONS_FETCH_LIMIT = 200   # newest events read per toolkit per run
_FUTURE_SLACK = timedelta(days=1)   # an issue updated_at further ahead than this never pins the cursor
# Connection events are stamped with the producer's time and a calendar event
# with its start, so only clock skew is tolerated here; anything further ahead
# is emitted but never pins the cursor (see connections()).
_CONNECTIONS_FUTURE_SLACK = timedelta(minutes=5)
_SHARING_TITLES = {
    "shared_with_you": "Repo shared with this workspace: {repo}",
    "fetched": "New commits fetched: {repo}",
    "error": "Sharing error: {repo}",
    "revoked": "Sharing access revoked: {repo}",
}


class FeedResult(NamedTuple):
    items: list[dict]                    # facts, shaped by facts.build_fact
    cursor: Optional[str]                # None: leave the stored cursor alone


def _str_list(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _one_line(text, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _safe_fact(**fields) -> Optional[dict]:
    try:
        return facts.build_fact(**fields)
    except ledger.InboxError as exc:
        logger.warning("inbox feeder skipped a fact: %s", exc.message)
        return None


def _safe_key(value: str) -> str:
    return re.sub(r"\s+", "-", value)[:400]


# ── sharing ──────────────────────────────────────────────────────────────────


def sharing(doc: dict) -> FeedResult:
    """Relay transitions newer than the cursor. Kinds outside the four
    documented ones (for example ``cloned``, ``clone_failed``) get a generic
    title so a new kind never raises."""
    snap = sharing_status.snapshot()
    repos = snap.get("repos") if isinstance(snap.get("repos"), dict) else {}
    cursor = ledger.parse_ts(doc["cursors"].get("sharing"))
    newest: Optional[tuple[datetime, str]] = None
    items = []
    for e in snap.get("recent") or []:
        dt = ledger.parse_ts(e.get("at")) if isinstance(e, dict) else None
        if dt is None:
            continue
        if newest is None or dt > newest[0]:
            newest = (dt, e["at"])
        repo, kind = str(e.get("repo") or ""), str(e.get("kind") or "")
        if (cursor is not None and dt <= cursor) or not repo or not kind:
            continue
        project = (repos.get(repo) or {}).get("project") if isinstance(repos.get(repo), dict) else None
        project = project if facts.is_project_id(project) else None
        title = _SHARING_TITLES.get(kind, "Sharing: {kind} for {repo}").format(kind=kind, repo=repo)
        # Open lands on Sharing with this project selected, where the fetched
        # commits and Apply are; a repo not cloned here opens the page.
        link = {"view": "sharing", "project": project} if project else {"view": "sharing"}
        event = re.sub(r"[^a-z0-9_.:-]", "-", kind.lower())[:50]
        items.append(_safe_fact(
            title=_one_line(title, facts.TITLE_MAX), body=str(e.get("detail") or "")[:facts.BODY_MAX],
            kind="sharing." + event, section="projects", entity=project or repo, project_id=project, link=link, ts=e["at"],
            source={"kind": "sharing", "key": _safe_key(f"sharing:{kind}:{repo}:{e['at']}"),
                    "sharing": {"repo": repo[:200], "event": event}}))
    return FeedResult([it for it in items if it is not None], newest[1] if newest else None)


# ── issues ───────────────────────────────────────────────────────────────────


def _issue_fact(pid: str, repo: str, row: dict) -> Optional[dict]:
    number, state = row["number"], row["state"]
    lines = []
    raw_labels = row.get("labels")
    labels = [lb for lb in raw_labels if isinstance(lb, str) and lb] if isinstance(raw_labels, list) else []
    if labels:
        lines.append("labels: " + ", ".join(labels))
    assignees = row.get("assignees") if isinstance(row.get("assignees"), list) else []
    logins = [a["login"] for a in assignees if isinstance(a, dict) and isinstance(a.get("login"), str) and a["login"]]
    if logins:
        lines.append("assignees: " + ", ".join(logins))
    url = row.get("url")
    if not facts.is_url(url) or not str(url).startswith("https://"):
        return None   # the store needs an https issue url to adopt
    node_id = row.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        return None
    return _safe_fact(
        title=_one_line(f"Issue #{number} in {pid}: {_one_line(row['title'], 120)}", facts.TITLE_MAX),
        body="\n".join(lines)[:facts.BODY_MAX], kind=f"issue.{state}", section="issues", entity=repo,
        project_id=pid, link={"view": "projects", "project": pid}, ts=row["updated_at"], url=url,
        source={"kind": "github", "github": {"repo": repo, "number": number, "node_id": node_id, "url": url}})


def _mirror_rows(pid: str) -> tuple[Optional[list[dict]], bool]:
    """``(rows, unreadable)`` for one project's issue mirror. ``rows`` is
    ``None`` when there is nothing to read; ``unreadable`` is True only
    when a mirror file exists and could not be used."""
    path = github_mirror.mirror_path(pid)
    if path is None or not path.is_file():
        return None, False          # absent by design: readable-empty
    doc = github_mirror.read_mirror(pid)
    if doc is None:
        return None, True           # exists but unusable (bad JSON, wrong schema, unreadable)
    issues = doc.get("issues")
    rows = [r for r in (issues.values() if isinstance(issues, dict) else [])
            if isinstance(r, dict) and isinstance(r.get("number"), int) and not isinstance(r.get("number"), bool)
            and isinstance(r.get("title"), str)]
    return rows, False


def _project_repo(pid: str) -> Optional[str]:
    try:
        return VisualizerScope(pid).github_repo()
    except Exception:  # noqa: BLE001 - a project whose remote cannot be read has no repo to adopt from
        return None


def issues(doc: dict) -> FeedResult:
    """GitHub issues in a watched state (``sources.issues.states``, default
    ``open``) whose ``updated_at`` is newer than the cursor, or than the last
    7 days when there is none, across every project's issue mirror, as
    adopted work items in their project (the projection reads their state
    from the mirror, so an issue that closes closes its work item).

    The cursor advances to the newest ``updated_at`` seen across all readable
    rows, kept or not, ignoring a value more than a day ahead of now so one
    hand-edited mirror cannot starve every other project."""
    states = frozenset(_str_list(ledger.source_config(doc, "issues").get("states")))
    cursor = ledger.parse_ts(doc["cursors"].get("issues"))
    now = datetime.now(timezone.utc)
    floor = cursor or (now - ISSUES_BOOTSTRAP_WINDOW)
    horizon = now + _FUTURE_SLACK
    newest: Optional[tuple[datetime, str]] = None
    items: list[dict] = []
    # Enumerated through the module attribute, so a test can patch the
    # enumeration on the module it lives in.
    for pid in workspace_index.list_project_ids():
        if not facts.is_project_id(pid):
            continue
        rows, unreadable = _mirror_rows(pid)
        if unreadable:
            logger.warning("inbox issues: the mirror for %s is unreadable this run", pid)
        repo: Optional[str] = None
        for row in rows or []:
            dt = ledger.parse_ts(row.get("updated_at"))
            if dt is None:
                continue   # only the cursor and the emit need a parsable updated_at
            if dt > horizon:
                logger.debug("inbox issues: %s #%s has updated_at in the future; it never pins the cursor",
                             pid, row["number"])
            elif newest is None or dt > newest[0]:
                newest = (dt, row["updated_at"])
            if row.get("state") in states and dt > floor:
                repo = repo or _project_repo(pid)
                if repo is None:
                    logger.debug("inbox issues: %s has no GitHub remote; its issues are not adopted", pid)
                    break
                it = _issue_fact(pid, repo, row)
                if it is not None:
                    items.append(it)
    return FeedResult(items, newest[1] if newest else None)


# ── connections ──────────────────────────────────────────────────────────────


def _connection_fact(toolkit: str, ev: dict) -> Optional[dict]:
    kind, key = ev["type"], ev["key"]
    title = _one_line(ev.get("title") if isinstance(ev.get("title"), str) else "", facts.TITLE_MAX) \
        or f"{toolkit} {kind}: {key}"
    body = ev.get("body") if isinstance(ev.get("body"), str) else ""
    url = ev.get("url")
    return _safe_fact(
        title=title, body=body[:facts.BODY_MAX],
        kind=re.sub(r"[^a-z0-9_.:-]", "-", f"{toolkit}.{kind}".lower())[:60], section="connections", entity=toolkit,
        link={"view": "connectors"}, ts=ev["ts"], url=url if facts.is_url(url) else None,
        source={"kind": "connection", "key": _safe_key(f"connection:{toolkit}:{kind}:{key}"),
                "connection": {"toolkit": toolkit[:200], "type": str(kind)[:200], "event": str(key)[:200]}})


def connections(doc: dict) -> FeedResult:
    """Collected items newer than the cursor (or the last 24 h when there
    is none) from every configured connection's ``events.jsonl``. One cursor
    covers every toolkit: it advances to the newest ``ts`` seen across all
    of them, so a toolkit added later only surfaces what arrives after that
    point (its older lines stay in its events file).

    Collectors stamp an event with the producer's own time, and a calendar
    collector stamps upcoming events with their start (up to a week ahead).
    Such an event is still emitted, but a ``ts`` further ahead than
    :data:`_CONNECTIONS_FUTURE_SLACK` never pins the cursor: otherwise one
    calendar poll would put the floor days into the future and mail or
    pages arriving now, from any toolkit, would never surface. Re-reading
    the same future event on later runs is harmless (keyed ingest)."""
    cursor = ledger.parse_ts(doc["cursors"].get("connections"))
    now = datetime.now(timezone.utc)
    floor = cursor or (now - BOOTSTRAP_WINDOW)
    horizon = now + _CONNECTIONS_FUTURE_SLACK
    newest: Optional[tuple[datetime, str]] = None
    kept: list[tuple[datetime, str, dict]] = []
    for toolkit in connections_store.list_configured():
        try:
            events = connections_store.read_events(toolkit, limit=CONNECTIONS_FETCH_LIMIT)
        except Exception as exc:
            logger.warning("inbox connections: could not read events for %s: %s", toolkit, exc)
            continue
        for ev in events:
            if not isinstance(ev, dict) or not isinstance(ev.get("type"), str) or not ev["type"] \
                    or not isinstance(ev.get("key"), str) or not ev["key"]:
                continue
            dt = ledger.parse_ts(ev.get("ts"))
            if dt is None:
                continue
            if dt > horizon:
                logger.debug("inbox connections: %s %s has a ts in the future; it never pins the cursor",
                             toolkit, ev["key"])
            elif newest is None or dt > newest[0]:
                newest = (dt, ev["ts"])
            if dt > floor:
                kept.append((dt, toolkit, ev))
    kept.sort(key=lambda entry: entry[0])   # read_events is newest-first; ingest chronologically
    items = [it for it in (_connection_fact(tk, ev) for _dt, tk, ev in kept) if it is not None]
    return FeedResult(items, newest[1] if newest else None)


def feeder(name: str) -> Callable[[dict], FeedResult]:
    return globals()[name]
