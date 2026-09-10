"""CRUD over ``<project>/.xo/peers.json`` — the collaborator roster."""

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

#: Value written into the document's ``$schema`` key.
_SCHEMA_REF = "xo/peers.schema.json"

#: The top-level keys this store owns — every key the schema declares, because
#: ``peers.schema.json`` is ``additionalProperties: false`` and there is no
#: second writer.
_OWNS: frozenset[str] = frozenset({"$schema", "schema", "updated_at", "peers"})

#: The three roles ``peers.schema.json`` enumerates, and no others.
VALID_ROLES: frozenset[str] = frozenset({"owner", "collaborator", "viewer"})

# The assignee charset, byte for byte.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")

# A label is human text (a display name may carry spaces, accents,
# punctuation), so only control characters are excluded — the same rule
# ``workitems_store`` applies to a GitHub label.
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")

_ENDPOINT_LIMIT = 2000

#: A ceiling on the roster, because this is a synced document and an unbounded
#: array is an unbounded snapshot. Far above any real team.
_MAX_PEERS = 1000

#: Canonical key order for a stored record, matching ``peers.schema.json``'s
#: own property order.
_KEY_ORDER: tuple[str, ...] = ("user_id", "role", "added_at", "endpoint", "label")


class _Unset:
    """Sentinel: "the caller did not supply this field"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


UNSET = _Unset()


class PeersStoreError(Exception):
    """
    Base for all store failures. ``code`` is the BFF error code the route maps
    to ``detail.code`` — same shape as ``WorkitemsStoreError`` and
    ``TodosStoreError``.
    """

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
    """``None`` or an ``http(s)://`` URL."""
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
    """The O-E refusal, in one place."""
    return PeersStoreError(
        "corrupt_document",
        f"{path} is not a readable peers document ({reason}); refusing to "
        f"read or write it. Treating it as empty would discard every "
        f"collaborator it lists (docs/OUTSTANDING.md O-E). Repair or move "
        f"the file.",
    )


def _read_document(path: Path) -> tuple[Optional[str], list[dict]]:
    """Return ``(updated_at, peers)``, deep-copied."""
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
    """Persist the roster, refusing a document that went unreadable."""
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
    """``record`` with its keys in :data:`_KEY_ORDER`, extras kept at the end."""
    out = {key: record[key] for key in _KEY_ORDER if key in record}
    for key, value in record.items():
        if key not in out:
            out[key] = value
    return out


def _index_of(peers: list[dict], user_id: str) -> Optional[int]:
    """Position of ``user_id`` in the roster, or ``None``."""
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
    """Add a peer to the roster. Returns the stored record."""
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
    """``(updated_at, peers)`` in one read, oldest first."""
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
    """Return the peer's record, or ``None`` if they are not on the roster."""
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
    """Update a peer's mutable fields. Returns the updated record."""
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

        # Re-ordered because a write can *introduce* a key: a record stored
        # without ``label`` gains one when somebody sets it, and ``_KEY_ORDER``
        # exists so the synced document does not carry that kind of diff noise.
        record = _ordered(record)
        peers[index] = record
        _write(peers_path, peers)
        return copy.deepcopy(record)


def delete_peer(peers_path: Path, user_id: str) -> bool:
    """Remove a peer."""
    with locked(peers_path):
        _, peers = _read_document(peers_path)
        index = _index_of(peers, user_id)
        if index is None:
            return False
        peers.pop(index)
        _write(peers_path, peers)
        return True
