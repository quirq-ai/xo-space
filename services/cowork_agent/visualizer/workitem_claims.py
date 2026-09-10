"""Agent claims over workitems, and the derived ``in_progress`` (plan §5.4)."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_owned,
)
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.ingest.events import Event, WorkitemEvent
from services.cowork_agent.visualizer.sinks import timeline

logger = logging.getLogger(__name__)


#: On-disk revision of ``claims.json`` (plan §5.4).
CLAIMS_SCHEMA = 1

#: Path of the claims document relative to a project's **runtime** root
#: (``~/.quirq/projects/<pid>/``).
CLAIMS_RELPATH = "workitems/claims.json"

#: Top-level keys this module owns, for :func:`write_json_owned`.
_OWNS: frozenset[str] = frozenset({"schema", "updated_at", "claims"})

#: Same charset as ``workitems_store`` and ``todos_store``: permissive enough
#: for the colon-separated composite session keys and realistic runtime keys,
#: restrictive enough to reject traversal and anything that could turn a
#: document key into arbitrary caller text.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")

#: The floor on the grace window, in seconds.
CLAIM_GRACE_SECONDS = 60.0

#: Clamp copied from ``watcher._poll_interval_seconds``.
_TICK_MIN_S = 0.25
_TICK_MAX_S = 60.0


class WorkitemClaimsError(Exception):
    """``(code, message)``, exactly the shape ``WorkitemsStoreError`` has."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> Optional[datetime]:
    """ISO-8601 → aware datetime, or ``None`` when it cannot be read."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def grace_seconds() -> float:
    """How long a claim is in progress without presence corroboration."""
    raw = (os.getenv("QUIRQ_WATCHER_INTERVAL_SECONDS", "1") or "1").strip()
    try:
        interval = float(raw)
    except ValueError:
        interval = 1.0
    interval = min(_TICK_MAX_S, max(_TICK_MIN_S, interval))
    return max(CLAIM_GRACE_SECONDS, 2.0 * interval)


def claims_path_for(runtime_root: Path) -> Path:
    """``<runtime root>/workitems/claims.json``."""
    return Path(runtime_root) / CLAIMS_RELPATH


# ── Validation ─────────────────────────────────────────────────────────────


def _validate_key(value: object, *, kind: str, code: str) -> str:
    if not isinstance(value, str) or not _SAFE_KEY_RE.match(value):
        raise WorkitemClaimsError(
            code,
            f"{kind} must match [A-Za-z0-9_:-.]{{1,200}}; got {value!r}.",
        )
    return value


# ── Document I/O ───────────────────────────────────────────────────────────


def _corrupt(path: Path, reason: str) -> WorkitemClaimsError:
    return WorkitemClaimsError(
        "corrupt_document",
        f"{path} is not a readable claims document ({reason}); refusing to "
        f"write it, because writing it as empty would discard the claims of "
        f"every other live session on this machine. The file is runtime tier "
        f"and disposable — delete it and the next claim recreates it.",
    )


def read_claims(path: Path) -> dict[str, dict]:
    """The ``claims`` map, deep-copied. Raises on a document it cannot read."""
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
    if version is not None and (isinstance(version, bool) or version != CLAIMS_SCHEMA):
        raise WorkitemClaimsError(
            "unsupported_schema",
            f"{path} declares schema {version!r}; this module writes schema "
            f"{CLAIMS_SCHEMA} and will not rewrite a document it cannot fully "
            f"represent.",
        )

    raw = parsed.get("claims")
    if raw is None:
        raise _corrupt(path, "no claims map")
    if not isinstance(raw, dict):
        raise _corrupt(path, f"claims is a {type(raw).__name__}, expected object")
    for key, record in raw.items():
        if not isinstance(record, dict):
            raise _corrupt(path, f"claims[{key!r}] is a {type(record).__name__}")
    return copy.deepcopy(raw)


def read_claims_quiet(path: Path) -> dict[str, dict]:
    """:func:`read_claims`, but never raising — ``{}`` on any problem."""
    try:
        return read_claims(path)
    except WorkitemClaimsError as exc:
        logger.warning(
            "claims document unusable (%s); deriving in_progress as if there "
            "were no claims: %s", exc.code, exc,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning("claims document unusable; deriving in_progress as empty",
                       exc_info=True)
    return {}


def _write(path: Path, claims: dict) -> None:
    try:
        write_json_owned(
            path,
            owns=_OWNS,
            values={
                "schema": CLAIMS_SCHEMA,
                "updated_at": _iso(_now()),
                "claims": claims,
            },
        )
    except CorruptDocumentError as exc:
        raise _corrupt(path, exc.reason) from exc


# ── Lifecycle events ───────────────────────────────────────────────────────


def _emit(path: Path, events: list[Event]) -> None:
    """Append claim events to the project's timeline. **Never raises.**"""
    if not events:
        return
    timeline.apply_quiet(Path(path).parent.parent, events)


# ── The claim, and its release ─────────────────────────────────────────────


def claim_workitem(
    path: Path,
    workitem_id: str,
    *,
    session_id: str,
    runtime: str,
    started_at: Optional[str] = None,
) -> dict:
    """Record that ``session_id`` is working ``workitem_id``. Returns the claim."""
    _validate_key(workitem_id, kind="workitem_id", code="invalid_value")
    _validate_key(session_id, kind="session_id", code="invalid_session_id")
    _validate_key(runtime, kind="runtime", code="invalid_runtime")
    if started_at is None:
        stamp = _iso(_now())
    else:
        parsed = _parse_iso(started_at)
        if parsed is None:
            raise WorkitemClaimsError(
                "invalid_value",
                f"started_at must be an ISO-8601 timestamp; got {started_at!r}.",
            )
        stamp = _iso(parsed)

    record = {"session_id": session_id, "runtime": runtime, "started_at": stamp}
    with locked(path):
        claims = read_claims(path)
        claims[workitem_id] = record
        _write(path, claims)

    # Emitted for every claim, including the upsert that replaces an existing
    # one: re-claiming is a real transition (a different session is working it
    # now, or the same one restarted), and collapsing it would lose exactly the
    # history §5.4 says only this log holds.
    _emit(path, [
        WorkitemEvent(
            ts=stamp,
            native_session_id=session_id,
            runtime=runtime,
            action="claimed",
            workitem_id=workitem_id,
        )
    ])
    return dict(record)


def release_workitem(path: Path, workitem_id: str) -> bool:
    """Drop the claim. ``True`` if this call removed one."""
    _validate_key(workitem_id, kind="workitem_id", code="invalid_value")
    with locked(path):
        claims = read_claims(path)
        if workitem_id not in claims:
            # Nothing was released, so nothing is logged: the idempotent second
            # DELETE, and the implicit release of a workitem that was never
            # claimed, must not both look like work stopping.
            return False
        released = claims.pop(workitem_id)
        _write(path, claims)

    # The claim being removed is what says *who* stopped, so it is read off the
    # record rather than asked of the caller — the implicit releases (closing,
    # tombstoning) do not know the session.
    _emit(path, [
        WorkitemEvent(
            ts=_iso(_now()),
            native_session_id=str(released.get("session_id") or "")
            if isinstance(released, dict) else "",
            runtime=str(released.get("runtime") or "")
            if isinstance(released, dict) else "",
            action="released",
            workitem_id=workitem_id,
        )
    ])
    return True


def release_workitem_quiet(path: Path, workitem_id: str) -> bool:
    """:func:`release_workitem` for the *implicit* releases."""
    try:
        return release_workitem(path, workitem_id)
    except WorkitemClaimsError as exc:
        logger.warning(
            "could not release the claim on workitem %s (%s); it will lapse "
            "with its session: %s", workitem_id, exc.code, exc,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "could not release the claim on workitem %s; it will lapse with "
            "its session", workitem_id, exc_info=True,
        )
    return False


# ── The derivation ─────────────────────────────────────────────────────────


def live_session_ids(activity: Optional[dict]) -> frozenset[str]:
    """The session ids in a presence snapshot's ``open_sessions``."""
    if not isinstance(activity, dict):
        return frozenset()
    rows = activity.get("open_sessions")
    if not isinstance(rows, list):
        return frozenset()
    return frozenset(
        str(row["session_id"])
        for row in rows
        if isinstance(row, dict) and row.get("session_id")
    )


def is_claim_live(
    claim: object,
    *,
    live_sessions: Iterable[str],
    now: Optional[datetime] = None,
    grace: Optional[float] = None,
) -> bool:
    """Whether one claim currently means "an agent is working this"."""
    if not isinstance(claim, dict):
        return False
    session_id = claim.get("session_id")
    lookup = (
        live_sessions
        if isinstance(live_sessions, (set, frozenset))
        else set(live_sessions)
    )
    if isinstance(session_id, str) and session_id in lookup:
        return True
    started = _parse_iso(claim.get("started_at"))
    if started is None:
        return False
    window = grace_seconds() if grace is None else float(grace)
    age = ((now or _now()) - started).total_seconds()
    return 0 <= age < window


def in_progress_ids(
    claims: dict[str, Any],
    *,
    live_sessions: Iterable[str],
    now: Optional[datetime] = None,
    grace: Optional[float] = None,
) -> frozenset[str]:
    """The workitem ids that are in progress right now."""
    if not isinstance(claims, dict) or not claims:
        return frozenset()
    live = set(live_sessions)
    moment = now or _now()
    window = grace_seconds() if grace is None else float(grace)
    return frozenset(
        workitem_id
        for workitem_id, claim in claims.items()
        if isinstance(workitem_id, str)
        and is_claim_live(claim, live_sessions=live, now=moment, grace=window)
    )
