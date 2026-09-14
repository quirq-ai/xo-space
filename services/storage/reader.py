"""Pure JSON readers for visualizer and watcher state.

Space-level (``services/storage``): the one file-reading module the
BFF routes' scope handles call. Returns plain dicts; never touches an
agent runtime's native storage under ``~``. It reads portable
visualizer files from project/workspace ``.xo/`` and ephemeral activity
from ``~/.quirq/cache/activity/``. Path resolution and clamping live in
the caller (see ``services/cowork_agent/scopes.py``
``VisualizerScope`` / ``WorkspaceVisualizerScope``).

Two non-trivial readers:

* :func:`read_jsonl_tail_reverse`: backward scan of ``timeline.jsonl``
  with optional ``before`` timestamp + ``types`` allowlist.
* :func:`merge_session_record`: stitches an adapter-owned
  ``sessionslist.json`` row with the watcher-owned
  ``sessions-augment.json`` row keyed by the same composite session
  id (see docs/watcher-design.md §3.7.2). Asserts field-disjointness
  so a future schema collision fails loudly in tests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ── Adapter-owned vs watcher-owned field partitions (§3.7) ────────────────────

# Fields the runtime adapters write to ``sessionslist.json``. Used to assert
# disjointness on merge: if the watcher ever starts writing one of these
# into ``sessions-augment.json``, ``merge_session_record`` will refuse to
# stitch and the route will 500. That's the intended fail-closed behaviour.
_ADAPTER_FIELDS: frozenset[str] = frozenset({
    "sessionId", "nativeSessionId", "directory", "backend", "updatedAt", "usage",
})

# Fields the watcher writes to ``sessions-augment.json``. Disjoint from
# ``_ADAPTER_FIELDS`` by construction.
_AUGMENT_FIELDS: frozenset[str] = frozenset({
    "messageCount", "toolCallCount", "taskCount",
    "firstActivity", "lastActivity", "ended_at", "episode_refs",
})


# ── Plain JSON / JSONL readers ────────────────────────────────────────────────


def read_json(path: Path) -> Optional[dict]:
    """Return parsed JSON, or ``None`` if the file is missing or unreadable.

    Malformed JSON logs at WARN and returns ``None``. The caller (route
    layer) maps ``None`` to either an empty document or a 500
    ``scope_unavailable`` depending on the endpoint contract; this
    helper is shape-agnostic.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("visualizer.read_json failed for %s: %s", path, exc)
        return None


def read_jsonl_tail_reverse(
    path: Path,
    *,
    limit: int,
    before_ts: Optional[str] = None,
    types: Optional[frozenset[str]] = None,
) -> list[dict]:
    """Read up to ``limit`` events from a ``timeline.jsonl`` file, newest-first.

    Parameters
    ----------
    path
        Absolute path to the timeline file. Missing file → empty list.
    limit
        Maximum number of events to return. Caller clamps to a sane
        upper bound (see route layer; the design says max 500).
    before_ts
        Optional ISO-8601 string. Events with ``ts >= before_ts`` are
        skipped (used for cursor-style pagination).
    types
        Optional allowlist of event ``type`` values. Events outside the
        set are skipped. ``None`` means "all types".

    Returns the events in **descending** ``ts`` order (newest first).

    Implementation note
    -------------------
    Timeline rotates at 8 MB (§3.8), so the file is small. We load the
    whole file, scan once, keep the matching tail. Simple, correct,
    fast enough; revisit only if rotation grows.
    """
    if limit <= 0 or not path.is_file():
        return []

    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("visualizer.read_jsonl_tail_reverse io failed for %s: %s", path, exc)
        return []

    matches: list[dict] = []
    # Iterate from newest line backward so we can stop the moment we hit limit.
    for line in reversed(raw):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("visualizer: dropping malformed timeline line in %s", path)
            continue
        if not isinstance(ev, dict):
            continue
        if types is not None and ev.get("type") not in types:
            continue
        if before_ts is not None:
            ts = ev.get("ts")
            if not isinstance(ts, str) or ts >= before_ts:
                continue
        matches.append(ev)
        if len(matches) >= limit:
            break

    return matches


# ── Session-record merge (§3.7.2) ─────────────────────────────────────────────


def merge_session_record(
    base: dict, augment: Optional[dict]
) -> dict:
    """Stitch an adapter-owned ``sessionslist.json`` row with the
    watcher-owned ``sessions-augment.json`` row.

    The two field sets are disjoint by design (see ``_ADAPTER_FIELDS`` /
    ``_AUGMENT_FIELDS``). If a collision ever appears, this function
    raises ``AssertionError`` so a misbehaving sink (or a schema drift
    that quietly aliased a name) fails closed in tests rather than
    silently overwriting an adapter field.

    ``augment`` may be ``None`` (watcher hasn't written for this session
    yet); the function returns the base row unchanged in that case.
    Keys present only in ``augment`` (i.e. no matching adapter row) are
    handled at the caller (a session that the adapter has no row for
    isn't a session at all, by definition).
    """
    if not isinstance(base, dict):
        raise TypeError(f"merge_session_record: base must be dict, got {type(base).__name__}")
    if augment is None:
        return dict(base)
    if not isinstance(augment, dict):
        raise TypeError(f"merge_session_record: augment must be dict|None, got {type(augment).__name__}")

    overlap = set(base.keys()) & set(augment.keys())
    if overlap:
        raise AssertionError(
            f"merge_session_record: field collision between adapter row and watcher augment: {sorted(overlap)}"
        )
    merged = dict(base)
    merged.update(augment)
    return merged


def merge_sessionslist(
    base_index: Optional[dict], augment_doc: Optional[dict]
) -> dict[str, dict]:
    """Stitch the full ``sessionslist.json`` map with ``sessions-augment.json``.

    ``base_index`` is the adapter-owned flat map ``{<key>: <adapter_row>}``.
    ``augment_doc`` is the watcher-owned wrapper
    ``{schema, updated_at, sessions: {<key>: <augment_row>}}``.

    Returns ``{<key>: <merged_row>}`` for every key present in the base
    index. Augment rows without a matching adapter row are dropped (a
    session that no adapter created is not a session; possibly stale
    data from a renamed/deleted session). The watcher itself prunes
    these on its next tick.
    """
    if not isinstance(base_index, dict) or not base_index:
        return {}
    augments: dict[str, dict] = {}
    if isinstance(augment_doc, dict):
        s = augment_doc.get("sessions")
        if isinstance(s, dict):
            augments = s
    return {
        key: merge_session_record(row, augments.get(key))
        for key, row in base_index.items()
        if isinstance(row, dict)
    }


# ── Helpers exposed for tests ─────────────────────────────────────────────────


def adapter_field_names() -> Iterable[str]:
    """Exposed for tests / verification (§7.4 wire-allowlist scan)."""
    return _ADAPTER_FIELDS


def augment_field_names() -> Iterable[str]:
    """Exposed for tests / verification."""
    return _AUGMENT_FIELDS
