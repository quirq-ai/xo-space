"""CRUD over ``<project>/.xo/peers.json`` — the collaborator roster.

A **peer** is a human this project is shared with, carrying a ``role``
and the moment they were added. The document answers exactly one
question — *who is on this project now* — and
``peers.schema.json`` says so in as many words: "Roster of human
collaborators. Empty list = solo project. Sync state lives separately in
sync.json."

This module is the sibling of
:mod:`~services.cowork_agent.visualizer.workitems_store`: same
``flock.locked`` read-modify-write, same
:func:`~...atomic_write.write_json_owned` refusal, same
``StoreError(code, message)`` shape, same ``invalid_*`` vocabulary. An
agent that can drive workitems can drive peers without learning a second
dialect. Four things differ, and every one of them is a decision rather
than an omission.

**1. The identity is ``user_id``, and there is no other one.** The schema
gives a peer no id field and stores ``peers`` as an *array*, so the
identity is the ``user_id`` itself — it is the path segment, the match
key, and the thing that makes the roster a set. Two records for one
``user_id`` therefore have no single answer to "what is Ada's role", and
:func:`_read_document` refuses such a document rather than picking one.
``user_id`` is immutable: changing it is a delete plus a create, not a
:func:`update_peer`.

**2. The charset is the assignee charset, deliberately.** ``user_id``
validates against :data:`_SAFE_KEY_RE`, byte-for-byte the pattern
``workitems_store`` applies to ``assignee`` (and ``todos_store`` to
``runtime``). That is the whole point of a roster: a ``user_id`` that is
a legal peer must always be a legal workitem ``assignee``, or the two
surfaces would disagree about who a person is. The regex is duplicated
here rather than imported because that is this tree's convention — three
stores already carry their own copy — and ``tests/test_peers_store.py``
pins the four together, both as strings and behaviourally, so the copy
cannot drift silently.

**3. Removing a peer is a hard delete. There is no tombstone.** This
diverges from todos and workitems on purpose, for two reasons that
compound:

* The schema is ``additionalProperties: false`` and declares no
  ``deleted_at`` / ``deleted_by``. Tombstoning would mean changing a
  document that already **ships in the project template**, so every
  scaffolded project and every restored snapshot would have to be
  migrated to record something nobody asked for.
* More importantly, ``peers.json`` is in the **synced** tier (rule
  R-TIER). A removed collaborator kept as a tombstone would travel to
  every Space this project ever reaches, carrying "this person used to
  have access" forever. That is a privacy problem wearing a history
  feature's clothes. A roster answers "who is on this now"; the record of
  who *was* is not this document's job, and inventing it here would put
  it in the one tier that cannot forget.

  The cost is stated rather than hidden: removing a peer really does
  destroy the fact that they were ever listed. If a project needs an
  access log, it needs an append-only log in the runtime tier, not a
  graveyard in the roster.

**4. Nothing is attributed and nothing is emitted.** A peer record has no
``created_by``-shaped field, so these functions take no ``runtime``:
accepting one would be accepting a value there is nowhere to put.
``timeline.jsonl``'s closed vocabulary has ``peer.sync.*`` and nothing
for a roster edit, so this module appends no events — writing a
``peer.sync.started`` line because somebody was added to a list would be
a false statement about a sync that never happened. Adding roster events
means adding the event types first.

**A corrupt document raises; it is never treated as empty.** This is the
same hard rule ``workitems_store`` follows and the same deliberate
divergence from ``todos_store._read_sessions``, which maps an unparseable
``todos.json`` to "no sessions" so the next write silently discards it —
catalogued as **O-E** in ``docs/OUTSTANDING.md``. It matters more here,
not less: a roster read as empty is a project that has silently forgotten
every collaborator, and the next create would write that emptiness back
over the only copy. So every entry point classifies the file and refuses,
and the write goes through :func:`write_json_owned` — the merge
primitive, which raises
:class:`~...atomic_write.CorruptDocumentError` rather than repairing by
overwriting. Two independent refusals, one of them enforced by shared
machinery.

**Not here, on purpose.** Nothing validates a workitem ``assignee``
against this roster, and nothing should without being asked: rejecting an
assignee who is not a listed peer is a behaviour change on a second
surface. Sync itself is not here either — ``peers.json`` records who the
project is shared *with*; whether and when anything was actually
exchanged belongs to the sync API.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_owned,
)
from services.cowork_agent.visualizer.flock import locked


#: On-disk revision of ``peers.json``, matching ``peers.schema.json``'s
#: ``schema: { const: 1 }``.
PEERS_SCHEMA = 1

#: Value written into the document's ``$schema`` key. It is the schema's
#: own ``$id``, NOT a filesystem path — the ``.xo/schema/`` pointer
#: syncplan T16 removed was dangling on every document that carried it.
_SCHEMA_REF = "xo/peers.schema.json"

#: The top-level keys this store owns — every key the schema declares,
#: because ``peers.schema.json`` is ``additionalProperties: false`` and
#: there is no second writer. Declared for :func:`write_json_owned`,
#: which is what makes an undeclared key a ``ValueError`` rather than a
#: silent widening of ownership.
_OWNS: frozenset[str] = frozenset({"$schema", "schema", "updated_at", "peers"})

#: The three roles ``peers.schema.json`` enumerates, and no others.
#: ``owner`` is not enforced to be unique and no role implies any
#: capability here: nothing in this system reads ``role`` to make an
#: access decision, so inventing a constraint would be inventing a
#: permission model the code does not have.
VALID_ROLES: frozenset[str] = frozenset({"owner", "collaborator", "viewer"})

# The assignee charset, byte for byte. See module docstring §2: a
# ``user_id`` that is a legal peer must always be a legal workitem
# ``assignee``, and it is also persisted into a synced document, so it
# may not become a channel for arbitrary caller text or path traversal.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")

# A label is human text (a display name may carry spaces, accents,
# punctuation), so only control characters are excluded — the same rule
# ``workitems_store`` applies to a GitHub label.
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")

_ENDPOINT_LIMIT = 2000

#: A ceiling on the roster, because this is a synced document and an
#: unbounded array is an unbounded snapshot. Far above any real team.
_MAX_PEERS = 1000

#: Canonical key order for a stored record, matching
#: ``peers.schema.json``'s own property order. It exists because
#: :func:`update_peer` can *introduce* a key — a record stored without a
#: ``label`` gains one when somebody sets it — and without one place that
#: says what the order is, two peers with identical fields would
#: serialise differently and every diff of the synced document would
#: carry that noise.
_KEY_ORDER: tuple[str, ...] = ("user_id", "role", "added_at", "endpoint", "label")


class _Unset:
    """Sentinel: "the caller did not supply this field".

    Needed because ``None`` is a *value* for ``label`` and ``endpoint`` —
    clearing a display name and not mentioning it are different requests,
    and a PATCH that could not express the first would leave no way to
    remove one.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


UNSET = _Unset()


class PeersStoreError(Exception):
    """Base for all store failures. ``code`` is the BFF error code the
    route maps to ``detail.code`` — same shape as ``WorkitemsStoreError``
    and ``TodosStoreError``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Validation ─────────────────────────────────────────────────────────────


def _validate_user_id(value: object) -> str:
    """The identity check, and the one that must agree with ``assignee``."""
    if not isinstance(value, str) or not _SAFE_KEY_RE.match(value):
        raise PeersStoreError(
            "invalid_user_id",
            "user_id must match [A-Za-z0-9_:\\-\\.] (1..200 chars) — the same "
            "charset a workitem assignee must satisfy, so a listed peer can "
            "always be assigned work.",
        )
    return value


def _validate_role(value: object) -> str:
    if value not in VALID_ROLES:
        raise PeersStoreError(
            "invalid_role", f"role must be one of {sorted(VALID_ROLES)}."
        )
    return str(value)


def _validate_label(value: object) -> Optional[str]:
    """``None`` or printable text. ``None`` is "no display name"."""
    if value is None:
        return None
    if not isinstance(value, str) or not _LABEL_RE.match(value):
        raise PeersStoreError(
            "invalid_label",
            "label must be null or 1..200 printable characters (no control "
            "characters).",
        )
    return value


def _validate_endpoint(value: object) -> Optional[str]:
    """``None`` or an ``http(s)://`` URL.

    A scheme is required rather than optional: ``endpoint`` is documented
    as a *sync endpoint URL*, and a bare host stored here would be
    something a caller has to guess a scheme for later. ``http://`` is
    allowed alongside ``https://`` because a Space on a private network
    is a real deployment, not a mistake.
    """
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not (value.startswith("https://") or value.startswith("http://"))
        or len(value) > _ENDPOINT_LIMIT
        or any(ch.isspace() for ch in value)
    ):
        raise PeersStoreError(
            "invalid_endpoint",
            f"endpoint must be null or an http(s) URL of at most "
            f"{_ENDPOINT_LIMIT} characters with no whitespace.",
        )
    return value


# ── Document I/O ───────────────────────────────────────────────────────────


def _corrupt(path: Path, reason: str) -> PeersStoreError:
    """The O-E refusal, in one place.

    Deliberately *not* an empty roster. An unreadable ``peers.json`` is
    authored state in the synced tier and the bytes on disk may be the
    only copy; treating it as empty would make the next create delete
    every collaborator the file held — and, because this document is what
    a Space consults to know who a project is shared with, it would do so
    without anything looking wrong. Refuse and say what to do.
    """
    return PeersStoreError(
        "corrupt_document",
        f"{path} is not a readable peers document ({reason}); refusing to "
        f"read or write it. Treating it as empty would discard every "
        f"collaborator it lists (docs/OUTSTANDING.md O-E). Repair or move "
        f"the file.",
    )


def _read_document(path: Path) -> tuple[Optional[str], list[dict]]:
    """Return ``(updated_at, peers)``, deep-copied.

    An **absent** file is ``(None, [])`` — nothing has been written yet,
    which is a legitimate state and the only one that reads as an empty
    roster. Note that a *present* document with ``"peers": []`` is also
    an empty roster and is perfectly valid: the schema says so, and it is
    what the project template ships.

    Everything else that is not a well-formed peers document raises
    :class:`PeersStoreError` with code ``corrupt_document``: unreadable
    bytes, an empty file (what a truncated non-atomic write leaves
    behind), invalid JSON, a non-object at the top level, a missing or
    non-array ``peers``, a non-object entry, an entry with no usable
    ``user_id``, or two entries claiming the same ``user_id``.

    That last check is this document's version of the O-C lesson. The
    array has no key to make identity unique by construction, so the
    store *verifies* it instead of assuming it: a roster with Ada twice
    has no single answer to ``GET /peers/ada`` and no single record for a
    PATCH to edit, and silently preferring the first or the last would be
    a lie either way.

    A stored ``user_id`` is checked for being a non-empty string, not for
    matching :data:`_SAFE_KEY_RE`. The schema types it as a plain string,
    so a document a newer Space wrote may legitimately carry an id this
    revision would not mint; refusing the whole roster over one such
    entry would fail closed on a valid document. Writes are held to the
    stricter charset, reads are not.

    A ``schema`` this store does not know is refused with code
    ``unsupported_schema``: a document written by a newer Space and
    restored here carries keys this revision would drop on the next
    write, and dropping them silently is O-E reached by another road.

    Like ``workitems_store`` and unlike ``todos_store``, this hands back
    no comparison baseline: :func:`_write` needs none, because
    ``write_json_owned`` re-reads the file itself and that read *is* the
    merge. The list is deep-copied so mutating it cannot alias the parsed
    document the write primitive will compare against.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (None, [])
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
    if version is not None and (isinstance(version, bool) or version != PEERS_SCHEMA):
        raise PeersStoreError(
            "unsupported_schema",
            f"{path} declares schema {version!r}; this store writes schema "
            f"{PEERS_SCHEMA} and will not rewrite a document it cannot fully "
            f"represent.",
        )

    raw = parsed.get("peers")
    if raw is None:
        raise _corrupt(path, "no peers list")
    if not isinstance(raw, list):
        raise _corrupt(path, f"peers is a {type(raw).__name__}, expected array")

    seen: set[str] = set()
    for index, record in enumerate(raw):
        if not isinstance(record, dict):
            raise _corrupt(path, f"peers[{index}] is a {type(record).__name__}")
        user_id = record.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise _corrupt(
                path, f"peers[{index}] carries user_id {user_id!r}, which is "
                      f"not a usable identity"
            )
        if user_id in seen:
            raise _corrupt(
                path,
                f"peers lists {user_id!r} more than once; the roster is a set "
                f"keyed by user_id and there is no way to choose between two "
                f"records for one person",
            )
        seen.add(user_id)

    stamp = parsed.get("updated_at")
    return (stamp if isinstance(stamp, str) else None, copy.deepcopy(raw))


def _write(path: Path, peers: list[dict]) -> None:
    """Persist the roster, refusing a document that went unreadable.

    ``write_json_owned`` rather than ``write_json_atomic_if_changed``.
    The store does own the whole document, which would permit the cheaper
    primitive — but that one *repairs* a corrupt file by overwriting it,
    and this file is the durable, synced record of who a project is
    shared with. Paying one extra JSON parse per write buys the shared
    machinery's refusal (:class:`CorruptDocumentError`) as a second,
    independent guard behind :func:`_read_document`'s.

    Top-level ``updated_at`` is volatile by default, so a call that
    changes no peer writes nothing at all and the stamp stays where the
    last real change left it — which is what makes it an accurate answer
    to "when did this roster last change" rather than "when was it last
    touched".
    """
    try:
        write_json_owned(
            path,
            owns=_OWNS,
            values={
                "$schema": _SCHEMA_REF,
                "schema": PEERS_SCHEMA,
                "updated_at": _now_iso(),
                "peers": peers,
            },
        )
    except CorruptDocumentError as exc:
        raise _corrupt(path, exc.reason) from exc


def _ordered(record: dict) -> dict:
    """``record`` with its keys in :data:`_KEY_ORDER`, extras kept at the end.

    Extras are *kept*, not dropped, exactly as in ``workitems_store``.
    ``peers.schema.json`` forbids them, so one can only arrive from a
    document this revision did not write — and quietly discarding it is
    the same silent-loss defect the ``unsupported_schema`` refusal exists
    to prevent, reached by a different road.
    """
    out = {key: record[key] for key in _KEY_ORDER if key in record}
    for key, value in record.items():
        if key not in out:
            out[key] = value
    return out


def _index_of(peers: list[dict], user_id: str) -> Optional[int]:
    """Position of ``user_id`` in the roster, or ``None``.

    Uniqueness is already guaranteed by :func:`_read_document`, so the
    first match is the only match.
    """
    for index, record in enumerate(peers):
        if isinstance(record, dict) and record.get("user_id") == user_id:
            return index
    return None


# ── CRUD ───────────────────────────────────────────────────────────────────


def create_peer(
    peers_path: Path,
    *,
    user_id: str,
    role: str,
    label: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> dict:
    """Add a peer to the roster. Returns the stored record.

    ``added_at`` is **server-set** and never editable: it records when
    this Space learned of the peer, and a caller-supplied value would
    make it a claim rather than an observation.

    **A ``user_id`` that is already listed is ``peer_exists``, not an
    upsert**, and that is the one contested decision in this module. The
    roster is a set keyed by identity, so a POST of an existing id has
    two plausible readings and they disagree about something that
    matters:

    * As an upsert it would rewrite ``role`` — quietly turning a
      ``viewer`` into an ``owner``, or an ``owner`` into a ``viewer``,
      because somebody re-ran a create. A privilege change should never
      be a side effect of an insert that the caller believed was new.
    * It would also have to decide what ``added_at`` means. Resetting it
      loses when the person actually joined; keeping it makes the "201
      Created" a lie about a record that predates the request. There is
      no answer that is true both ways.

    Refusing costs the caller nothing, because the edit they wanted is
    already expressible: :func:`update_peer` changes a role, and it says
    so. And a caller who genuinely wants "ensure listed" can read first —
    the conflict is cheap to detect and impossible to misread, which is
    the opposite of a silent overwrite.

    Raises :class:`PeersStoreError` on any invalid input, and on a
    ``peers.json`` that exists but cannot be read — a corrupt document is
    never treated as an empty roster (O-E).
    """
    resolved_user_id = _validate_user_id(user_id)
    resolved_role = _validate_role(role)
    resolved_label = _validate_label(label)
    resolved_endpoint = _validate_endpoint(endpoint)

    with locked(peers_path):
        _, peers = _read_document(peers_path)
        if _index_of(peers, resolved_user_id) is not None:
            raise PeersStoreError(
                "peer_exists",
                f"{resolved_user_id} is already on this project's roster. The "
                f"roster is a set keyed by user_id, so a create cannot also "
                f"be an edit: PATCH the peer to change their role, label or "
                f"endpoint.",
            )
        if len(peers) >= _MAX_PEERS:
            raise PeersStoreError(
                "invalid_value",
                f"this project already lists {_MAX_PEERS} peers, which is the "
                f"ceiling for a document that travels with every snapshot.",
            )
        record = _ordered(
            {
                "user_id": resolved_user_id,
                "role": resolved_role,
                "added_at": _now_iso(),
                "endpoint": resolved_endpoint,
                "label": resolved_label,
            }
        )
        peers.append(record)
        _write(peers_path, peers)
        return copy.deepcopy(record)


def read_roster(
    peers_path: Path, *, role: Optional[str] = None
) -> tuple[Optional[str], list[dict]]:
    """``(updated_at, peers)`` in one read, oldest first.

    The list route serves both halves, and two calls would be two reads
    of a file a concurrent request can change in between — which is how a
    roster gets served with a stamp belonging to a different revision of
    it. So the filter lives here rather than above: filtering must not
    cost a second read.

    Order is the array's own, which is insertion order, which is
    ``added_at`` order — there is no separate sort to get wrong.

    ``role`` narrows to one role and is **validated** rather than matched
    loosely, so a typo is ``invalid_role`` instead of a confident empty
    list. That is the reason ``list_workitems`` validates its ``status``
    filter too.
    """
    if role is not None:
        _validate_role(role)
    updated_at, peers = _read_document(peers_path)
    if role is None:
        return (updated_at, peers)
    return (updated_at, [r for r in peers if r.get("role") == role])


def list_peers(peers_path: Path, *, role: Optional[str] = None) -> list[dict]:
    """Just the roster — :func:`read_roster` without the document stamp."""
    return read_roster(peers_path, role=role)[1]


def get_peer(peers_path: Path, user_id: str) -> Optional[dict]:
    """Return the peer's record, or ``None`` if they are not on the roster.

    No ``include_deleted`` twin, and there never will be one: removal is
    a hard delete here (module docstring §3), so an absent peer is
    absent — there is no history to opt into.
    """
    _, peers = _read_document(peers_path)
    index = _index_of(peers, user_id)
    return None if index is None else peers[index]


def update_peer(
    peers_path: Path,
    user_id: str,
    *,
    role: Optional[str] = None,
    label: Any = UNSET,
    endpoint: Any = UNSET,
) -> dict:
    """Update a peer's mutable fields. Returns the updated record.

    ``role`` takes ``None`` to mean "not supplied" — it is not nullable,
    so the two readings cannot collide. ``label`` and ``endpoint`` are
    nullable and therefore take :data:`UNSET`: passing ``None``
    **clears** them, which is how a display name or a sync endpoint is
    removed.

    ``user_id`` and ``added_at`` are **not** parameters. ``user_id`` is
    the identity, so changing it is a delete plus a create rather than an
    edit — a PATCH that re-keyed a record would silently transfer
    whatever the old id meant to a new person. ``added_at`` is an
    observation this Space made and is not the caller's to revise.

    Raises ``peer_not_found`` if the id is not on the roster.

    A call that changes nothing writes nothing and stamps nothing: an
    idempotent PATCH leaves the document byte-identical.
    """
    resolved_role = None if role is None else _validate_role(role)
    resolved_label = label if isinstance(label, _Unset) else _validate_label(label)
    resolved_endpoint = (
        endpoint if isinstance(endpoint, _Unset) else _validate_endpoint(endpoint)
    )

    with locked(peers_path):
        _, peers = _read_document(peers_path)
        index = _index_of(peers, user_id)
        if index is None:
            raise PeersStoreError("peer_not_found", "Peer not found.")
        record = peers[index]

        changed = False
        if resolved_role is not None and record.get("role") != resolved_role:
            record["role"] = resolved_role
            changed = True
        for field, value in (("label", resolved_label), ("endpoint", resolved_endpoint)):
            if not isinstance(value, _Unset) and record.get(field) != value:
                record[field] = value
                changed = True

        if not changed:
            return copy.deepcopy(record)

        # Re-ordered because a write can *introduce* a key: a record
        # stored without ``label`` gains one when somebody sets it, and
        # ``_KEY_ORDER`` exists so the synced document does not carry
        # that kind of diff noise.
        record = _ordered(record)
        peers[index] = record
        _write(peers_path, peers)
        return copy.deepcopy(record)


def delete_peer(peers_path: Path, user_id: str) -> bool:
    """Remove a peer. ``True`` if this call removed them, ``False`` if
    there was nothing to remove — idempotent, so a second DELETE of the
    same ``user_id`` is a no-op rather than a 404, matching the todos and
    workitems contract.

    **The record is destroyed, not tombstoned.** See the module docstring
    §3 for both reasons: the shipped schema has no field to tombstone
    into, and ``peers.json`` is in the synced tier, so a tombstone would
    carry "this person used to have access" to every Space the project
    reaches. A roster answers who is on the project *now*.

    There is no ``deleted_by`` parameter for the same reason there is no
    ``runtime`` on create: the schema declares no place to record one,
    and this store does not accept values it cannot store.
    """
    with locked(peers_path):
        _, peers = _read_document(peers_path)
        index = _index_of(peers, user_id)
        if index is None:
            return False
        peers.pop(index)
        _write(peers_path, peers)
        return True
