"""
Antigravity (agy) usage — discovery + parser + dashboard aggregator.

Loaded through the usage capability. Same public contract as claude_code/codex;
the entries-based views (summary/analytics/sessions/summary_card/get_session/sync)
are delegated to :mod:`services.cowork_agent.adapters.usage_common` via a
``Source`` bound to this module's discovery + parser, and the dashboard is
``usage_common.dashboard_from_digests``, so its rows have Claude's and Codex's shape.

Sources:
  * **usage** — ``conversations/<uuid>.db`` (:mod:`tokens`): one record per model
    call with the provider's fresh input, cache-read, cache-write and output
    tokens, each dated by the step the call produced. These are agy's own
    per-call usage records (they equal what agy reports over ACP and writes on
    the transcript's steps), so the figures are **exact**, not estimates. A call
    that failed before the provider answered has no usage record: it has no
    entry, never zero tokens. Cost is unknown
    (agy records none): an entry's cost is empty (the shared views count it
    as missing).
  * **turns** — the store's ``USER_INPUT`` steps (one per message the person
    sent); the transcript ``brain/<uuid>/…/transcript_full.jsonl`` adds only the
    title and the tool names of each call's step.
  * **sub-agents** — a conversation agy ran for another (``conversation_parents``
    or ``parent_conversation_id`` in ``conversation_summaries.db``) counts under
    its root conversation, as Claude's and Codex's sub-agents do.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, tzinfo
from threading import Lock
from typing import Iterator, Optional

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.antigravity import transcript as _t
from services.cowork_agent.adapters.antigravity.paths import CONVERSATIONS_DIR, transcript_path
from services.cowork_agent.adapters.antigravity.tokens import UNREAD, Conversation, conversation_usage, store_stamp

def _resolve_tz(tz: str) -> tzinfo:
    return _uc.resolve_tz(tz)


def _provider_for(model: str | None) -> str:
    low = (model or "").lower()
    if "gemini" in low:
        return "google"
    if "claude" in low:
        return "anthropic"
    if "gpt" in low or "oss" in low:
        return "open"
    return "google"


# ── Conversations and their roots ─────────────────────────────────────────────

_parents_kept: tuple[tuple, dict[str, str]] = ((), {})
_parents_lock = Lock()


def _parents() -> dict[str, str]:
    """child conversation id → its parent's, from agy's index (kept until it changes)."""
    global _parents_kept
    db = _t._SUMMARIES_DB
    stamp = store_stamp(db)
    with _parents_lock:
        if _parents_kept[0] == stamp:
            return _parents_kept[1]
    parents: dict[str, str] = {}
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        except sqlite3.Error:
            con = None
        if con is not None:
            try:
                for query in ("SELECT conversation_id, parent_conversation_id FROM conversation_summaries",
                              "SELECT conversation_id, parent_id FROM conversation_parents"):
                    try:
                        for child, parent in con.execute(query):
                            if isinstance(child, str) and isinstance(parent, str) and parent and parent != child:
                                parents[child] = parent
                    except sqlite3.Error:
                        continue   # an older index without this column or table
            finally:
                con.close()
    with _parents_lock:
        _parents_kept = (stamp, parents)
    return parents


def _root(conversation_id: str, parents: Optional[dict[str, str]] = None) -> str:
    """The top-level conversation a sub-agent's conversation belongs to."""
    parents = _parents() if parents is None else parents
    seen = {conversation_id}
    current = conversation_id
    while current in parents and parents[current] not in seen:
        current = parents[current]
        seen.add(current)
    return current


def _conversation_id(path: str) -> str:
    name = os.path.basename(str(path))
    return name[:-3] if name.endswith(".db") else name


def _discover() -> list[str]:
    """Every agy conversation store on disk, failed conversations included."""
    try:
        return sorted(str(p) for p in CONVERSATIONS_DIR.glob("*.db") if p.is_file())
    except OSError:
        return []


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """Conversation stores (``agent_id`` accepted for contract symmetry, ignored)."""
    return _discover()


def session_of(path: str) -> str:
    return _root(_conversation_id(path))


# ── One conversation, read once per change ────────────────────────────────────


class _Parsed:
    __slots__ = ("meta", "entries", "unreported_ms", "digest")

    def __init__(self, meta: dict, entries: list, unreported_ms: list[int], digest: _uc.Digest) -> None:
        self.meta = meta
        self.entries = entries              # every entry, unwindowed, in time order
        self.unreported_ms = unreported_ms  # when each call without a usage record ran
        self.digest = digest


def _transcript_facts(conversation_id: str) -> tuple[str, dict[int, list[str]]]:
    """The first prompt (the title) and each step's tool names, streamed."""
    title = ""
    tools: dict[int, list[str]] = {}
    try:
        stream = transcript_path(conversation_id).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return title, tools
    with stream:
        for line in stream:
            try:
                step = json.loads(line)
            except ValueError:
                continue
            if not isinstance(step, dict):
                continue
            kind = step.get("type")
            if kind == "USER_INPUT" and not title:
                title = _t.strip_user_request(step.get("content") or "")[:80]
            elif kind == "PLANNER_RESPONSE" and isinstance(step.get("step_index"), int):
                names = list(_t.iter_tool_names(step))
                if names:
                    tools[step["step_index"]] = names
    return title, tools


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _build(conversation_id: str, root: str, conversation: Conversation) -> _Parsed:
    title, tools = _transcript_facts(conversation_id) if conversation.calls or conversation.user_times else ("", {})
    sub_agent = root != conversation_id
    meta: dict = {"sessionId": root, "sessionFile": conversation_id}
    if title and not sub_agent:
        meta["title"] = title

    timeline: list[tuple[float, int, object]] = [(at, 0, None) for at in conversation.user_times]
    timeline += [(c.at, 1, c) for c in conversation.reported if c.at is not None]
    timeline.sort(key=lambda item: (item[0], item[1]))

    entries: list = []
    builder = _uc.DigestBuilder()
    last_user: Optional[float] = None
    for at, kind, call in timeline:
        moment = datetime.fromtimestamp(at, tz=timezone.utc)
        if kind == 0:
            entries.append({"role": "user", "timestamp": _ms(at)})
            builder.user(moment, moment.isoformat(), counted=True)
            last_user = at
            continue
        model = call.model or "unknown"
        duration_ms = _ms(at - last_user) if last_user is not None else None
        last_user = None    # the first call after a prompt answers it
        entries.append({
            "role": "assistant",
            "usage": {
                "input": call.fresh_input, "output": call.output,
                "cacheRead": call.cache_read, "cacheWrite": call.cache_write,
                "totalTokens": call.fresh_input + call.output + call.cache_read + call.cache_write,
                "cost": {},     # agy records no cost: the shared views count it as missing, not free
            },
            "provider": _provider_for(model),
            "model": model,
            "messageId": call.message_id,
            "timestamp": _ms(at),
            "stopReason": None,
            "toolNames": tools.get(call.step, []) if call.step is not None else [],
            "toolResultCounts": {"total": 0, "errors": 0},
            "durationMs": duration_ms,
        })
        builder.reply(moment, model=model, input=call.fresh_input, output=call.output,
                      cache_read=call.cache_read, cache_write=call.cache_write,
                      tokens=call.fresh_input + call.output, message_id=call.message_id)
    unreported = sorted(_ms(c.at) for c in conversation.unreported if c.at is not None)
    digest = builder.build(file_id=root, session_id=root,
                           title="" if sub_agent else (title or "Untitled Session"))
    return _Parsed(meta, entries, unreported, digest)


_kept: dict[str, tuple[tuple, _Parsed]] = {}
_kept_lock = Lock()


def _stamp(conversation_id: str, root: str) -> tuple:
    try:
        st = os.stat(transcript_path(conversation_id))
        transcript = (st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        transcript = (0, 0, 0)
    return store_stamp(CONVERSATIONS_DIR / f"{conversation_id}.db"), transcript, root


def _parsed(path: str) -> _Parsed:
    conversation_id = _conversation_id(path)
    root = _root(conversation_id)
    stamp = _stamp(conversation_id, root)
    with _kept_lock:
        kept = _kept.get(conversation_id)
    if kept is not None and kept[0] == stamp:
        return kept[1]
    conversation = conversation_usage(conversation_id)
    parsed = _build(conversation_id, root, conversation)
    if conversation is not UNREAD and _stamp(conversation_id, root) == stamp:   # a failed read is never kept
        with _kept_lock:
            _kept[conversation_id] = stamp, parsed
    return parsed


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """One conversation (its store's path, or its id) as ``(meta, entries)`` in
    the shared shape: a user entry per prompt, an assistant entry per model
    call that has a usage record, each at its own time, within the window."""
    parsed = _parsed(path)
    entries = [e for e in parsed.entries
               if not (start_ms and e["timestamp"] < start_ms) and not (end_ms and e["timestamp"] > end_ms)]
    meta = dict(parsed.meta)
    meta["unreportedCalls"] = sum(1 for at in parsed.unreported_ms
                                  if not (start_ms and at < start_ms) and not (end_ms and at > end_ms))
    return meta, entries


# ── /api/usage dashboard (UsageStats) ─────────────────────────────────────────


class _Digests:
    """``usage_common.Digests``' reader for a conversation, whose usage spans
    its store, the store's ``-wal`` and its transcript (one file's stamp can't
    tell when it changed)."""

    @staticmethod
    def read(paths: list[str]) -> Iterator[tuple[str, Optional[_uc.Digest]]]:
        with _kept_lock:
            for gone in _kept.keys() - {_conversation_id(p) for p in paths}:
                del _kept[gone]
        for path in paths:
            try:
                yield path, _parsed(path).digest
            except Exception:
                yield path, None


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    days = max(1, min(days, 365))
    zone = _resolve_tz(tz)
    paths = _discover()
    stats = _uc.dashboard_from_digests(_Digests, paths, days=days, zone=zone, provider="")
    # A model's provider follows from the model (agy runs Gemini, Claude and GPT-OSS models).
    for row in stats["by_model"]:
        row["provider_id"] = _provider_for(row["model_id"])
    # Kept for older clients: agy's figures are its own usage records, not estimates.
    stats["estimated_tokens"] = False
    return stats


def dashboard(*, window: dict) -> dict:
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _s, _e, derived = _uc.window_to_ms(window)
    return aggregate_for_dashboard(days=derived or 30, tz="local")


# ── Shared entries-based views ────────────────────────────────────────────────

_SOURCE = _uc.Source(discover=lambda: get_session_files(), parse_file=parse_file, session_of=session_of)


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


sync_payload = aggregate_for_sync


__all__ = [
    "get_session_files", "parse_file", "aggregate_for_dashboard", "dashboard",
    "build_summary", "analytics", "summary", "summary_card", "list_sessions",
    "get_session", "aggregate_for_sync", "sync_payload", "session_of",
]
