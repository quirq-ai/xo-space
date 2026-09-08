"""Agent claims over workitems, and the derived ``in_progress`` (plan §5.4).

A workitem is **in progress iff an agent is currently working it**. That
is derived at read time and never stored — the whole point of task W7b.
The principle is T22's, restated on a second document: the watcher's
``alive`` comes from an *observed* heartbeat rather than an ``enabled``
config flag, so a crashed watcher reads as stalled instead of "Live". A
stored ``in_progress`` is a flag that lies the moment the process holding
it dies, and clearing it needs a cleanup path that must itself survive
the crash. A derived one needs nothing: the observation stops and the
state evaporates.

**Where the observation comes from, and where it does not.**
Liveness is read out of ``open_sessions`` in the per-project presence
snapshot — the file
:func:`services.cowork_agent.visualizer.state.project_activity_path`
names, written by
:mod:`services.cowork_agent.visualizer.sinks.activity`. That list is
*rebuilt from scratch* every watcher tick out of the active source's
``poll_presence()``, so a session that stops being present simply stops
appearing in it. Nothing has to notice the death and nothing has to
write a record of it.

It is emphatically **not** read from ``ended_at``.
``sinks/sessions_augment.py`` says of that field: *"currently always
null (filled once session-close detection lands)"* — and session-close
detection does not exist in this system. A design that waited for a
session to be marked closed would leave every claim live forever, which
is exactly the stale flag this module exists to avoid. If session-close
detection ever does land, it is a *second* corroborating signal, never
the primary one.

**The gap that would otherwise flicker.** ``sinks/activity.py`` drops a
presence row whose model is not yet known ("session live but no
assistant message yet" — the schema requires ``agent``). So a session
that claims a workitem and then reads the claim back can find itself
absent from ``open_sessions`` for as long as it takes to emit a first
assistant turn. Without a grace window the UI would show the workitem
in progress, then not, then in progress again. So a *young* claim is in
progress on its own authority; only once it is older than
:func:`grace_seconds` does the presence snapshot have to corroborate it.
The plan phrases the window as "one tick"; the real gap being covered is
"until the first assistant message", which is many ticks at the default
one-second interval, so the floor below is what actually does the work
and the tick interval only raises it.

**Tier.** ``~/.quirq/projects/<pid>/workitems/claims.json`` — the
runtime tier (rule R-TIER), machine-local and disposable. Never
``.xo/``. Two Spaces working the same GitHub issue therefore each hold
their own claims file and each shows their own agent's progress; neither
can overwrite the other's, because neither can see the other's, and
``rm -rf ~/.quirq`` costs at most the claims of sessions that are
currently live.

**The history of a derived state** (plan §8, W10). Because
``in_progress`` is never stored, the only record that it was ever true
is the pair of ``workitem.claimed`` / ``workitem.released`` lines these
functions append to the runtime ``timeline.jsonl``. That is what makes
"what was this agent working on last Tuesday" answerable without ever
having written a flag that could have been wrong. Note the asymmetry
that follows from §5.4: a claim that simply **lapses** — the session
died — emits nothing, because nothing runs at that moment. A lapse is an
absence of observation, not an event, and manufacturing one would need
the cleanup path this design exists to avoid. The log therefore says
when work started and when it was explicitly handed back; the *current*
answer still comes from presence.

**Corrupt documents, asymmetrically.** A read that is feeding the
derived value degrades to "no claims" and logs
(:func:`read_claims_quiet`): a workitem list must not 409 because a
disposable file went bad, and the worst outcome is a row that reads as
not-in-progress. A *write* refuses (:func:`read_claims`, raising
``corrupt_document``), because writing-as-empty would discard the claims
of every other live session on this machine — reading-as-empty is a
degradation, writing-as-empty is destruction. The remedy for a refused
write is to delete the file, which is legitimate precisely because this
tier is disposable.
"""

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
#: (``~/.quirq/projects/<pid>/``). Declared here rather than spelled out
#: at the call site so the tier decision lives with the module that owns
#: the file — see :func:`claims_path_for`.
CLAIMS_RELPATH = "workitems/claims.json"

#: Top-level keys this module owns, for :func:`write_json_owned`.
_OWNS: frozenset[str] = frozenset({"schema", "updated_at", "claims"})

#: Same charset as ``workitems_store`` and ``todos_store``: permissive
#: enough for the colon-separated composite session keys and realistic
#: runtime keys, restrictive enough to reject traversal and anything
#: that could turn a document key into arbitrary caller text. No agent
#: is named here, or anywhere in this module — core code never can.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")

#: The floor on the grace window, in seconds. Not the watcher tick: the
#: gap being covered is "presence row suppressed until the first
#: assistant message", which is many ticks at the default interval. A
#: claim that never becomes live is therefore in progress for at most
#: this long, self-healing with nothing to clean up.
CLAIM_GRACE_SECONDS = 60.0

#: Clamp copied from ``watcher._poll_interval_seconds``. Importing the
#: watcher for it would drag every sink into the request path and would
#: read the interval once at import, which a test that repoints the
#: environment could not move.
_TICK_MIN_S = 0.25
_TICK_MAX_S = 60.0


class WorkitemClaimsError(Exception):
    """``(code, message)``, exactly the shape ``WorkitemsStoreError`` has.

    Deliberate: the BFF's ``_workitem_error`` maps store codes onto HTTP
    statuses, and reusing the vocabulary (``invalid_session_id``,
    ``invalid_runtime``, ``corrupt_document``) means the claim routes get
    the same mapping — including ``corrupt_document`` → 409 — without a
    second error table that could disagree with the first.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> Optional[datetime]:
    """ISO-8601 → aware datetime, or ``None`` when it cannot be read.

    ``None`` is the fail-closed answer: an unparseable ``started_at``
    makes the claim's age unknown, and an unknown age must not read as
    *young* (which would grant an unbounded grace window to a record
    nobody can date).
    """
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
    """How long a claim is in progress without presence corroboration.

    :data:`CLAIM_GRACE_SECONDS` normally, raised to two watcher ticks if
    the tick has been configured slower than that — a window shorter
    than the interval that refreshes ``open_sessions`` could not cover
    even one missed observation, so the tick can only push the floor up.
    """
    raw = (os.getenv("QUIRQ_WATCHER_INTERVAL_SECONDS", "1") or "1").strip()
    try:
        interval = float(raw)
    except ValueError:
        interval = 1.0
    interval = min(_TICK_MAX_S, max(_TICK_MIN_S, interval))
    return max(CLAIM_GRACE_SECONDS, 2.0 * interval)


def claims_path_for(runtime_root: Path) -> Path:
    """``<runtime root>/workitems/claims.json``.

    Takes the *runtime* root — what
    ``project_layout.runtime_dir_for_project`` returns — so the caller
    cannot accidentally hand it a project's ``.xo/``. There is no
    variant that resolves against the synced root, because there is no
    circumstance in which a claim belongs there.
    """
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
    """The ``claims`` map, deep-copied. Raises on a document it cannot read.

    An **absent** file is ``{}`` — nothing has claimed anything yet,
    which is the only state that legitimately reads as empty. Anything
    else that is not a well-formed claims document raises
    ``corrupt_document``; a ``schema`` this revision does not write
    raises ``unsupported_schema``, matching ``workitems_store``.

    This is the strict read, used on the write path. The derived-value
    read path uses :func:`read_claims_quiet`.
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
    """:func:`read_claims`, but never raising — ``{}`` on any problem.

    The read path behind the derived ``in_progress`` must be total. A
    workitem list that 409'd because a *disposable* machine-local file
    went bad would trade a whole surface for a cosmetic field; the
    honest degradation is that the affected rows read as
    not-in-progress, which is also what they read as when the file has
    simply been deleted.
    """
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
    """Append claim events to the project's timeline. **Never raises.**

    The claim is already on disk when this is called, and the caller is
    going to report success either way. A failure to log must not become
    a failure to claim — the store would then have applied a write the
    caller was told to retry. ``timeline.apply_quiet`` swallows and logs
    everything for that reason.

    ``path`` is ``<runtime root>/workitems/claims.json``
    (:func:`claims_path_for`), so the runtime root — where
    ``timeline.jsonl`` lives — is its grandparent. Deriving it that way
    rather than re-resolving through ``project_layout`` is deliberate:
    this module never learns a project id, and the path it was handed
    was already resolved through the chokepoint by whoever built it.
    """
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
    """Record that ``session_id`` is working ``workitem_id``. Returns the claim.

    An upsert: re-claiming refreshes ``started_at`` and a claim from a
    different session replaces the one that was there. There is no
    conflict status, because the file is machine-local — the cross-Space
    case the plan cares about cannot reach it — and a claim held by a
    session that has since died would otherwise need a takeover rule to
    become claimable again.

    ``started_at`` is accepted only so a caller can hand in a stamp it
    has already taken; it is not a way to backdate a claim past the
    grace window on purpose, and it is validated as a timestamp rather
    than trusted.
    """
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

    # Emitted for every claim, including the upsert that replaces an
    # existing one: re-claiming is a real transition (a different session
    # is working it now, or the same one restarted), and collapsing it
    # would lose exactly the history §5.4 says only this log holds.
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
    """Drop the claim. ``True`` if this call removed one.

    Idempotent — releasing an unclaimed workitem is a no-op, not an
    error — because the release is called from three places that cannot
    know whether a claim exists: the explicit ``DELETE .../claim``,
    closing a workitem, and tombstoning one.
    """
    _validate_key(workitem_id, kind="workitem_id", code="invalid_value")
    with locked(path):
        claims = read_claims(path)
        if workitem_id not in claims:
            # Nothing was released, so nothing is logged: the idempotent
            # second DELETE, and the implicit release of a workitem that
            # was never claimed, must not both look like work stopping.
            return False
        released = claims.pop(workitem_id)
        _write(path, claims)

    # The claim being removed is what says *who* stopped, so it is read
    # off the record rather than asked of the caller — the implicit
    # releases (closing, tombstoning) do not know the session.
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
    """:func:`release_workitem` for the *implicit* releases.

    Closing or deleting a workitem releases its claim (§5.4), and
    neither of those requests may fail because a disposable runtime file
    is unwritable: the workitem write already succeeded, and a stranded
    claim is harmless — it stops reading as in progress as soon as the
    session goes, which is the property this whole module rests on.
    """
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
    """The session ids in a presence snapshot's ``open_sessions``.

    ``activity`` is the parsed per-project ``activity.json``; ``None``
    (never written, or deleted) yields the empty set, which is the
    correct reading — nothing has been observed, so nothing is live.

    The set is rebuilt by the watcher from ``poll_presence()`` on every
    tick, so this function needs no notion of staleness of its own: a
    session that stopped being present is already absent from the input.
    """
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
    """Whether one claim currently means "an agent is working this".

    Two ways to be true, and the order matters:

    1. the claim's session appears in ``live_sessions`` — the observed,
       load-bearing signal;
    2. the claim is younger than the grace window — the anti-flicker
       rule for the interval in which the presence row is suppressed
       because the session has not produced an assistant message yet
       (see the module docstring).

    Everything else is false, including a claim whose ``started_at``
    cannot be parsed *or lies in the future*: an unknown or impossible
    age is treated as old, so such a claim depends entirely on the
    observed signal rather than earning an unbounded grace window.
    """
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
    """The workitem ids that are in progress right now.

    Pure: it reads no file and mutates nothing, so the whole derivation
    is testable by handing it a claims map and a set of live sessions.
    Nothing here deletes or rewrites a lapsed claim — the claim is left
    exactly as written and simply stops being reported, which is what
    makes "killing the agent clears it with no cleanup path" true rather
    than merely likely.
    """
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
