"""Atomic file writes for the watcher's sinks.

Every ``.xo/`` file the watcher rewrites goes through
:func:`write_json_atomic`. The BFF reader either sees the previous
revision or the new one — never a partial / torn write — because
``os.replace`` is atomic on the same filesystem.

Why a dedicated helper rather than each sink doing its own ``write +
rename``: one place to enforce ``ensure_ascii=False`` (we want UTF-8
on disk), ``sort_keys=False`` (preserve template ordering for diff
readability), and the trailing newline. Also a single test target.

Two further primitives sit on top of it (syncplan §3, rule R-WRITE):
:func:`write_json_owned` for a document several writers contribute to —
it replaces only the keys the caller declares and carries the rest
forward — and :func:`write_json_atomic_if_changed` for a document one
writer wholly owns. Both return whether the document actually changed
and skip the write when it did not, which is what stops the watcher
rewriting every file every tick.

None of these ``fsync``. ``os.replace`` protects a reader from a torn
document; it does not order the write against a power loss.
"""

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

    Used by the timeline sink. Append is non-atomic at the *line*
    level (each ``write`` syscall is atomic for sub-PIPE_BUF buffers
    on Linux, but multi-event batches may interleave with concurrent
    writers). The timeline file has exactly one writer — the
    watcher — so interleaving cannot happen. ``fsync`` after the batch
    ensures events survive a server crash.
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
    """Raised when a document that must be *merged* cannot be parsed.

    :func:`write_json_owned` refuses to guess. Treating an unreadable
    document as an empty one is precisely the failure mode that mints a
    fresh ``pid`` over a corrupt ``project.json`` (syncplan §5.1) — the
    merge base is gone, so every key the caller does not own would be
    silently dropped on the next write.

    A caller that genuinely owns the whole document does not need a
    merge base and should use :func:`write_json_atomic_if_changed`,
    which repairs a corrupt file by overwriting it.
    """

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
#
# ``volatile`` entries are dotted paths, because the timestamp that must
# not count as a change is not always at the top level: ``sessions.json``
# stamps it at ``meta.generated_at``
# (``visualizer/session_telemetry.py:271``). A top-level-only exclusion
# would silently fail there — no error, just a rewrite every tick forever.
#
# Paths are compiled into a mask tree: ``{"updated_at": None, "meta":
# {"generated_at": None}}``, where ``None`` means "ignore this key
# entirely" and a dict means "descend, ignoring only what is below".
# Descending is the load-bearing half: a real change to ``meta.sources``
# next to a volatile ``meta.generated_at`` must still count as a change.


def _build_mask(volatile: Iterable[str]) -> dict[str, Any]:
    """Compile dotted volatile paths into a nested mask tree.

    A shorter path wins over a longer one that extends it: given both
    ``"meta"`` and ``"meta.generated_at"``, the whole ``meta`` key is
    ignored. Empty segments are skipped, so ``""`` and ``"a..b"`` are
    tolerated rather than producing a mask on a ``""`` key.
    """
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
    """Compare two parsed documents, ignoring the masked paths.

    Comparison is on *parsed objects*, never on serialised text, so a key
    reorder is not a change (Python dict equality is order-insensitive) —
    otherwise a readdir reorder upstream would read as a content change
    forever.
    """
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
    """Read ``path`` as JSON, classifying the outcome.

    Returns ``("ok", parsed)``, ``("absent", None)``, or
    ``("fault", reason)``. The three are returned as a tagged pair
    rather than as overloaded values because a document whose top-level
    JSON value is a string is a perfectly readable document, and must
    not be mistaken for a fault reason.

    An *empty* file is a fault, not an absent one: a zero-byte JSON file
    is what a truncated non-atomic write leaves behind (syncplan T4),
    never a legitimate state.
    """
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


# ── The two write primitives (syncplan §3, rule R-WRITE) ──────────────────────


def write_json_owned(
    path: Path,
    *,
    owns: frozenset[str],
    values: dict,
    volatile: tuple[str, ...] = ("updated_at",),
) -> bool:
    """Replace exactly the ``owns`` keys; carry every other key forward.

    Rule R-WRITE: a writer may only overwrite a document it *wholly*
    owns. Everything else must be a key-scoped merge — read the
    document, replace only the declared keys, carry the rest through
    untouched. A blind ``dict.update`` of a full payload is overwrite
    with extra steps and does **not** satisfy the rule.

    Omitting an owned key **deletes** it. That is unambiguous precisely
    because ``owns`` is declared separately from ``values``: a key the
    writer owns and did not supply is a deletion, not an accident.

    ``values`` may not carry a key outside ``owns`` — that is a bug in
    the caller (a write it never declared), so it raises ``ValueError``
    rather than silently widening the ownership set.

    Returns ``True`` iff the document on disk changed, ignoring
    ``volatile``. When nothing but a volatile path differs, no write
    happens at all and the stale volatile value stays on disk — that is
    the point: it is what removes the per-tick churn.

    Semantics at the edges:

    * **File absent** — the merge base is empty, the file is created
      from ``values`` alone, and the return is ``True`` (nothing became
      something). Parent directories are created.
    * **File present but corrupt** (unparseable, unreadable, empty, or
      holding a non-object such as a list) — raises
      :class:`CorruptDocumentError`. It is *not* treated as empty:
      doing that discards every foreign key, which is the exact bug
      class syncplan T1 exists to fix.

    Unlike :func:`write_json_atomic_if_changed` this takes no cached
    ``previous``: the read is not an optimisation here, it *is* the
    merge. A cached base would resurrect keys another writer deleted and
    drop keys another writer added — the failure R-WRITE exists to
    prevent.

    No ``fsync``, matching :func:`write_json_atomic`: safe against a
    reader seeing a torn document, not against power-loss reordering.
    """
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

    # dict(base) keeps every foreign key *and its position*, so a merge
    # does not reshuffle the document and make diffs unreadable.
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
    """Full-ownership write, skipped when nothing but ``volatile`` differs.

    Use this only where the writer owns the *entire* document; anything
    another writer can contribute to needs :func:`write_json_owned`.

    Returns ``True`` iff the file was written.

    **Where the comparison baseline comes from is explicit.** By default
    the current file is read and parsed. Pass ``previous=`` — the payload
    this writer last wrote, held in memory — to skip that read, or
    ``previous=None`` to assert there was no previous document (which
    always writes). The tradeoff, per syncplan §3: re-reading costs one
    JSON parse per candidate write, so on a tick that skips most writes
    the read is close to a wash against the write it saves. The in-memory
    baseline is strictly cheaper and is what T26's callers pass; the
    disk read is the correct default for a caller with no such state,
    and for the first write after a restart.

    The in-memory baseline is only sound under full ownership. If
    anything else can edit the file, a cached ``previous`` goes stale and
    this silently stops writing — use :func:`write_json_owned` there.

    Edge cases: an absent file always writes — **including on the
    ``previous=`` fast path**, which does not otherwise touch the disk.
    Without that check a caller holding an in-memory baseline compares
    equal, skips the write, and never notices the target was deleted, so
    the file never comes back; that is a regression against writing
    unconditionally, and it cost ``projects.json`` its self-heal. The
    guard is one ``exists()`` on the skip path only — far cheaper than
    the JSON parse ``previous=`` exists to avoid, and it costs nothing on
    the path that writes. A corrupt or unreadable file also always
    writes — safe here and nowhere else, because full ownership means
    there is nothing in it to preserve, so the write repairs it.

    Comparison is on parsed objects, so key order is not a change, and
    ``volatile`` accepts nested dotted paths such as
    ``"meta.generated_at"`` (see :func:`_build_mask`).

    No ``fsync``, matching :func:`write_json_atomic`: safe against a
    reader seeing a torn document, not against power-loss reordering.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    if isinstance(previous, _Unset):
        status, baseline = _read_document(path)
        if status != "ok":
            # Absent, corrupt, truncated: nothing worth preserving under
            # full ownership — write and be done.
            baseline = _MISSING
    else:
        baseline = _MISSING if previous is None else previous

    if baseline is not _MISSING and _equal_ignoring(
        baseline, payload, _build_mask(volatile)
    ):
        # Content matches the baseline — but the baseline may be an
        # in-memory one, in which case nothing has looked at the disk.
        # Re-materialise a target that has gone missing rather than
        # skipping forever.
        if path.exists():
            return False
    write_json_atomic(path, payload)
    return True
