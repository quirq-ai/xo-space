"""
Codex CLI usage — discovery + parser + dashboard aggregator.

Loaded by ``services.cowork_agent.engine.usage_loader.load_usage_module()`` when
``AGENT_NAME=codex``. Same public contract as claude_code/openclaw/antigravity;
the entries-based views (summary/analytics/sessions/summary_card/get_session/
sync) are delegated to :mod:`services.cowork_agent.adapters.usage_common` via a
``Source`` bound to this module's discovery + parser.

Source: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO8601>-<uuid>.jsonl`` — the
on-disk rollout Codex writes per session. Discovery walks the sessions tree
**recursively** (codex nests by date, unlike claude's one-dir-per-project) via
``codex.paths.iter_rollouts`` — the single source of truth for the CODEX_HOME
resolution (env override, then manifest ``home_dir``, then ``~/.codex``). No
xo-project filtering, so the UI shows the full codex usage.

Token shape uses codex rollout fields from ``event_msg/token_count`` →
``info.last_token_usage`` (per-turn delta): ``input_tokens`` (INCLUDES the
cached portion), ``cached_input_tokens``, ``cache_write_input_tokens``,
``output_tokens``. We de-cache the input so downstream never double-counts.
Cost is always 0.0 — codex rollouts have no billing field (subscription plan).
Provider is the constant ``"openai"``; model is ``turn_context.model`` per turn.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, tzinfo
from collections.abc import Iterator
from typing import NamedTuple, Optional

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.codex import paths as _paths


# ─────────────────────────────────────────────────────────────────────────────
# Configuration + helpers (used by discovery / parse_file / dashboard)
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_tz(tz: str) -> tzinfo:
    return _uc.resolve_tz(tz)


def _record_time(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _iter_jsonl(path: str) -> Iterator[dict]:
    """Tolerant JSONL reader, line by line (a rollout can run to hundreds of
    MB) — skips malformed lines instead of raising, so one bad rollout record
    doesn't poison a whole session."""
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        pass


# Standard 8-4-4-4-12 UUID; the rollout filename is
# ``rollout-<ISO8601 with dashes>-<uuid>.jsonl`` so a trailing-UUID search is the
# only reliable split (the timestamp segment also contains dashes). This is an
# unanchored ``search`` variant of ``paths._UUID_RE`` (which is a private,
# fullmatch-anchored guard and not part of the paths public surface).
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _uuid_from_name(basename: str) -> str:
    """Extract the session UUID from a rollout filename.

    Kept identical to what get_session substring-matches on: the UUID appears in
    the basename, so returning it as ``meta.sessionId`` makes
    ``session_id in os.path.basename(sf)`` (usage_common.get_session) match.
    """
    m = _UUID_RE.search(basename)
    if m:
        return m.group(0)
    stem = basename[: -len(".jsonl")] if basename.endswith(".jsonl") else basename
    return stem


# TODO(codex): CONFIRM the exact injected-frame markers against a real rollout's
# event_msg/user_message frames. Verified: codex injects an environment/context
# preamble that must NOT count as a user turn. These two prefixes are the
# high-confidence defaults; widen if a real rollout shows other wrappers.
#   Check: for f in ~/.codex/sessions/**/rollout-*.jsonl; do
#     jq -c 'select(.type=="event_msg" and .payload.type=="user_message")
#            | .payload.message' "$f"; done | head
_INJECTED_USER_PREFIXES = ("<environment_context>", "<user_instructions>")


def _user_text(message) -> str:
    """Normalize a user_message.message (str, or list of content blocks) to text."""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        return " ".join(
            b.get("text", "")
            for b in message
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ).strip()
    return ""


def _is_genuine_user(message) -> bool:
    """True only for a real user prompt — filters injected developer /
    <environment_context> frames so protocol scaffolding is not a 'turn'."""
    text = _user_text(message)
    if not text:
        return False
    return not any(text.startswith(p) for p in _INJECTED_USER_PREFIXES)


def _title(text: str) -> str:
    return text[:80] + ("..." if len(text) > 80 else "")


class _User(NamedTuple):
    at: datetime
    stamp: str
    text: str
    key: str  # the UserMessage item's id, else one made from its line (``_turns``)


class _Call(NamedTuple):
    """One model call's usage, as codex reports it (``input`` includes ``cached``)."""
    at: datetime
    stamp: str
    model: str
    input: int
    cached: int
    output: int
    cache_write: int
    key: str                            # the call's response id, else one made from its line (``_turns``)


def _usage(raw: object) -> tuple[int, int, int, int]:
    raw = raw if isinstance(raw, dict) else {}
    return tuple(int(raw.get(k, 0) or 0) for k in (  # type: ignore[return-value]
        "input_tokens", "cached_input_tokens", "output_tokens", "cache_write_input_tokens"))


def _turns(path: str, meta: dict) -> list:
    """A rollout's user messages (``_User``) and model calls (``_Call``), in
    file order; ``meta`` gets its ``sessionId`` and ``title``.

    * The session is the first ``session_meta``'s ``session_id``: a
      sub-agent's names its root thread; a fork's second meta is its parent's.
    * A model call is a ``token_usage_record`` (one per response, compaction
      calls included, as codex's own running ``thread_token_usage`` counts
      them). Before a rollout's first record (all of one from a codex that
      writes none; the part of a resumed one written before codex wrote
      them) a call is ``event_msg/token_count``'s ``last_token_usage``,
      skipping an event that repeats the one before it (codex re-sends it
      with a rate-limit update) and a context reset after compaction (no
      input, no output). From the first record on, each ``token_count``
      follows its call's record and repeats it (seen 11-27), so it is
      skipped.
    * A user message is an ``item_completed`` ``UserMessage`` item (an older
      codex's ``user_message`` event), when it is one a person (or XO) sent.
    * Each has a key that is the same wherever its line is copied (a fork):
      the response or item id codex gives, else one made from the line's own
      time and content, so a copied older line is counted once.
    """
    turns: list = []
    has_records = False
    models: dict[str, str] = {}
    current_model: Optional[str] = None
    last_info: object = None
    for record in _iter_jsonl(path):
        top = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if top == "session_meta":
            if meta.get("sessionId") is None:
                sid = payload.get("session_id") or payload.get("id")
                if isinstance(sid, str) and sid:
                    meta["sessionId"] = sid
            continue
        if top == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m:
                current_model = m
                if isinstance(payload.get("turn_id"), str):
                    models[payload["turn_id"]] = m
            continue
        stamp = record.get("timestamp")
        rt = _record_time(stamp)
        if top == "token_usage_record":
            has_records = True
            if rt is None:
                continue
            inp, cached, out, write = _usage(payload.get("usage"))
            turn_model = models.get(payload.get("turn_id")) if isinstance(payload.get("turn_id"), str) else None
            key = payload.get("response_id")
            if not (isinstance(key, str) and key):
                key = f"token_usage_record:{stamp}:{inp}:{cached}:{out}:{write}"
            turns.append(_Call(rt, stamp, turn_model or current_model or "gpt-5", inp, cached, out, write, key))
            continue
        if top != "event_msg":
            continue
        etype = payload.get("type")
        if etype == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict) or info == last_info:
                continue
            last_info = info
            inp, cached, out, write = _usage(info.get("last_token_usage"))
            if has_records or rt is None or not (inp or out):
                continue
            turns.append(_Call(rt, stamp, current_model or "gpt-5", inp, cached, out, write,
                               f"token_count:{stamp}:{inp}:{cached}:{out}:{write}"))
            continue
        if etype == "user_message":
            message, item_id = payload.get("message"), None
        elif etype == "item_completed" and isinstance(payload.get("item"), dict) \
                and payload["item"].get("type") == "UserMessage":
            message, item_id = payload["item"].get("content"), payload["item"].get("id")
        else:
            continue
        if rt is None or not _is_genuine_user(message):
            continue
        text = _user_text(message)
        if not meta.get("title"):
            meta["title"] = _title(text)
        if not (isinstance(item_id, str) and item_id):
            item_id = f"user_message:{stamp}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
        turns.append(_User(rt, stamp, text, item_id))
    return turns


# ─────────────────────────────────────────────────────────────────────────────
# File discovery — every rollout under $CODEX_HOME/sessions/**/  (RECURSIVE)
# ─────────────────────────────────────────────────────────────────────────────


def _discover() -> list[str]:
    """Return every ``rollout-*.jsonl`` under the sessions root, RECURSIVELY.

    Delegates the recursive walk + CODEX_HOME resolution to
    ``codex.paths.iter_rollouts`` (the canonical codex discovery primitive: it
    rglobs ``sessions/YYYY/MM/DD/`` — NOT one level deep like claude_code's
    projects walk — and returns an empty iterator when the tree is absent). We
    re-sort by path string for a deterministic order independent of filesystem
    mtimes. No xo-project filtering: we want the full picture the Settings →
    Usage tab needs, archived threads included: their calls were still made.
    """
    return sorted(str(p) for p in _paths.iter_rollouts(archived=True) if p.is_file())


# ─────────────────────────────────────────────────────────────────────────────
# Contract — discovery + parser (codex-specific)
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """Every codex rollout .jsonl on disk.

    ``agent_id`` is accepted for contract symmetry with openclaw but ignored —
    codex is single-tenant per deployment.
    """
    return _discover()


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse a single codex rollout .jsonl into ``(meta, entries)``.

    Entries are the shared normalized shape (role=user|assistant): one
    assistant entry per model call (see ``_turns``). ``input`` is the fresh
    input (codex's ``input_tokens`` less its cached part, which is
    ``cacheRead``). ``meta.sessionId`` is the session's id (a sub-agent's: its
    root thread's), else the filename's.
    """
    meta: dict = {"sessionId": None, "sessionFile": os.path.basename(path)}
    entries: list = []
    last_user_ts: Optional[int] = None
    turns = _turns(path, meta)
    meta.pop("title", None)
    meta["sessionId"] = meta["sessionId"] or _uuid_from_name(os.path.basename(path))

    for turn in turns:
        ts_epoch_ms = int(turn.at.timestamp() * 1000)
        if start_ms and ts_epoch_ms < start_ms:
            continue
        if end_ms and ts_epoch_ms > end_ms:
            continue
        if isinstance(turn, _User):
            last_user_ts = ts_epoch_ms
            entries.append({"role": "user", "timestamp": ts_epoch_ms, "messageId": turn.key})
            continue
        inp = turn.input - turn.cached
        if inp < 0:
            inp = turn.input  # defensive: cached should never exceed input
        duration_ms = (ts_epoch_ms - last_user_ts) if last_user_ts else None
        entries.append({
            "role": "assistant",
            "usage": {
                "input": inp,
                "output": turn.output,
                "cacheRead": turn.cached,
                "cacheWrite": turn.cache_write,
                "totalTokens": turn.input + turn.output,  # VERIFIED rule (excludes cacheWrite)
                "cost": {"total": 0.0, "input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
            },
            "provider": "openai",
            "model": turn.model,
            "messageId": turn.key,
            "timestamp": ts_epoch_ms,
            "stopReason": None,
            "toolNames": [],
            "toolResultCounts": {"total": 0, "errors": 0},
            "durationMs": duration_ms,
        })

    return meta, entries


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Codex-specific (separate from shared entries views): each rollout's digest,
# kept until the file changes, folded by usage_common.dashboard_from_digests
# (empty-session mtime parity, a response-time clamp). Always returns
# total_cost=0.0 (no billing in codex rollouts). NO 'estimated' flag — codex
# token_count is real usage, unlike agy's client-side estimates.
# ─────────────────────────────────────────────────────────────────────────────


def _digest(path: str) -> _uc.Digest:
    """What the dashboard reads of one rollout (see ``usage_common.Digest``):
    a day's and a session's tokens are fresh input + output, as claude_code's."""
    file_id = _uuid_from_name(os.path.basename(path))
    meta: dict = {"sessionId": None}
    turns = _turns(path, meta)
    builder = _uc.DigestBuilder()
    for turn in turns:
        if isinstance(turn, _User):
            builder.user(turn.at, turn.stamp, counted=True, message_id=turn.key)
            continue
        inp = turn.input - turn.cached
        if inp < 0:
            inp = turn.input
        builder.reply(turn.at, model=turn.model, input=inp, output=turn.output, cache_read=turn.cached,
                      cache_write=turn.cache_write, tokens=inp + turn.output, message_id=turn.key)
    session_id = meta["sessionId"] or file_id
    # A sub-agent's rollout counts as (and under) its root thread, whose own rollout names it.
    title = meta.get("title") or ("" if session_id != file_id else "Untitled Session")
    return builder.build(file_id=session_id, session_id=session_id, title=title)


_DIGESTS = _uc.Digests(_digest, kept_as="codex")


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    return _uc.dashboard_from_digests(_DIGESTS, _discover(), days=days, zone=_resolve_tz(tz), provider="openai")


def dashboard(*, window: dict) -> dict:
    """UsageStats — codex's /api/usage payload. Wraps aggregate_for_dashboard."""
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _start_ms, _end_ms, derived_days = _uc.window_to_ms(window)
    return aggregate_for_dashboard(days=derived_days or 30, tz="local")


# ─────────────────────────────────────────────────────────────────────────────
# Shared entries-based views — bound to this module's discovery + parser.
# (Delegates + __all__ copied from antigravity/usage.py:258-298.)
# ─────────────────────────────────────────────────────────────────────────────

def _session_of(path: str) -> str:
    """The session a rollout belongs to: its first ``session_meta``'s (one line read)."""
    try:
        with open(path, "r") as f:
            first = json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        first = {}
    payload = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    sid = (payload or {}).get("session_id") or (payload or {}).get("id")
    return sid if isinstance(sid, str) and sid else _uuid_from_name(os.path.basename(path))


_SOURCE = _uc.Source(discover=lambda: get_session_files(), parse_file=parse_file, session_of=_session_of)


def build_summary(session_meta: dict, entries: list) -> dict:
    return _uc.build_summary(session_meta, entries)


def analytics(*, window: dict) -> dict:
    return _uc.analytics(_SOURCE, window=window)


def summary(*, window: dict) -> dict:
    return _uc.summary(_SOURCE, window=window)


def summary_card(*, window: dict) -> dict:
    return _uc.summary_card(_SOURCE, window=window)


def list_sessions() -> dict:
    return _uc.list_sessions(_SOURCE)


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    return _uc.get_session(_SOURCE, session_id, window=window)


def aggregate_for_sync(*, since_date: Optional[str] = None) -> dict:
    return _uc.aggregate_for_sync(_SOURCE, since_date=since_date)


# Canonical sync surface name (used by services/usage_sync.py).
sync_payload = aggregate_for_sync


__all__ = [
    "get_session_files", "parse_file", "aggregate_for_dashboard", "dashboard",
    "build_summary", "analytics", "summary", "summary_card", "list_sessions",
    "get_session", "aggregate_for_sync", "sync_payload",
]
