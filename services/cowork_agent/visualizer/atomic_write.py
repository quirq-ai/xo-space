"""Atomic file writes for the records this system keeps on disk."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


def write_json_atomic(path: Path, data: Any) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Parent directories are created on demand (the watcher's
    workspace-tier sink writes the first-ever workspace ``.xo/``
    directory this way). Writes a sibling ``<path>.tmp`` file and
    ``os.replace`` it over the target — atomic on POSIX as long as
    both paths live on the same filesystem (true for everything under
    ``~/xo-projects/`` in practice).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def append_jsonl(path: Path, lines: list[dict]) -> None:
    """Append one or more JSON objects as JSONL lines, then ``fsync``.

    Used by both timeline sinks — the per-project ``sinks/timeline.py``
    and the multiplexed ``workspace/timeline.py``.

    **This file has many writers, and the contract below is what a new
    one may rely on.** It is spelled out in full because the previous
    version of this docstring asserted *"exactly one writer — the
    watcher"*, which stopped being true when the todos HTTP API became
    the todo event source (syncplan §7, T7) and was catalogued as defect
    **O-D**. The appenders to a project ``timeline.jsonl`` today are the
    watcher tick, :mod:`~services.cowork_agent.visualizer.todos_store`,
    :mod:`~services.cowork_agent.visualizer.workitems_store` and
    :mod:`~services.cowork_agent.visualizer.workitem_claims` — one tick
    thread and three request threads — and the count has only ever gone
    up. Do not re-narrow this claim; widen the list.

    **Guaranteed.**

    * *No writer loses another's bytes.* The handle is opened ``"a"``
      (``O_APPEND``), so each ``write(2)`` on it seeks to end-of-file
      and writes as one indivisible step. Concurrent appends land one
      after another in some order; none can overwrite another or write
      at a stale offset. No lock is taken here and none is needed for
      this property.
    * *Whole lines, at the batch sizes anything in this system writes.*
      A batch small enough to reach the kernel as a single ``write(2)``
      cannot be split, so a reader parsing line-by-line never sees half
      an event.
    * *Durability on return.* ``flush`` + ``fsync`` before returning, so
      an event this call has returned for survives both a process crash
      and a machine crash.

    **Not guaranteed — do not build on these.**

    * *Order between writers.* File order is the order the kernel saw
      the writes, not ``ts`` order: an event stamped earlier by one
      thread can sit below one stamped later by another. A reader that
      needs chronology sorts by ``ts``.
    * *That one caller's batch stays contiguous.* Another writer's batch
      may land in the middle of it, so no caller may encode meaning in
      two lines being adjacent.
    * *Line integrity for a batch larger than the stream buffer.*
      Python's buffered writer splits a payload bigger than the buffer
      (~8 KiB) into several ``write(2)`` calls, at byte boundaries
      rather than line boundaries — so a concurrent appender can tear a
      line *inside* such a batch. Every caller today is far below that
      (one event is a few hundred bytes, a batch a handful of events); a
      caller wanting to append megabytes in one go must split it into
      batches itself.
    * *Anything about a concurrent rotation.* ``sinks/timeline.py``
      renames the file past 8 MB. An append that resolved the old inode
      lands in the rotated file: nothing is lost, but the last lines of
      an epoch can end up on the far side of the rename.

    Lines are serialised with ``ensure_ascii=False`` (UTF-8 on disk,
    matching :func:`write_json_atomic`) and each is newline-terminated,
    including the last, so the next append starts a line rather than
    extending one.
    """
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(payload)
        fp.flush()
        os.fsync(fp.fileno())


class CorruptDocumentError(ValueError):
    """Raised when a document that must be *merged* cannot be parsed."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"{path} is not a readable JSON object ({reason}); refusing to "
            f"merge into it because every key not owned by this writer would "
            f"be lost. Repair or delete the file, or use "
            f"write_json_atomic_if_changed if this writer owns the whole "
            f"document."
        )
        self.path = path
        self.reason = reason


class _Unset:
    """Sentinel: 'no cached previous payload was supplied — read disk'."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()

_MISSING = object()


# ── Volatile-path handling ────────────────────────────────────────────────────
# ``volatile`` entries are dotted paths, because the timestamp that must not
# count as a change is not always at the top level: ``sessions.json`` stamps it
# at ``meta.generated_at`` (``visualizer/session_telemetry.py:271``).


def _build_mask(volatile: Iterable[str]) -> dict[str, Any]:
    """Compile dotted volatile paths into a nested mask tree."""
    mask: dict[str, Any] = {}
    for raw in volatile or ():
        parts = [part for part in str(raw).split(".") if part]
        if not parts:
            continue
        node = mask
        subsumed = False
        for part in parts[:-1]:
            child = node.get(part, _MISSING)
            if child is None:
                # An ancestor is already ignored wholesale.
                subsumed = True
                break
            if child is _MISSING or not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        if not subsumed:
            node[parts[-1]] = None
    return mask


def _equal_ignoring(left: Any, right: Any, mask: dict[str, Any]) -> bool:
    """Compare two parsed documents, ignoring the masked paths."""
    if not mask or not isinstance(left, dict) or not isinstance(right, dict):
        return left == right

    ignored = {key for key, sub in mask.items() if sub is None}
    keys = (set(left) | set(right)) - ignored
    for key in keys:
        if key not in left or key not in right:
            return False
        sub = mask.get(key)
        if isinstance(sub, dict):
            if not _equal_ignoring(left[key], right[key], sub):
                return False
        elif left[key] != right[key]:
            return False
    return True


def _read_document(path: Path) -> tuple[str, Any]:
    """Read ``path`` as JSON, classifying the outcome."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("absent", None)
    except (OSError, UnicodeDecodeError) as exc:
        return ("fault", f"unreadable: {exc}")
    if not text.strip():
        return ("fault", "empty file")
    try:
        return ("ok", json.loads(text))
    except json.JSONDecodeError as exc:
        return ("fault", f"invalid JSON: {exc}")


def read_stamped_document(path: Path, *, schema: int) -> tuple[str, Any]:
    """
    Read a schema-stamped JSON object, classifying the outcome for a store that
    must refuse rather than guess: ``("absent", None)``, ``("ok", parsed)``,
    ``("fault", reason)`` or ``("schema", found)``.
    """
    state, value = _read_document(path)
    if state != "ok":
        return (state, value)
    if not isinstance(value, dict):
        return ("fault", f"top-level {type(value).__name__}, expected object")
    found = value.get("schema")
    if found is not None and (isinstance(found, bool) or found != schema):
        return ("schema", found)
    return ("ok", value)


def unsupported_schema_message(path: Path, found: Any, expected: int) -> str:
    """Why a document stamped by a newer writer is refused, not rewritten."""
    return (
        f"{path} declares schema {found!r}; this Space writes schema "
        f"{expected} and will not rewrite a document it cannot fully "
        f"represent."
    )


# ── The two write primitives (syncplan §3, rule R-WRITE) ──────────────────────


def write_json_owned(
    path: Path,
    *,
    owns: frozenset[str],
    values: dict,
    volatile: tuple[str, ...] = ("updated_at",),
) -> bool:
    """Replace exactly the ``owns`` keys; carry every other key forward."""
    owned = frozenset(owns)
    if not isinstance(values, dict):
        raise TypeError("values must be a dict")
    unknown = sorted(set(values) - owned)
    if unknown:
        raise ValueError(
            f"write_json_owned({path}): values carries undeclared key(s) "
            f"{unknown}; add them to owns or drop them from values"
        )

    status, current = _read_document(path)
    if status == "fault":
        raise CorruptDocumentError(path, str(current))
    if status == "absent":
        base: dict[str, Any] = {}
        exists = False
    elif isinstance(current, dict):
        base = current
        exists = True
    else:
        raise CorruptDocumentError(
            path, f"top-level {type(current).__name__}, expected object"
        )

    # dict(base) keeps every foreign key *and its position*, so a merge does
    # not reshuffle the document and make diffs unreadable.
    merged = dict(base)
    for key in owned:
        if key not in values:
            merged.pop(key, None)
    for key, value in values.items():
        merged[key] = value

    if exists and _equal_ignoring(base, merged, _build_mask(volatile)):
        return False
    write_json_atomic(path, merged)
    return True


def write_json_atomic_if_changed(
    path: Path,
    payload: dict,
    volatile: tuple[str, ...] = ("updated_at",),
    *,
    previous: Any = _UNSET,
) -> bool:
    """Full-ownership write, skipped when nothing but ``volatile`` differs."""
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    if isinstance(previous, _Unset):
        status, baseline = _read_document(path)
        if status != "ok":
            # Absent, corrupt, truncated: nothing worth preserving under full
            # ownership — write and be done.
            baseline = _MISSING
    else:
        baseline = _MISSING if previous is None else previous

    if baseline is not _MISSING and _equal_ignoring(
        baseline, payload, _build_mask(volatile)
    ):
        # Content matches the baseline — but the baseline may be an in-memory
        # one, in which case nothing has looked at the disk.
        if path.exists():
            return False
    write_json_atomic(path, payload)
    return True


class ChangeGate:
    """
    Write-on-change publishing over :func:`write_json_atomic_if_changed`,
    holding the payload this process last wrote per target path (syncplan §3,
    T26). Sound only for a writer that owns the whole document.
    """

    __slots__ = ("_previous", "_limit")

    def __init__(self, limit: int = 64) -> None:
        self._previous: dict[str, dict] = {}
        self._limit = limit

    def reset(self) -> None:
        """Drop the baselines. For tests, and for a root switch."""
        self._previous.clear()

    def publish(
        self,
        target: Path,
        payload: dict,
        volatile: tuple[str, ...] = ("updated_at",),
    ) -> bool:
        """Write ``payload`` iff it differs. ``True`` when the file changed."""
        key = str(target)
        if key in self._previous and target.exists():
            # Steady state: one ``stat`` and a dict comparison, no read.
            changed = write_json_atomic_if_changed(
                target, payload, volatile, previous=self._previous[key]
            )
        else:
            # No baseline yet (first tick of the process), or the file was
            # removed underneath us — ``rm -rf ~/.quirq`` is a documented clean
            # reset (syncplan §4) and must repopulate on the next tick, not on
            # the next content change.
            changed = write_json_atomic_if_changed(target, payload, volatile)
        if key not in self._previous and len(self._previous) >= self._limit:
            self._previous.clear()
        self._previous[key] = payload
        return changed
