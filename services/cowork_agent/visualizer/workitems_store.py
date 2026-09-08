"""CRUD over ``<project>/.xo/workitems.json`` — the durable work surface.

A **workitem** is a coarse, durable unit of work with an owner and a
lifecycle, possibly mirroring a GitHub issue. A **todo** is an agent's
step list inside one session. They are distinct records, joined by
``links.todo_ids`` (workitems-plan §9, D3), and this module is the
sibling of :mod:`~services.cowork_agent.visualizer.todos_store`: same
``runtime`` vocabulary, same tombstone semantics, same
``StoreError(code, message)`` shape, so an agent that can drive todos
can drive workitems without learning a second dialect.

Three things are load-bearing here and none of them is obvious.

**1. This file must stay quiet, because it syncs.** ``.xo/`` is the
synced tier (rule R-TIER); a document that cached what GitHub owns
would churn at GitHub's rate inside the tier that is snapshot-restored
wholesale. So for an adopted item this store writes the *adoption
record* and its local annotations, and nothing else: ``title`` and
``labels`` are snapshotted once, at adoption, as the readable fallback
when the mirror is gone (labels are fetched lazily then, because the
poll deliberately does not carry them — §6.2), and ``status``,
``state_reason`` and ``body`` are **omitted entirely** (plan §5.3). A
stale title is a cosmetic inaccuracy; a stale ``closed`` is a false
statement about whether the work is done. Absent beats wrong. Writing
one of those three for an adopted item is refused
(``github_authoritative``) rather than quietly accepted.

``assignee`` **is not one of them, and used to be.** D1 put assignment
in GitHub — ``PUT …/assignee`` on an adopted item issued a ``PATCH``
against the issue and this store refused the field. That decision was
reversed (§13, amendment 33): GitHub is now **read-only** to this
system, and assignment is a *local annotation* carried here for every
workitem, adopted or not. It stays quiet because it is written when a
human assigns, never when GitHub changes. The cost of the reversal,
stated where the field lives: ``.xo/`` does not continuously sync
(restore is a wholesale force-replace), so an assignment is visible
only inside the Space that made it.

**2. There is no ``in_progress``.** ``status`` is ``open`` | ``closed``
— GitHub's own two values, so the two can never disagree (§5.4, D7).
"In progress" is derived at read time from a live agent claim and is
never stored: a stored flag lies the moment the process holding it
dies. "Cancelled" is not invented either — it is ``closed`` +
``state_reason: not_planned``.

**3. A corrupt document raises; it is never treated as empty.** This is
the one hard requirement of the task (plan §10, W2) and it is a
deliberate divergence from ``todos_store._read_sessions``, which maps an
unparseable file to "no sessions" so the next create silently discards
whatever the file held — catalogued as **O-E** in ``docs/OUTSTANDING.md``.
"I own the whole document, so there is nothing in it worth preserving"
is sound only when the document is re-derivable. This one is not: it is
authored state in the synced tier, and the corrupt bytes may be the only
copy. So every read classifies the file and refuses, and the write goes
through :func:`~services.cowork_agent.visualizer.atomic_write.write_json_owned`
— the merge primitive, which raises
:class:`~...atomic_write.CorruptDocumentError` rather than repairing by
overwriting — even though full ownership would have permitted the
cheaper :func:`...write_json_atomic_if_changed`. Two independent
refusals, one of them enforced by shared machinery.

:func:`~services.cowork_agent.visualizer.flock.locked` guards the
read-modify-write: one writer, but concurrent *requests* (FastAPI's
thread pool, or a second uvicorn worker) are still two writers.

**Adoption is here; the fetch that feeds it is not.**
:func:`adopt_workitem` and :func:`unadopt_workitem` are state
transitions rather than field edits — ``source.kind`` decides which
fields the record may carry at all — so they live with the storage
they have to keep valid. The issue reference and the label snapshot
are the caller's to supply; this module makes no network call, and
the GitHub mirror is a different document with a different writer.

**This module is also the workitem event source** (plan §8, W10). Every
write path that changes a record's lifecycle appends a ``workitem.*``
line to the runtime ``timeline.jsonl``, for the reason T7 made the todos
API the todo event source: the event is emitted where the *write*
happens, so it cannot be missed by a caller that reached the store down
a different route, and it exists exactly when the record does. Emission
is best-effort by construction — see :func:`_emit`.

**Not here, on purpose.** The mirror and the read-time projection are
W4–W6 and ``visualizer/workitem_projection.py``; claims and derived
``in_progress`` are W7b (``visualizer/workitem_claims.py``, which emits
``workitem.claimed`` / ``.released`` the same way).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_owned,
)
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.ingest.events import Event, WorkitemEvent
from services.cowork_agent.visualizer.sinks import timeline


logger = logging.getLogger(__name__)


#: On-disk revision of ``workitems.json`` (plan §5.1).
WORKITEMS_SCHEMA = 1

#: Value written into the document's ``$schema`` key. It is the schema's
#: own ``$id`` (visualizer/schema/workitems.schema.json), NOT a filesystem
#: path — the ``.xo/schema/`` pointer syncplan T16 removed was dangling on
#: every document that carried it.
_SCHEMA_REF = "xo/workitems.schema.json"

#: The top-level keys this store owns. Declared for
#: :func:`write_json_owned`, which is what makes an undeclared key a
#: ``ValueError`` in tests rather than a silent widening of ownership.
_OWNS: frozenset[str] = frozenset({"$schema", "schema", "updated_at", "items"})

#: ``open`` | ``closed`` and nothing else — GitHub's vocabulary (D7).
VALID_STATUSES: frozenset[str] = frozenset({"open", "closed"})

#: GitHub's ``state_reason`` values. ``None`` clears it.
VALID_STATE_REASONS: frozenset[str] = frozenset(
    {"completed", "not_planned", "reopened"}
)

VALID_SOURCE_KINDS: frozenset[str] = frozenset({"local", "github"})

#: The fields GitHub is authoritative for. Never stored for an adopted
#: item (§5.3) — see the module docstring.
#:
#: ``assignee`` was the fourth entry and is deliberately **not** here any
#: more (§13, amendment 33 — the reversal of D1). Nothing in this system
#: writes to GitHub, so an assignee is ours to record; an adopted record
#: may carry one exactly like a local one, and ``workitems.schema.json``
#: permits it on both. The other three stay GitHub's.
GITHUB_OWNED_FIELDS: tuple[str, ...] = ("status", "state_reason", "body")

_DEFAULT_STATUS = "open"

# Same charset as ``todos_store``: permissive enough for realistic
# adapter keys and composite session ids, restrictive enough to reject
# path traversal and anything that could turn a synced document into a
# channel for arbitrary caller text.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")

# A label is human text (GitHub allows spaces, colons, emoji), so only
# control characters are excluded.
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
    """Sentinel: "the caller did not supply this field".

    Needed because ``None`` is a *value* for ``body``, ``state_reason``
    and ``assignee`` — clearing an assignee and not mentioning it are
    different requests, and a PATCH that could not express the first
    would leave no way to un-assign.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


UNSET = _Unset()


class WorkitemsStoreError(Exception):
    """Base for all store failures. ``code`` is the BFF error code the
    route maps to ``detail.code`` — same shape as ``TodosStoreError``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Lifecycle events (plan §8, W10) ────────────────────────────────────────
#
# The store is the event source, not the routes. Five entry points reach
# these records — the five CRUD routes, adoption, un-adoption, the
# assignee endpoint and the implicit release on close — and an emit
# bolted onto each is an emit that one of them will eventually be added
# without. Emitting from the write means a ``workitem.*`` line exists
# exactly when the write that caused it committed.
#
# The events are rendered by ``sinks/timeline.py`` against the closed
# vocabulary in ``ingest/events.WORKITEM_ACTIONS``; this module never
# spells a ``type`` string, so it cannot invent one the schema has no
# branch for.


def emit_workitem_events(project_id: str, events: Iterable[Event]) -> None:
    """Append workitem lifecycle events to a project's timeline.

    Public because one caller has no store write to hang an event on:
    assigning an *adopted* workitem writes to GitHub and never touches
    ``.xo/`` (§5.3), so the BFF route is the only place that knows it
    happened. It is addressed by ``project_id`` rather than by path so
    that caller does not have to resolve one — the tier decision stays
    inside ``project_layout``, where the chokepoint guard wants it.

    **Never raises**, and that is load-bearing: see :func:`_emit`.
    """
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
    """Fan lifecycle events to the timeline. **Never fails the write.**

    Two independent reasons the log cannot be allowed to fail the
    operation, and they compound:

    1. The record is already on disk by the time this is called. Raising
       here would turn a workitem that exists into a ``500``, and the
       caller would then retry a create that already succeeded.
    2. The timeline is derived, append-only history. Losing a line costs
       one entry in a log; losing ``workitems.json`` costs authored state
       in the synced tier.

    So :func:`timeline.apply_quiet` swallows and logs every failure, and
    the resolution of the runtime directory above it does too. Callers
    invoke this *after* releasing the store's lock — the append ends in
    an ``fsync``, and holding a document lock across one is how a slow
    disk becomes a slow API.

    ``workitems_path`` is ``<project>/.xo/workitems.json``, so the
    project folder is its grandparent — the same derivation
    ``todos_store`` uses, and the reason the timeline lands in the
    *runtime* tier while the record it describes stays in the synced one.
    """
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
    """Whether a workitem record carries a tombstone.

    One predicate so "deleted" means the same thing to the store, the
    routes and the tests. Absent / ``null`` ``deleted_at`` is alive.
    """
    return isinstance(item, dict) and item.get("deleted_at") is not None


def is_adopted(item: object) -> bool:
    """Whether the record mirrors a GitHub issue (``source.kind == "github"``).

    The single predicate for the §5.3 asymmetry: everything that is
    refused for an adopted item is refused through this.
    """
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
    """``None`` or a string within ``limit``. Unlike
    :func:`_validate_content_length` an empty string is allowed — an
    emptied body is a real edit, not a malformed one."""
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
    """Validate and normalise ``source.github``.

    ``node_id`` is required alongside ``number`` because it survives a
    repo rename or transfer, which ``repo``/``number`` do not — it is the
    match key against the mirror (§5.1).
    """
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
    """Validate and normalise the ``source`` block.

    ``None`` means a plain local item — the common case, and the one the
    CRUD routes (§7.1) always take.
    """
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
    """The O-E refusal, in one place.

    Deliberately *not* ``(None, {})``. An unreadable ``workitems.json``
    is authored state in the synced tier and the bytes on disk may be
    the only copy; treating it as empty would make the next create
    delete every workitem the file held. Refuse and say what to do.
    """
    return WorkitemsStoreError(
        "corrupt_document",
        f"{path} is not a readable workitems document ({reason}); refusing to "
        f"read or write it. Treating it as empty would discard every workitem "
        f"it holds (docs/OUTSTANDING.md O-E). Repair or move the file.",
    )


def _read_document(path: Path) -> dict:
    """Return the document's items map, deep-copied.

    An **absent** file is ``{}`` — nothing has been written yet, which is
    a legitimate state and the only one that reads as empty.

    Everything else that is not a well-formed workitems document raises
    :class:`WorkitemsStoreError` with code ``corrupt_document``:
    unreadable bytes, an empty file (what a truncated non-atomic write
    leaves behind), invalid JSON, a non-object at the top level, a
    missing or non-object ``items`` map, a non-object record, a key that
    is not a canonical UUID4, or a record whose ``id`` disagrees with the
    key it is filed under.

    That last check is the O-C lesson made executable: the key is only
    trustworthy if it is unique by construction, so the store never
    *derives* identity from the key — it verifies the two agree and
    refuses if they do not, rather than silently preferring one.

    A ``schema`` the store does not know is refused too (code
    ``unsupported_schema``): a document written by a newer Space and
    restored here carries keys this revision would drop on the next
    write, and dropping them silently is the same defect wearing a
    different hat.

    Unlike ``todos_store._read_sessions`` this hands back no comparison
    baseline, because :func:`_write` needs none: ``write_json_owned``
    re-reads the file itself, and that read *is* the merge — a cached
    baseline would resurrect keys another writer deleted. The map is
    still deep-copied, so mutating it cannot alias the parsed document
    the write primitive will compare against.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise _corrupt(path, f"unreadable: {exc}") from exc
    if not text.strip():
        raise _corrupt(path, "empty file")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _corrupt(path, f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _corrupt(path, f"top-level {type(parsed).__name__}, expected object")

    version = parsed.get("schema")
    if version is not None and (
        isinstance(version, bool) or version != WORKITEMS_SCHEMA
    ):
        raise WorkitemsStoreError(
            "unsupported_schema",
            f"{path} declares schema {version!r}; this store writes schema "
            f"{WORKITEMS_SCHEMA} and will not rewrite a document it cannot "
            f"fully represent.",
        )

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
    """Persist the items map, refusing a document that went unreadable.

    ``write_json_owned`` rather than ``write_json_atomic_if_changed``.
    The store does own the whole document, which would permit the
    cheaper primitive — but that one *repairs* a corrupt file by
    overwriting it, and this file is the durable, synced record of what
    someone chose to work on. Paying one extra JSON parse per write buys
    the shared machinery's refusal (:class:`CorruptDocumentError`) as a
    second, independent guard behind :func:`_read_document`'s.

    Top-level ``updated_at`` is volatile by default, so a call that
    changes no workitem writes nothing at all.
    """
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
#: and the plan's §5.1 example. It exists because :func:`adopt_workitem` and
#: :func:`unadopt_workitem` *add and remove* keys on a record that is already
#: on disk: without one place that says what the order is, a record that had
#: been adopted and un-adopted would serialise differently from one created
#: local, and every diff of the synced document would carry that noise.
_KEY_ORDER: tuple[str, ...] = (
    "id", "title", "body", "labels", "status", "state_reason", "source",
    "assignee", "links", "created_at", "updated_at", "created_by",
    "deleted_at", "deleted_by",
)


def _ordered(record: dict) -> dict:
    """``record`` with its keys in :data:`_KEY_ORDER`, extras kept at the end.

    Extras are *kept*, not dropped. This store refuses a document it cannot
    fully represent (``unsupported_schema``) rather than silently rewriting
    it, and quietly discarding an unknown key here would be that same defect
    reached by a different road.
    """
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
    """One freshly-minted record, with a server-minted UUID4 ``id``.

    The §5.3 asymmetry lives here and nowhere else: an adopted record simply
    does not carry ``status``, ``state_reason`` or ``body``. They are absent
    rather than ``null`` because the schema *forbids* them for
    ``source.kind == "github"`` — "unknown, ask GitHub" and "known to be
    empty" are different claims, and only absence can make the first one.

    ``assignee`` is on the other side of that line, and the placement is the
    point of amendment 33: it is written for **both** kinds, ``null`` when
    nobody is assigned. Nothing writes it to GitHub any more, so there is no
    upstream value it could go stale against — it is simply ours.
    """
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
    """Create a workitem. Returns the stored record.

    ``id`` is a server-minted **UUID4**, not a short id: the workspace
    rollup (§7.3) is a cross-project query, and O-C is exactly such a
    query losing rows because a key was a constant rather than an
    identifier. Unique by construction is the requirement; a UUID4 meets
    it without depending on ``pid``, which does not exist during the
    pre-mint window.

    ``source=None`` (the default) creates a **local** item: it carries
    ``status`` (``open`` unless told otherwise), ``state_reason``,
    ``assignee`` and ``body``. Passing
    ``source={"kind": "github", "github": {...}}`` records an
    **adoption**: ``title`` is snapshotted once as the readable fallback
    and the GitHub-owned fields are omitted from the record entirely —
    supplying one raises ``github_authoritative`` rather than storing a
    value that will be wrong within the hour (§5.3). ``assignee`` is
    accepted for both kinds: it is a local annotation now, not GitHub's
    (§13, amendment 33).

    Raises :class:`WorkitemsStoreError` on any invalid input, and on a
    ``workitems.json`` that exists but cannot be read — a corrupt
    document is never treated as an empty one (O-E).
    """
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

    # ``is None`` rather than ``or``: an empty status is a malformed
    # request, not an unstated one, and must not quietly become "open".
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

    # ``created`` and, for an item born adopted, ``adopted``: a record
    # that starts life mirroring an issue did both in one call, and a
    # reader of the log should not have to infer the second from a
    # ``kind`` field on the first.
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
    """Return the record, or ``None`` if there is no such workitem.

    A tombstoned item is *not* a match unless ``include_deleted`` — the
    caller asked for a workitem, and a deleted one is history.
    """
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
    """Every workitem, in creation order, oldest first.

    A **list**, never the raw map — §7.3's rollup unions these across
    projects, and a list has no union key, so the O-C collision class
    cannot occur there at all.

    The ``status`` filter reads what is *stored*, so an adopted item
    never matches it: it stores no status, by design. Filtering an
    adopted item on state is the read-time projection's job (§5.3, W7),
    which joins the mirror — this store deliberately cannot answer it,
    rather than answering it wrong.

    ``assignee`` is different since amendment 33: it *is* stored, for
    both kinds, so this filter answers for an adopted item too. What it
    still cannot see is GitHub's own assignees, which live in the mirror
    and are information rather than assignment — the rollup (§7.3) is
    where the two are considered together.
    """
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
    """Update fields on an existing workitem. Returns the updated record.

    ``title``, ``labels``, ``status``, ``todo_ids`` and ``session_ids``
    take ``None`` to mean "not supplied" — they are not nullable, so the
    two readings cannot collide. ``body``, ``state_reason`` and
    ``assignee`` are nullable and therefore take :data:`UNSET`: passing
    ``None`` **clears** them, which is how an item is un-assigned or
    reopened.

    For an adopted item the GitHub-owned fields raise
    ``github_authoritative`` (§5.3): closing an adopted workitem means
    closing the issue, not writing ``closed`` into a file GitHub does
    not read. ``assignee`` is **not** among them since amendment 33 —
    assigning an adopted workitem writes here, like everything else.

    Raises ``workitem_not_found`` if the id is absent or names a
    tombstone — a deleted workitem is not editable, which is what stops
    it coming back to life.

    A call that changes nothing writes nothing and stamps nothing: an
    idempotent PATCH leaves the document byte-identical.
    """
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

        # Captured before the mutation loop, because the loop writes
        # into ``record`` in place: after it, "what it was" is gone.
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
            # Nothing was written, so nothing is emitted either: an
            # idempotent PATCH must not mint history it did not make.
            return copy.deepcopy(record)

        stamp = _now_iso()
        record["updated_at"] = stamp
        # Re-ordered because a write can *introduce* a key: an adopted
        # record stored before amendment 33 carries no ``assignee``, and
        # assigning it would otherwise append the key after ``deleted_by``.
        # ``_KEY_ORDER`` exists precisely so the synced document does not
        # carry that kind of diff noise.
        record = _ordered(record)
        items[workitem_id] = record
        _write(workitems_path, items)
        updated = copy.deepcopy(record)

    # Outside the lock (:func:`_emit`). Only *transitions* are events:
    # a title or a label edit has no type in §8 and inventing one would
    # put a vocabulary in the log that nothing declares.
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
    """Soft-delete a workitem. ``True`` if this call tombstoned it,
    ``False`` if there was nothing to delete — idempotent, so a second
    DELETE of the same id is a no-op rather than a 404.

    The record is never removed (plan §5.1, syncplan §5.5): ``deleted_at``
    and ``deleted_by`` are set alongside the unchanged ``status``, so
    "we decided not to do this" (``closed`` + ``not_planned``) and "this
    should not have existed" stay distinguishable forever.

    ``deleted_by`` carries the calling **runtime** — the same vocabulary
    ``create_workitem`` requires, validated against the same charset,
    because it is persisted into a synced document and may not become a
    channel for arbitrary caller text. ``None`` when the caller did not
    say; an unattributed tombstone is still a tombstone.
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

    # The tombstone is the event; the ``created`` line stays true and is
    # never retracted, because the workitem *was* created. ``runtime``
    # carries ``deleted_by`` when the caller attributed the delete, and
    # is simply absent when it did not — an unattributed tombstone is
    # still a tombstone (and so is an unattributed line).
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
#
# Adoption is a **state transition, not a field edit**, which is why it is not
# ``update_workitem`` (§13, amendment 8). ``source.kind`` decides which fields
# the record may even carry: ``workitems.schema.json`` forbids ``status``,
# ``state_reason`` and ``body`` on an adopted item, and *requires* ``status``
# on a local one. So flipping the kind has to drop three keys in one direction
# and materialise them in the other, and a PATCH that set ``source`` alone
# would leave the record invalid whichever way it went. ``assignee`` is the
# one field that crosses unchanged in both directions (amendment 33): the
# schema permits it on either kind, because it is ours and not GitHub's.
#
# What adoption writes is the §5.1 adoption record: the issue reference, plus
# ``title`` and ``labels`` as a **one-time snapshot** that is never refreshed.
# That pair is the fallback that makes a stale item readable — without it an
# adopted item whose issue has left the mirror renders as ``repo#42``, which
# is not a work item anyone can act on. It stays cheap because adoption is a
# human act, so the synced tier still changes only when a human does
# something; a GitHub rename does not touch this file.
#
# Neither function makes a network call. The issue reference and the label
# snapshot are the caller's to supply — the route fetches them once, at
# adoption, which is what keeps the mirror single-writer (§5.2, amendment 2).


def _adopted_node_id(record: object) -> Optional[str]:
    """The ``node_id`` a record is adopted to, or ``None`` if it is local.

    Matching is on ``node_id`` and never on ``repo``/``number``: the node id
    survives a repository rename or transfer, and those two do not (§5.1).
    """
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
    """Track a GitHub issue as a workitem. Returns ``(record, created)``.

    Two entry points, one function, because the choice between them has to be
    made **inside the lock**:

    * ``workitem_id=None`` — the ordinary case. A new adopted record is minted
      for the issue, *unless* a live record already tracks that ``node_id``,
      in which case that record comes back with ``created=False``. Adoption is
      a ``POST`` a UI can double-fire, and "look it up, then create it" from
      the route would be a read-modify-write across two calls — the way one
      issue ends up with two workitems and the surface starts lying about how
      much work there is.
    * ``workitem_id`` supplied — an existing **local** workitem starts
      mirroring the issue, keeping its id, its ``links`` and its history. This
      is not D8's promotion, which is the other direction and is what this
      plan refuses: nothing is created on GitHub, and the issue being adopted
      already exists and was already public.

    The transition **drops** ``status``, ``state_reason`` and ``body``. That
    is a deliberate loss of local edits, not an oversight: those three are
    GitHub's for an adopted item, and keeping the old values would leave the
    record asserting a state nothing maintains. The title and labels the
    caller passes replace the local ones for the same reason — the record now
    stands for the issue.

    ``assignee`` **survives** the transition (amendment 33). It is a local
    annotation and adoption does not change who this Space decided owes the
    work; dropping it would silently un-assign somebody for adopting the
    issue their work was already about.

    Raises :class:`WorkitemsStoreError`: ``invalid_source`` for a malformed
    reference, ``workitem_not_found`` for an absent or tombstoned id,
    ``already_adopted`` when the target already tracks a *different* issue or
    the issue is already tracked by a different workitem, and the usual
    ``corrupt_document`` / ``unsupported_schema`` refusals.
    """
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
                # Idempotent: the issue is already tracked. Returning the
                # record rather than a second one is what makes a double-fired
                # adopt harmless — and, since nothing was written, it emits
                # nothing: a re-fired POST must not mint a second history.
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
            # existence and it was pointed at an issue. A reader should
            # not have to infer the second from a ``kind`` field on the
            # first, and the ``adopted`` line is what makes "when did we
            # start tracking this issue" a query rather than a guess.
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
            # No ``created`` line here: the record already existed, and
            # this call is the moment it started standing for the issue.
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
    """Stop mirroring a GitHub issue, keeping the workitem. Returns the record.

    The mirror image of :func:`adopt_workitem`, and the reason neither is a
    field edit: an adopted record carries **no** ``status``, and the schema
    *requires* one on a local record. So un-adopting has to materialise the
    GitHub-owned fields in the same write that drops ``source.github``, or it
    leaves a document that no longer validates.

    The caller supplies what to materialise, because the store reads no
    runtime state: it cannot see the mirror, and inventing a value would be
    the stale ``closed`` §5.3 forbids. The route passes the state the
    projection was last serving, so the item looks the same to a reader across
    the transition; ``status`` falls back to ``open`` — the least-committal
    value, which keeps the row visible and actionable rather than reading as
    work someone finished.

    ``assignee`` is **kept** by default (:data:`UNSET`), which is the reversal
    of D1 arriving here: it was never GitHub's to materialise, it is a local
    annotation the record already carries, and un-adopting an issue is not a
    statement about who owes the work. Passing ``None`` clears it explicitly;
    passing a name replaces it.

    Idempotent: un-adopting an already-local workitem returns it unchanged and
    writes nothing. ``workitem_not_found`` for an absent or tombstoned id.
    """
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
