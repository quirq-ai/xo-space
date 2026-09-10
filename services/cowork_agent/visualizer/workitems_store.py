"""CRUD over ``<project>/.xo/workitems.json`` — the durable work surface."""

from __future__ import annotations

import copy
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    read_stamped_document,
    unsupported_schema_message,
    write_json_owned,
)
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.ingest.events import Event, WorkitemEvent
from services.cowork_agent.visualizer.sinks import timeline


logger = logging.getLogger(__name__)


#: On-disk revision of ``workitems.json`` (plan §5.1).
WORKITEMS_SCHEMA = 1

#: Value written into the document's ``$schema`` key.
_SCHEMA_REF = "xo/workitems.schema.json"

#: The top-level keys this store owns.
_OWNS: frozenset[str] = frozenset({"$schema", "schema", "updated_at", "items"})

#: ``open`` | ``closed`` and nothing else — GitHub's vocabulary (D7).
VALID_STATUSES: frozenset[str] = frozenset({"open", "closed"})

#: GitHub's ``state_reason`` values. ``None`` clears it.
VALID_STATE_REASONS: frozenset[str] = frozenset(
    {"completed", "not_planned", "reopened"}
)

VALID_SOURCE_KINDS: frozenset[str] = frozenset({"local", "github"})

#: The fields GitHub is authoritative for. Never stored for an adopted item
#: (§5.3) — see the module docstring.
GITHUB_OWNED_FIELDS: tuple[str, ...] = ("status", "state_reason", "body")

_DEFAULT_STATUS = "open"

# Same charset as ``todos_store``: permissive enough for realistic adapter keys
# and composite session ids, restrictive enough to reject path traversal and
# anything that could turn a synced document into a channel for arbitrary
# caller text.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")

# A label is human text (GitHub allows spaces, colons, emoji), so only control
# characters are excluded.
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,100}$")

_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}$")

# Canonical lowercase UUID v4, matching workitems.schema.json.
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_TITLE_LIMIT = 1000
_BODY_LIMIT = 16000
_MAX_LABELS = 50
_MAX_LINKS = 500


class _Unset:
    """Sentinel: "the caller did not supply this field"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


UNSET = _Unset()


class WorkitemsStoreError(Exception):
    """
    Base for all store failures. ``code`` is the BFF error code the route maps
    to ``detail.code`` — same shape as ``TodosStoreError``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Lifecycle events (plan §8, W10) ────────────────────────────────────────
# The store is the event source, not the routes.


def emit_workitem_events(project_id: str, events: Iterable[Event]) -> None:
    """Append workitem lifecycle events to a project's timeline."""
    try:
        root = project_layout.runtime_dir_for_project(project_id)
    except Exception:  # noqa: BLE001 - a log must not fail the write it logs
        logger.warning(
            "workitem events: no runtime home for %s; dropping %s line(s)",
            project_id, len(list(events)) if isinstance(events, list) else "some",
            exc_info=True,
        )
        return
    timeline.apply_quiet(root, events)


def _emit(workitems_path: Path, events: list[Event]) -> None:
    """Fan lifecycle events to the timeline. **Never fails the write.**"""
    if not events:
        return
    emit_workitem_events(workitems_path.parent.parent.name, events)


def _issue_ref(source: object) -> tuple[Optional[str], Optional[int]]:
    """``(repo, number)`` from a validated ``source``, or ``(None, None)``."""
    if not isinstance(source, dict):
        return (None, None)
    ref = source.get("github")
    if not isinstance(ref, dict):
        return (None, None)
    repo = ref.get("repo")
    number = ref.get("number")
    return (
        repo if isinstance(repo, str) else None,
        number if isinstance(number, int) and not isinstance(number, bool) else None,
    )


def is_deleted(item: object) -> bool:
    """Whether a workitem record carries a tombstone."""
    return isinstance(item, dict) and item.get("deleted_at") is not None


def is_adopted(item: object) -> bool:
    """Whether the record mirrors a GitHub issue (``source.kind == "github"``)."""
    if not isinstance(item, dict):
        return False
    source = item.get("source")
    return isinstance(source, dict) and source.get("kind") == "github"


# ── Validation ─────────────────────────────────────────────────────────────


def _validate_safe_key(value: object, kind: str) -> None:
    if not isinstance(value, str) or not _SAFE_KEY_RE.match(value):
        raise WorkitemsStoreError(
            f"invalid_{kind}",
            f"{kind} must match [A-Za-z0-9_:\\-\\.] (1..200 chars).",
        )


def _validate_content_length(value: object, *, field: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkitemsStoreError(
            "invalid_value", f"{field} is required and must be non-empty."
        )
    if len(value) > limit:
        raise WorkitemsStoreError("invalid_value", f"{field} exceeds {limit} chars.")


def _validate_optional_text(value: object, *, field: str, limit: int) -> None:
    """
    ``None`` or a string within ``limit``. Unlike
    :func:`_validate_content_length` an empty string is allowed — an emptied
    body is a real edit, not a malformed one.
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise WorkitemsStoreError("invalid_value", f"{field} must be a string or null.")
    if len(value) > limit:
        raise WorkitemsStoreError("invalid_value", f"{field} exceeds {limit} chars.")


def _validate_status(value: object) -> None:
    if value not in VALID_STATUSES:
        raise WorkitemsStoreError(
            "invalid_status",
            f"status must be one of {sorted(VALID_STATUSES)}. There is no "
            f"in_progress: it is derived from a live agent claim, never stored.",
        )


def _validate_state_reason(value: object) -> None:
    if value is None:
        return
    if value not in VALID_STATE_REASONS:
        raise WorkitemsStoreError(
            "invalid_state_reason",
            f"state_reason must be null or one of {sorted(VALID_STATE_REASONS)}.",
        )


def _validate_labels(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise WorkitemsStoreError("invalid_value", "labels must be a list of strings.")
    if len(value) > _MAX_LABELS:
        raise WorkitemsStoreError("invalid_value", f"labels exceeds {_MAX_LABELS} entries.")
    labels: list[str] = []
    for label in value:
        if not isinstance(label, str) or not _LABEL_RE.match(label):
            raise WorkitemsStoreError(
                "invalid_value",
                "each label must be a non-empty string of at most 100 printable chars.",
            )
        labels.append(label)
    return labels


def _validate_ids(value: object, *, kind: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise WorkitemsStoreError("invalid_value", f"{kind}s must be a list of strings.")
    if len(value) > _MAX_LINKS:
        raise WorkitemsStoreError("invalid_value", f"{kind}s exceeds {_MAX_LINKS} entries.")
    for entry in value:
        _validate_safe_key(entry, kind)
    return [str(entry) for entry in value]


def _validate_github_ref(value: object) -> dict:
    """Validate and normalise ``source.github``."""
    if not isinstance(value, dict):
        raise WorkitemsStoreError(
            "invalid_source", "source.github must be an object."
        )
    missing = [k for k in ("repo", "number", "node_id", "url") if k not in value]
    if missing:
        raise WorkitemsStoreError(
            "invalid_source", f"source.github is missing {missing}."
        )
    unknown = sorted(set(value) - {"repo", "number", "node_id", "url"})
    if unknown:
        raise WorkitemsStoreError(
            "invalid_source", f"source.github carries unknown key(s) {unknown}."
        )
    repo = value["repo"]
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise WorkitemsStoreError(
            "invalid_source", "source.github.repo must be 'owner/name'."
        )
    number = value["number"]
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise WorkitemsStoreError(
            "invalid_source", "source.github.number must be a positive integer."
        )
    _validate_safe_key(value["node_id"], "node_id")
    url = value["url"]
    if (
        not isinstance(url, str)
        or not url.startswith("https://")
        or len(url) > 2000
        or any(ch.isspace() for ch in url)
    ):
        raise WorkitemsStoreError(
            "invalid_source", "source.github.url must be an https URL."
        )
    return {"repo": repo, "number": number, "node_id": value["node_id"], "url": url}


def _validate_source(value: object) -> dict:
    """Validate and normalise the ``source`` block."""
    if value is None:
        return {"kind": "local"}
    if not isinstance(value, dict):
        raise WorkitemsStoreError("invalid_source", "source must be an object.")
    unknown = sorted(set(value) - {"kind", "github"})
    if unknown:
        raise WorkitemsStoreError(
            "invalid_source", f"source carries unknown key(s) {unknown}."
        )
    kind = value.get("kind")
    if kind not in VALID_SOURCE_KINDS:
        raise WorkitemsStoreError(
            "invalid_source", f"source.kind must be one of {sorted(VALID_SOURCE_KINDS)}."
        )
    if kind == "github":
        if "github" not in value:
            raise WorkitemsStoreError(
                "invalid_source",
                "source.github is required when source.kind == 'github'.",
            )
        return {"kind": "github", "github": _validate_github_ref(value["github"])}
    if value.get("github") is not None:
        raise WorkitemsStoreError(
            "invalid_source",
            "source.github is only valid when source.kind == 'github'.",
        )
    return {"kind": "local"}


def _refuse_github_owned(supplied: dict[str, Any]) -> None:
    """Refuse a write of a field GitHub owns, for an adopted item (§5.3)."""
    named = [
        field
        for field, value in supplied.items()
        if field in GITHUB_OWNED_FIELDS and not isinstance(value, _Unset)
    ]
    if named:
        raise WorkitemsStoreError(
            "github_authoritative",
            f"{sorted(named)} are GitHub's for an adopted workitem and are not "
            f"stored here: a stale value is a false statement about who owes "
            f"what, so the field is omitted rather than snapshotted. Change it "
            f"on the issue.",
        )


# ── Document I/O ───────────────────────────────────────────────────────────


def _corrupt(path: Path, reason: str) -> WorkitemsStoreError:
    """The O-E refusal, in one place."""
    return WorkitemsStoreError(
        "corrupt_document",
        f"{path} is not a readable workitems document ({reason}); refusing to "
        f"read or write it. Treating it as empty would discard every workitem "
        f"it holds (docs/OUTSTANDING.md O-E). Repair or move the file.",
    )


def _read_document(path: Path) -> dict:
    """Return the document's items map, deep-copied."""
    state, value = read_stamped_document(path, schema=WORKITEMS_SCHEMA)
    if state == "absent":
        return {}
    if state == "fault":
        raise _corrupt(path, value)
    if state == "schema":
        raise WorkitemsStoreError(
            "unsupported_schema",
            unsupported_schema_message(path, value, WORKITEMS_SCHEMA),
        )
    parsed = value

    raw = parsed.get("items")
    if raw is None:
        raise _corrupt(path, "no items map")
    if not isinstance(raw, dict):
        raise _corrupt(path, f"items is a {type(raw).__name__}, expected object")
    for key, record in raw.items():
        if not isinstance(record, dict):
            raise _corrupt(path, f"items[{key!r}] is a {type(record).__name__}")
        if not isinstance(key, str) or not _UUID4_RE.match(key):
            raise _corrupt(path, f"items key {key!r} is not a canonical UUID4")
        if record.get("id") != key:
            raise _corrupt(
                path,
                f"items[{key!r}] carries id {record.get('id')!r}; the key and "
                f"the record's own id must agree",
            )
    return copy.deepcopy(raw)


def _write(path: Path, items: dict) -> None:
    """Persist the items map, refusing a document that went unreadable."""
    try:
        write_json_owned(
            path,
            owns=_OWNS,
            values={
                "$schema": _SCHEMA_REF,
                "schema": WORKITEMS_SCHEMA,
                "updated_at": _now_iso(),
                "items": items,
            },
        )
    except CorruptDocumentError as exc:
        raise _corrupt(path, exc.reason) from exc


def _visible(item: dict, include_deleted: bool) -> bool:
    return include_deleted or not is_deleted(item)


# ── The record shape ───────────────────────────────────────────────────────

#: Canonical key order for a stored record, matching ``workitems.schema.json``
#: and the plan's §5.1 example.
_KEY_ORDER: tuple[str, ...] = (
    "id", "title", "body", "labels", "status", "state_reason", "source",
    "assignee", "links", "created_at", "updated_at", "created_by",
    "deleted_at", "deleted_by",
)


def _ordered(record: dict) -> dict:
    """``record`` with its keys in :data:`_KEY_ORDER`, extras kept at the end."""
    out = {key: record[key] for key in _KEY_ORDER if key in record}
    for key, value in record.items():
        if key not in out:
            out[key] = value
    return out


def _new_record(
    *,
    runtime: str,
    title: str,
    body: Optional[str],
    labels: list[str],
    status: str,
    state_reason: Optional[str],
    assignee: Optional[str],
    source: dict,
    links: dict,
) -> dict:
    """One freshly-minted record, with a server-minted UUID4 ``id``."""
    adopted = source["kind"] == "github"
    stamp = _now_iso()
    record: dict[str, Any] = {"id": str(uuid.uuid4()), "title": title}
    if not adopted:
        record["body"] = body
    record["labels"] = labels
    if not adopted:
        record["status"] = status
        record["state_reason"] = state_reason
    record["source"] = source
    record["assignee"] = assignee
    record["links"] = links
    record["created_at"] = stamp
    record["updated_at"] = stamp
    record["created_by"] = runtime
    record["deleted_at"] = None
    record["deleted_by"] = None
    return _ordered(record)


# ── CRUD ───────────────────────────────────────────────────────────────────


def create_workitem(
    workitems_path: Path,
    *,
    runtime: str,
    title: str,
    body: Optional[str] = None,
    labels: Optional[list[str]] = None,
    status: Optional[str] = None,
    state_reason: Optional[str] = None,
    assignee: Optional[str] = None,
    source: Optional[dict] = None,
    todo_ids: Optional[list[str]] = None,
    session_ids: Optional[list[str]] = None,
) -> dict:
    """Create a workitem. Returns the stored record."""
    _validate_safe_key(runtime, "runtime")
    _validate_content_length(title, field="title", limit=_TITLE_LIMIT)
    resolved_source = _validate_source(source)
    adopted = resolved_source["kind"] == "github"
    links = {
        "todo_ids": _validate_ids(todo_ids or [], kind="todo_id"),
        "session_ids": _validate_ids(session_ids or [], kind="session_id"),
    }
    resolved_labels = _validate_labels(labels or [])

    if assignee is not None:
        _validate_safe_key(assignee, "assignee")
    if adopted:
        _refuse_github_owned(
            {
                "status": UNSET if status is None else status,
                "state_reason": UNSET if state_reason is None else state_reason,
                "body": UNSET if body is None else body,
            }
        )
    else:
        _validate_optional_text(body, field="body", limit=_BODY_LIMIT)
        _validate_state_reason(state_reason)

    # ``is None`` rather than ``or``: an empty status is a malformed request,
    # not an unstated one, and must not quietly become "open".
    initial_status = _DEFAULT_STATUS if status is None else status
    if not adopted:
        _validate_status(initial_status)

    record = _new_record(
        runtime=runtime,
        title=title,
        body=body,
        labels=resolved_labels,
        status=initial_status,
        state_reason=state_reason,
        assignee=assignee,
        source=resolved_source,
        links=links,
    )
    workitem_id = record["id"]

    with locked(workitems_path):
        items = _read_document(workitems_path)
        if workitem_id in items:
            # A UUID4 collision is not a thing that happens; if it did,
            # overwriting the other record would be the worst possible
            # response. 500, and the caller retries.
            raise WorkitemsStoreError(
                "scope_unavailable",
                f"workitem id collision ({workitem_id}); retry the call.",
            )
        items[workitem_id] = record
        _write(workitems_path, items)

    # ``created`` and, for an item born adopted, ``adopted``: a record that
    # starts life mirroring an issue did both in one call, and a reader of the
    # log should not have to infer the second from a ``kind`` field on the
    # first.
    events: list[Event] = [
        WorkitemEvent(
            ts=record["created_at"],
            runtime=runtime,
            action="created",
            workitem_id=workitem_id,
            title=title,
            kind=resolved_source["kind"],
        )
    ]
    if adopted:
        repo, number = _issue_ref(resolved_source)
        events.append(
            WorkitemEvent(
                ts=record["created_at"],
                runtime=runtime,
                action="adopted",
                workitem_id=workitem_id,
                title=title,
                repo=repo,
                number=number,
            )
        )
    _emit(workitems_path, events)

    return copy.deepcopy(record)


def get_workitem(
    workitems_path: Path, workitem_id: str, *, include_deleted: bool = False
) -> Optional[dict]:
    """Return the record, or ``None`` if there is no such workitem."""
    items = _read_document(workitems_path)
    record = items.get(workitem_id)
    if not isinstance(record, dict) or not _visible(record, include_deleted):
        return None
    return record


def list_workitems(
    workitems_path: Path,
    *,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    assignee: Optional[str] = None,
    include_deleted: bool = False,
) -> list[dict]:
    """Every workitem, in creation order, oldest first."""
    if status is not None:
        _validate_status(status)
    if kind is not None and kind not in VALID_SOURCE_KINDS:
        raise WorkitemsStoreError(
            "invalid_source", f"kind must be one of {sorted(VALID_SOURCE_KINDS)}."
        )
    if assignee is not None:
        _validate_safe_key(assignee, "assignee")

    items = _read_document(workitems_path)
    out: list[dict] = []
    for record in items.values():
        if not isinstance(record, dict) or not _visible(record, include_deleted):
            continue
        if status is not None and record.get("status") != status:
            continue
        if kind is not None:
            record_kind = "github" if is_adopted(record) else "local"
            if record_kind != kind:
                continue
        if assignee is not None and record.get("assignee") != assignee:
            continue
        out.append(record)
    return out


def update_workitem(
    workitems_path: Path,
    workitem_id: str,
    *,
    title: Optional[str] = None,
    labels: Optional[list[str]] = None,
    body: Any = UNSET,
    status: Optional[str] = None,
    state_reason: Any = UNSET,
    assignee: Any = UNSET,
    todo_ids: Optional[list[str]] = None,
    session_ids: Optional[list[str]] = None,
) -> dict:
    """Update fields on an existing workitem. Returns the updated record."""
    if title is not None:
        _validate_content_length(title, field="title", limit=_TITLE_LIMIT)
    if status is not None:
        _validate_status(status)
    if not isinstance(body, _Unset):
        _validate_optional_text(body, field="body", limit=_BODY_LIMIT)
    if not isinstance(state_reason, _Unset):
        _validate_state_reason(state_reason)
    if not isinstance(assignee, _Unset) and assignee is not None:
        _validate_safe_key(assignee, "assignee")
    resolved_labels = None if labels is None else _validate_labels(labels)
    resolved_todo_ids = (
        None if todo_ids is None else _validate_ids(todo_ids, kind="todo_id")
    )
    resolved_session_ids = (
        None if session_ids is None else _validate_ids(session_ids, kind="session_id")
    )

    with locked(workitems_path):
        items = _read_document(workitems_path)
        record = items.get(workitem_id)
        if not isinstance(record, dict) or is_deleted(record):
            raise WorkitemsStoreError("workitem_not_found", "Workitem not found.")

        if is_adopted(record):
            _refuse_github_owned(
                {
                    "status": UNSET if status is None else status,
                    "state_reason": state_reason,
                    "body": body,
                }
            )

        # Captured before the mutation loop, because the loop writes into
        # ``record`` in place: after it, "what it was" is gone.
        previous_status = record.get("status")
        previous_assignee = record.get("assignee")

        changed = False
        for field, value in (
            ("title", title),
            ("labels", resolved_labels),
            ("status", status),
        ):
            if value is not None and record.get(field) != value:
                record[field] = value
                changed = True
        for field, value in (
            ("body", body),
            ("state_reason", state_reason),
            ("assignee", assignee),
        ):
            if not isinstance(value, _Unset) and record.get(field) != value:
                record[field] = value
                changed = True

        links = record.get("links")
        if not isinstance(links, dict):
            links = {"todo_ids": [], "session_ids": []}
            record["links"] = links
            changed = True
        for field, value in (
            ("todo_ids", resolved_todo_ids),
            ("session_ids", resolved_session_ids),
        ):
            if value is not None and links.get(field) != value:
                links[field] = value
                changed = True

        if not changed:
            # Nothing was written, so nothing is emitted either: an idempotent
            # PATCH must not mint history it did not make.
            return copy.deepcopy(record)

        stamp = _now_iso()
        record["updated_at"] = stamp
        # Re-ordered because a write can *introduce* a key: an adopted record
        # stored before amendment 33 carries no ``assignee``, and assigning it
        # would otherwise append the key after ``deleted_by``.
        record = _ordered(record)
        items[workitem_id] = record
        _write(workitems_path, items)
        updated = copy.deepcopy(record)

    # Outside the lock (:func:`_emit`).
    events: list[Event] = []
    new_status = updated.get("status")
    if new_status != previous_status:
        if new_status == "closed":
            events.append(
                WorkitemEvent(
                    ts=stamp,
                    action="closed",
                    workitem_id=workitem_id,
                    state_reason=updated.get("state_reason"),
                )
            )
        elif new_status == "open":
            events.append(
                WorkitemEvent(
                    ts=stamp, action="reopened", workitem_id=workitem_id,
                )
            )
    new_assignee = updated.get("assignee")
    if new_assignee != previous_assignee:
        events.append(
            WorkitemEvent(
                ts=stamp,
                action="assigned",
                workitem_id=workitem_id,
                assignee=new_assignee if isinstance(new_assignee, str) else None,
            )
        )
    _emit(workitems_path, events)
    return updated


def delete_workitem(
    workitems_path: Path, workitem_id: str, *, deleted_by: Optional[str] = None
) -> bool:
    """
    Soft-delete a workitem. ``True`` if this call tombstoned it, ``False`` if
    there was nothing to delete — idempotent, so a second DELETE of the same id
    is a no-op rather than a 404.
    """
    if deleted_by is not None:
        _validate_safe_key(deleted_by, "runtime")

    with locked(workitems_path):
        items = _read_document(workitems_path)
        record = items.get(workitem_id)
        if not isinstance(record, dict) or is_deleted(record):
            return False
        stamp = _now_iso()
        record["deleted_at"] = stamp
        record["deleted_by"] = deleted_by
        record["updated_at"] = stamp
        _write(workitems_path, items)

    # The tombstone is the event; the ``created`` line stays true and is never
    # retracted, because the workitem *was* created.
    _emit(workitems_path, [
        WorkitemEvent(
            ts=stamp,
            runtime=deleted_by or "",
            action="deleted",
            workitem_id=workitem_id,
        )
    ])
    return True


# ── Adoption ───────────────────────────────────────────────────────────────
# Adoption is a **state transition, not a field edit**, which is why it is not
# ``update_workitem`` (§13, amendment 8).


def _adopted_node_id(record: object) -> Optional[str]:
    """The ``node_id`` a record is adopted to, or ``None`` if it is local."""
    if not is_adopted(record):
        return None
    ref = record.get("source", {}).get("github")  # type: ignore[union-attr]
    node_id = ref.get("node_id") if isinstance(ref, dict) else None
    return node_id if isinstance(node_id, str) and node_id else None


def _find_adoption(items: dict, node_id: str) -> Optional[dict]:
    """The live record already adopted to ``node_id``, if there is one."""
    for record in items.values():
        if not isinstance(record, dict) or is_deleted(record):
            continue
        if _adopted_node_id(record) == node_id:
            return record
    return None


def adopt_workitem(
    workitems_path: Path,
    *,
    runtime: str,
    github: dict,
    title: Optional[str] = None,
    labels: Optional[list[str]] = None,
    workitem_id: Optional[str] = None,
    todo_ids: Optional[list[str]] = None,
    session_ids: Optional[list[str]] = None,
) -> tuple[dict, bool]:
    """Track a GitHub issue as a workitem. Returns ``(record, created)``."""
    _validate_safe_key(runtime, "runtime")
    source = _validate_source({"kind": "github", "github": github})
    node_id = source["github"]["node_id"]
    if title is not None:
        _validate_content_length(title, field="title", limit=_TITLE_LIMIT)
    resolved_labels = None if labels is None else _validate_labels(labels)
    resolved_todo_ids = (
        None if todo_ids is None else _validate_ids(todo_ids, kind="todo_id")
    )
    resolved_session_ids = (
        None if session_ids is None else _validate_ids(session_ids, kind="session_id")
    )

    events: list[Event] = []
    with locked(workitems_path):
        items = _read_document(workitems_path)
        already = _find_adoption(items, node_id)

        if workitem_id is None:
            if already is not None:
                # Idempotent: the issue is already tracked.
                return copy.deepcopy(already), False
            if title is None:
                raise WorkitemsStoreError(
                    "invalid_value",
                    "title is required when adopting an issue into a new "
                    "workitem: it is the snapshot that keeps the item readable "
                    "when the mirror is gone.",
                )
            record = _new_record(
                runtime=runtime,
                title=title,
                body=None,
                labels=resolved_labels or [],
                status=_DEFAULT_STATUS,
                state_reason=None,
                assignee=None,
                source=source,
                links={
                    "todo_ids": resolved_todo_ids or [],
                    "session_ids": resolved_session_ids or [],
                },
            )
            items[record["id"]] = record
            _write(workitems_path, items)
            result, created = copy.deepcopy(record), True
            repo, number = _issue_ref(source)
            stamp = record["created_at"]
            # Two lines, because two things happened: a record came into
            # existence and it was pointed at an issue.
            events = [
                WorkitemEvent(
                    ts=stamp, runtime=runtime, action="created",
                    workitem_id=record["id"], title=title, kind="github",
                ),
                WorkitemEvent(
                    ts=stamp, runtime=runtime, action="adopted",
                    workitem_id=record["id"], title=title,
                    repo=repo, number=number,
                ),
            ]
        else:
            record = items.get(workitem_id)
            if not isinstance(record, dict) or is_deleted(record):
                raise WorkitemsStoreError(
                    "workitem_not_found", "Workitem not found."
                )

            current = _adopted_node_id(record)
            if current == node_id:
                return copy.deepcopy(record), False
            if current is not None:
                raise WorkitemsStoreError(
                    "already_adopted",
                    f"workitem {workitem_id} already tracks a different GitHub "
                    f"issue. Un-adopt it first (DELETE its /adoption) rather "
                    f"than re-pointing it, so the record never silently changes "
                    f"which issue it stands for.",
                )
            if already is not None:
                raise WorkitemsStoreError(
                    "already_adopted",
                    f"that issue is already tracked by workitem "
                    f"{already.get('id')}. Two workitems for one issue would "
                    f"each claim to be the work, and neither could be "
                    f"authoritative.",
                )

            updated = dict(record)
            for field in GITHUB_OWNED_FIELDS:
                updated.pop(field, None)
            updated["source"] = source
            if title is not None:
                updated["title"] = title
            if resolved_labels is not None:
                updated["labels"] = resolved_labels
            links = updated.get("links")
            links = dict(links) if isinstance(links, dict) else {
                "todo_ids": [], "session_ids": []
            }
            if resolved_todo_ids is not None:
                links["todo_ids"] = resolved_todo_ids
            if resolved_session_ids is not None:
                links["session_ids"] = resolved_session_ids
            updated["links"] = links
            stamp = _now_iso()
            updated["updated_at"] = stamp

            items[workitem_id] = _ordered(updated)
            _write(workitems_path, items)
            result, created = copy.deepcopy(items[workitem_id]), False
            repo, number = _issue_ref(source)
            # No ``created`` line here: the record already existed, and this
            # call is the moment it started standing for the issue.
            events = [
                WorkitemEvent(
                    ts=stamp, runtime=runtime, action="adopted",
                    workitem_id=workitem_id, title=result.get("title"),
                    repo=repo, number=number,
                )
            ]

    _emit(workitems_path, events)
    return result, created


def unadopt_workitem(
    workitems_path: Path,
    workitem_id: str,
    *,
    status: Optional[str] = None,
    state_reason: Optional[str] = None,
    assignee: Any = UNSET,
    body: Optional[str] = None,
) -> dict:
    """Stop mirroring a GitHub issue, keeping the workitem. Returns the record."""
    resolved_status = _DEFAULT_STATUS if status is None else status
    _validate_status(resolved_status)
    _validate_state_reason(state_reason)
    if not isinstance(assignee, _Unset) and assignee is not None:
        _validate_safe_key(assignee, "assignee")
    _validate_optional_text(body, field="body", limit=_BODY_LIMIT)

    with locked(workitems_path):
        items = _read_document(workitems_path)
        record = items.get(workitem_id)
        if not isinstance(record, dict) or is_deleted(record):
            raise WorkitemsStoreError("workitem_not_found", "Workitem not found.")
        if not is_adopted(record):
            return copy.deepcopy(record)

        updated = dict(record)
        updated["source"] = {"kind": "local"}
        updated["status"] = resolved_status
        updated["state_reason"] = state_reason
        updated["assignee"] = (
            record.get("assignee") if isinstance(assignee, _Unset) else assignee
        )
        updated["body"] = body
        updated["updated_at"] = _now_iso()

        items[workitem_id] = _ordered(updated)
        _write(workitems_path, items)
        return copy.deepcopy(items[workitem_id])
