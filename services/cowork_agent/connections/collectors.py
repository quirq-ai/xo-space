"""The collectors catalog: what to fetch from each toolkit and how to turn
the answer into ``events.jsonl`` lines.

A collector spec is data, not code::

    {"id", "label", "tool", "args", "list_keys", "id_keys", "title_keys",
     "body_keys", "ts_keys", "url_keys" (optional), "url_template" (optional),
     "default" (optional bool)}

``tool`` is always a read-only Composio slug (category ``read`` in
``connectors/composio/categories.py``; a test pins that). ``args`` may
carry the string placeholders ``{now_iso}`` and ``{now_plus_7d_iso}``,
substituted by :func:`render_args` on str leaves only (never
``str.format``, so a literal brace in a query is safe).

Composio wraps a tool's payload as ``{"successful", "data", "error"}``
inside the first text content item, so every ``list_keys`` list also
names the ``data.<x>`` path; the poller checks ``successful`` itself.

The dedupe key of an event is the element id only (``id_keys``): a
calendar event that is rescheduled keeps its id and is not surfaced
again; a recurring instance carries its own id and is. That is by design
(a stable key, no flood on every reschedule), and a test pins it.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from services.cowork_agent.connectors.composio.service import TOOLKITS
from services.cowork_agent.inbox.store import parse_ts

TITLE_MAX, BODY_MAX = 300, 4000
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_PLACEHOLDER_NOW, _PLACEHOLDER_7D = "{now_iso}", "{now_plus_7d_iso}"
_DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DIGITS_RE = re.compile(r"\d+")
_EPOCH_MS_THRESHOLD = 1e11        # anything larger is milliseconds, not seconds
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_GMAIL_MAP = {
    "list_keys": ["messages", "data.messages", "items"],
    "id_keys": ["messageId", "id", "threadId"],
    "title_keys": ["subject", "snippet", "preview.subject"],
    "body_keys": ["snippet", "preview.body", "messageText"],
    "ts_keys": ["messageTimestamp", "internalDate", "date"],
    "url_template": "https://mail.google.com/mail/u/0/#all/{id}",
}

_CATALOG: dict[str, list[dict]] = {toolkit: [] for toolkit in TOOLKITS}
_CATALOG.update({
    "gmail": [
        {"id": "unread", "label": "Unread mail", "default": True, "tool": "GMAIL_FETCH_EMAILS",
         "args": {"query": "is:unread", "max_results": 20}, **_GMAIL_MAP},
        {"id": "inbox", "label": "New mail in Inbox (last day)", "tool": "GMAIL_FETCH_EMAILS",
         "args": {"query": "in:inbox newer_than:1d", "max_results": 20}, **_GMAIL_MAP},
    ],
    "googlecalendar": [
        {"id": "upcoming", "label": "Upcoming events (next 7 days)", "default": True,
         "tool": "GOOGLECALENDAR_EVENTS_LIST",
         "args": {"calendarId": "primary", "timeMin": _PLACEHOLDER_NOW, "timeMax": _PLACEHOLDER_7D,
                  "singleEvents": True, "orderBy": "startTime", "maxResults": 25},
         "list_keys": ["items", "data.items", "events", "data.events"],
         "id_keys": ["id"], "title_keys": ["summary"], "body_keys": ["description", "location"],
         "ts_keys": ["start.dateTime", "start.date", "updated"], "url_keys": ["htmlLink"]},
    ],
    "notion": [
        {"id": "recent_pages", "label": "Recently edited pages", "default": True,
         "tool": "NOTION_SEARCH_NOTION_PAGE",
         "args": {"query": "", "page_size": 20,
                  "sort": {"direction": "descending", "timestamp": "last_edited_time"}},
         "list_keys": ["results", "data.results", "data.response_data.results"],
         "id_keys": ["id"],
         "title_keys": ["properties.title.title.0.plain_text", "properties.Name.title.0.plain_text",
                        "title", "name"],
         "body_keys": [], "ts_keys": ["last_edited_time", "created_time"], "url_keys": ["url"]},
    ],
})


# ── Catalog access ───────────────────────────────────────────────────────────


def catalog(toolkit: str) -> list[dict]:
    """Copies of the specs for ``toolkit`` (empty for a toolkit without
    collectors, or one the catalog does not know)."""
    return copy.deepcopy(_CATALOG.get(toolkit, []))


def collector(toolkit: str, collector_id: str) -> Optional[dict]:
    return next((spec for spec in catalog(toolkit) if spec["id"] == collector_id), None)


def default_ids(toolkit: str) -> list[str]:
    return [spec["id"] for spec in _CATALOG.get(toolkit, []) if spec.get("default")]


# ── Time ─────────────────────────────────────────────────────────────────────


def _aware(now: datetime) -> datetime:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return _aware(dt).strftime(TS_FORMAT)


def _from_epoch(number) -> Optional[datetime]:
    try:
        value = float(number)
        if value > _EPOCH_MS_THRESHOLD:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_any_ts(value) -> Optional[datetime]:
    """Producers disagree on timestamps: ISO with ``Z`` or an offset,
    epoch seconds or milliseconds (int, float or numeric string, told apart
    by magnitude) and date-only strings for all-day events. Always an
    aware UTC datetime, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _DIGITS_RE.fullmatch(text):
        return _from_epoch(int(text))
    if _DATE_ONLY_RE.fullmatch(text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return parse_ts(text)


def render_args(spec: dict, now: datetime) -> dict:
    """The spec's ``args`` with the two placeholders substituted on str
    leaves anywhere in the tree. Non-string leaves pass through untouched."""
    now_utc = _aware(now)
    values = {_PLACEHOLDER_NOW: iso(now_utc), _PLACEHOLDER_7D: iso(now_utc + timedelta(days=7))}

    def walk(node):
        if isinstance(node, str):
            for placeholder, value in values.items():
                node = node.replace(placeholder, value)
            return node
        if isinstance(node, dict):
            return {key: walk(child) for key, child in node.items()}
        if isinstance(node, list):
            return [walk(child) for child in node]
        return node

    args = spec.get("args")
    return walk(args) if isinstance(args, dict) else {}


# ── Payload mapping ──────────────────────────────────────────────────────────


def lookup(obj, dotted_path) -> Any:
    """Follow ``a.b.0.c`` through dicts and lists; ``None`` when any step is
    missing, out of range or not a container."""
    cur = obj
    for segment in str(dotted_path).split("."):
        if isinstance(cur, dict):
            if segment not in cur:
                return None
            cur = cur[segment]
        elif isinstance(cur, list):
            if not segment.isdigit() or int(segment) >= len(cur):
                return None
            cur = cur[int(segment)]
        else:
            return None
    return cur


def _as_text(value) -> Optional[str]:
    """Only a non-blank str or an int becomes text; lists, dicts, floats and
    bools are skipped so a Notion rich-text list never turns into its repr,
    and an empty subject falls through to the next key."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _as_key(value) -> Optional[str]:
    text = _as_text(value)
    return text.strip() if text and text.strip() else None


def _as_url(value) -> Optional[str]:
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return None


def _first(element: dict, keys, accept: Callable[[Any], Optional[str]]) -> Optional[str]:
    for key in keys or []:
        found = accept(lookup(element, key))
        if found is not None:
            return found
    return None


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _find_list(spec: dict, payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for path in spec.get("list_keys") or []:
        value = lookup(payload, path)
        if isinstance(value, list):
            return value
    return []


def _typed_title(element: dict) -> Optional[str]:
    """Notion rows name their title property freely; find the property of
    type ``title`` and join its rich-text ``plain_text`` parts."""
    props = element.get("properties")
    if not isinstance(props, dict):
        return None
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title" and isinstance(prop.get("title"), list):
            text = "".join(part["plain_text"] for part in prop["title"]
                           if isinstance(part, dict) and isinstance(part.get("plain_text"), str))
            if text.strip():
                return text
    return None


def extract_items(spec: dict, payload, *, toolkit: str, now: datetime) -> list[dict]:
    """Map a tool payload to event lines, newest-first by ``ts``:
    ``{"ts", "type", "key", "title", "body", "url", "toolkit"}``.
    Elements without a usable id and non-dict elements are skipped."""
    now_utc = _aware(now)
    label = str(spec.get("label") or spec.get("id") or "")
    out: list[dict] = []
    for element in _find_list(spec, payload):
        if not isinstance(element, dict):
            continue
        key = _first(element, spec.get("id_keys"), _as_key)
        if key is None:
            continue
        title = _first(element, spec.get("title_keys"), _as_text)
        if title is None:
            title = _typed_title(element)
        title = _one_line(title)[:TITLE_MAX] if title else ""
        if not title:
            title = _one_line(f"{label}: {key}")[:TITLE_MAX]
        body = (_first(element, spec.get("body_keys"), _as_text) or "")[:BODY_MAX]
        ts = next((dt for dt in (parse_any_ts(lookup(element, k)) for k in spec.get("ts_keys") or [])
                   if dt is not None), now_utc)
        url = _first(element, spec.get("url_keys"), _as_url)
        if url is None and isinstance(spec.get("url_template"), str):
            url = spec["url_template"].format(id=key)
        out.append({"ts": iso(ts), "type": spec["id"], "key": key, "title": title, "body": body,
                    "url": url, "toolkit": toolkit})
    out.sort(key=lambda event: parse_ts(event["ts"]) or _EPOCH, reverse=True)
    return out
