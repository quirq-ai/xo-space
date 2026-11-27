"""
Claude Code usage — discovery + parser + dashboard aggregator.

Loaded by ``services.cowork_agent.engine.usage_loader.load_usage_module()`` when
``AGENT_NAME=claude_code``. Same public contract as openclaw.

Source: ``~/.claude/projects/<encoded>/*.jsonl`` — every Anthropic transcript
Claude Code writes. Discovery walks the projects tree directly (NOT filtered
through xo-project ``sessionslist.json`` indices), so the UI shows the full
Claude Code usage the way the ``claude /usage`` CLI does.

Token shape uses Anthropic raw fields: ``input_tokens``, ``output_tokens``,
``cache_read_input_tokens``, ``cache_creation_input_tokens``. Cost is always
0.0 — Anthropic JSONL has no billing field. Records are deduped by
``message.id`` (every streaming chunk shares one id); a token-tuple dedup
historically over-counted by ~75% when tool_result records were interleaved.

A ``type:"user"`` record only counts as a user message if it carries actual
user text — pure tool_result records are protocol noise, not turns.

Only discovery, ``parse_file`` and the ``/api/usage`` dashboard are
Claude-Code-specific. The entries-based views (summary/analytics/sessions/sync)
are shared with openclaw via
:mod:`services.cowork_agent.adapters.usage_common` — claude_code's parser emits
the same normalized entry shape on purpose.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Optional

from services.cowork_agent.adapters import usage_common as _uc


# ─────────────────────────────────────────────────────────────────────────────
# Configuration + helpers (used by discovery / parse_file / dashboard)
# ─────────────────────────────────────────────────────────────────────────────


def _projects_root() -> Path:
    return Path(os.getenv("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")))


def _resolve_tz(tz: str) -> tzinfo:
    return _uc.resolve_tz(tz)


def _record_time(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _iter_jsonl(path: str, *, strict_io: bool = False) -> Iterator[dict]:
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        if strict_io:
            raise


def _user_text(msg: dict) -> str:
    """A user record's text: its string content, or its first non-empty text block."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    return text
    return ""


#: Text Claude Code puts in a user record itself: an interruption marker, a
#: background task's notice, a reminder, a local command's output.
_CLI_TEXT = ("[Request interrupted", "<task-notification", "<system-reminder", "<local-command-stdout",
             "<local-command-caveat")


def _sent(record: dict, msg: dict) -> bool:
    """A user record is a message sent to the session (by a person, or by XO
    for them) when it carries text Claude Code didn't add itself: not a meta
    record (an image's caption, a reminder), a compaction summary, a sub-agent's
    prompt from its parent (``isSidechain``), or one of ``_CLI_TEXT``. A tool
    result carries no text."""
    if record.get("isMeta") or record.get("isCompactSummary") or record.get("isSidechain"):
        return False
    text = _user_text(msg)
    return bool(text) and not text.startswith(_CLI_TEXT)


def _title(record: dict) -> str:
    msg = record.get("message", {}) or {}
    if record.get("type") != "user" or not msg or not _sent(record, msg):
        return ""
    text = _user_text(msg)
    return text[:80] + ("..." if len(text) > 80 else "")


def _call_usage(msg: dict) -> dict:
    """A model call's usage, or {} for none: a ``<synthetic>`` record is one
    Claude Code wrote itself (an API error's message), with no call behind it."""
    if msg.get("model") == "<synthetic>":
        return {}
    usage = msg.get("usage")
    return usage if isinstance(usage, dict) else {}


def _session_of(path: str) -> str:
    """The session a transcript's turns belong to: its name, or, for a
    sub-agent's (``<session>/subagents/agent-*.jsonl``), its parent's."""
    parts = Path(path).parts
    if len(parts) >= 3 and parts[-2] == "subagents":
        return parts[-3]
    base = parts[-1] if parts else ""
    return base[: -len(".jsonl")] if base.endswith(".jsonl") else base


# ─────────────────────────────────────────────────────────────────────────────
# File discovery — every JSONL under ~/.claude/projects/<encoded>/
# ─────────────────────────────────────────────────────────────────────────────


def _discover() -> list[str]:
    """Return every ``.jsonl`` file under the projects root, one level deep.

    Matches the ``claude /usage`` enumeration: each project directory holds
    one JSONL per session. No filtering on xo-project membership — we want
    the full picture the UI's Settings → Usage tab needs.
    """
    root = _projects_root()
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for proj in sorted(root.iterdir()):
        if not proj.is_dir() or proj.name.startswith("."):
            continue
        for f in sorted(proj.iterdir()):
            if f.is_file() and f.name.endswith(".jsonl"):
                out.append(str(f))
        # Sub-agents' transcripts (the Task tool's), after their sessions' own: a
        # sub-agent's file copies some of its parent's records, which count once.
        out.extend(str(f) for f in sorted(proj.glob("*/subagents/*.jsonl")) if f.is_file())
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Contract — discovery + parser (Claude-Code-specific)
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """Every Claude Code native JSONL on disk.

    ``agent_id`` is accepted for contract symmetry with openclaw but ignored —
    Claude Code is single-tenant per deployment.
    """
    return _discover()


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse a single Anthropic JSONL transcript.

    Returns ``(meta, entries)`` with entries shaped like openclaw's parser
    output (role=user|assistant, normalized usage keys). Dedups by
    ``message.id`` so streaming records of the same API call count once.
    """
    meta: dict = {"sessionId": None, "sessionFile": os.path.basename(path)}
    # Default session id = filename without `.jsonl` (= Anthropic native id); a sub-agent's: its parent's.
    if os.path.basename(path).endswith(".jsonl"):
        meta["sessionId"] = _session_of(path)

    return meta, list(_iter_entries(_iter_jsonl(path), meta, start_ms=start_ms, end_ms=end_ms))


def _iter_entries(
    records: Iterator[dict], meta: dict, *, start_ms: int | None, end_ms: int | None,
) -> Iterator[dict]:
    seen_message_ids: set[str] = set()
    last_user_ts: int | None = None

    for record in records:
        rtype = record.get("type")
        msg = record.get("message", {}) or {}
        ts_str = record.get("timestamp")
        rt = _record_time(ts_str)
        ts_epoch_ms = int(rt.timestamp() * 1000) if rt else None

        if ts_epoch_ms is not None:
            if start_ms and ts_epoch_ms < start_ms:
                continue
            if end_ms and ts_epoch_ms > end_ms:
                continue

        # Prefer record/message-supplied sessionId when present.
        sid = record.get("sessionId") or msg.get("sessionId")
        if isinstance(sid, str) and sid:
            meta["sessionId"] = sid

        if rtype == "user":
            if msg and _sent(record, msg):
                last_user_ts = ts_epoch_ms
                uuid = record.get("uuid")
                yield {"role": "user", "timestamp": ts_epoch_ms,
                       "messageId": uuid if isinstance(uuid, str) and uuid else None}
            continue

        if rtype != "assistant":
            continue

        usage_data = _call_usage(msg)
        if not usage_data:
            continue

        msg_id = msg.get("id")
        if isinstance(msg_id, str) and msg_id:
            if msg_id in seen_message_ids:
                continue
            seen_message_ids.add(msg_id)

        inp = int(usage_data.get("input_tokens", 0) or 0)
        out = int(usage_data.get("output_tokens", 0) or 0)
        cache_r = int(usage_data.get("cache_read_input_tokens", 0) or 0)
        cache_w = int(usage_data.get("cache_creation_input_tokens", 0) or 0)

        duration_ms = (ts_epoch_ms - last_user_ts) if (last_user_ts and ts_epoch_ms) else None

        yield {
            "role": "assistant",
            # Use openclaw-style keys so the shared build_summary can be reused.
            "usage": {
                "input": inp,
                "output": out,
                "cacheRead": cache_r,
                "cacheWrite": cache_w,
                "totalTokens": inp + out + cache_r + cache_w,
                "cost": {"total": 0.0, "input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
            },
            "provider": "anthropic",
            "model": msg.get("model") or "claude",
            "messageId": msg_id if isinstance(msg_id, str) and msg_id else None,
            "timestamp": ts_epoch_ms,
            "stopReason": msg.get("stop_reason"),
            "toolNames": [],
            "toolResultCounts": {"total": 0, "errors": 0},
            "durationMs": duration_ms,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Claude-Code-specific (separate from the shared entries views): each
# transcript's digest, kept until the file changes, folded by
# usage_common.dashboard_from_digests (empty-session mtime parity, a
# response-time clamp). Always returns total_cost=0.0 (no billing in Anthropic
# JSONL).
# ─────────────────────────────────────────────────────────────────────────────


def _digest(path: str) -> Optional[_uc.Digest]:
    """What the dashboard reads of one transcript (see ``usage_common.Digest``),
    read line by line."""
    if not os.path.basename(path).endswith(".jsonl"):
        return None
    session_id = _session_of(path)       # = Anthropic native id (a sub-agent's: its parent's)
    sub_agent = session_id != os.path.basename(path)[: -len(".jsonl")]
    title = ""
    turns = _uc.DigestBuilder()
    for record in _iter_jsonl(path):
        rtype = record.get("type")
        msg = record.get("message", {}) or {}
        if not msg:
            continue
        if not title and not sub_agent:
            title = _title(record)
        rt = _record_time(record.get("timestamp"))
        if rt is None:
            continue
        if rtype == "user":
            uuid = record.get("uuid")
            turns.user(rt, record.get("timestamp"), counted=_sent(record, msg),
                       message_id=uuid if isinstance(uuid, str) and uuid else None)
            continue
        if rtype != "assistant":
            continue
        usage_data = _call_usage(msg)
        if not usage_data:
            continue
        msg_id = msg.get("id")
        inp = int(usage_data.get("input_tokens", 0) or 0)
        out = int(usage_data.get("output_tokens", 0) or 0)
        turns.reply(
            rt, model=msg.get("model") or "claude", input=inp, output=out,
            cache_read=int(usage_data.get("cache_read_input_tokens", 0) or 0),
            cache_write=int(usage_data.get("cache_creation_input_tokens", 0) or 0),
            tokens=inp + out, message_id=msg_id if isinstance(msg_id, str) else None,
        )
    # A sub-agent's file counts as its session (``file_id``) but names it no title.
    return turns.build(file_id=session_id, session_id=session_id, title=title or ("" if sub_agent else "Untitled Session"))


_DIGESTS = _uc.Digests(_digest, kept_as="claude_code")


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    return _uc.dashboard_from_digests(_DIGESTS, _discover(), days=days, zone=_resolve_tz(tz), provider="anthropic")


def dashboard(*, window: dict) -> dict:
    """UsageStats — claude_code's /api/usage payload. Wraps aggregate_for_dashboard."""
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _start_ms, _end_ms, derived_days = _uc.window_to_ms(window)
    return aggregate_for_dashboard(days=derived_days or 30, tz="local")


# ─────────────────────────────────────────────────────────────────────────────
# Shared entries-based views — bound to this module's discovery + parser.
# Implementations live in usage_common; the public names/signatures below are
# unchanged so the loader and usage_sync keep working.
# ─────────────────────────────────────────────────────────────────────────────

def _card_entries(path: str, *, start_ms: int | None, end_ms: int | None) -> Iterator[dict]:
    return _iter_entries(_iter_jsonl(path, strict_io=True), {}, start_ms=start_ms, end_ms=end_ms)


_SOURCE = _uc.Source(discover=lambda: get_session_files(), parse_file=parse_file, card_entries=_card_entries,
                     session_of=_session_of)


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
