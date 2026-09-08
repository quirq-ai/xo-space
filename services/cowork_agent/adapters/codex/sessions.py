"""
Codex sessions capability.

Codex tees its sessions into the per-project session index (runtime tier),
tagged ``backend:"codex"``, so the generic project-tied scan applies —
``USES_PROJECT_SESSIONS = True``. The native message store is codex's on-disk
rollout file ``~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl`` (keyed
by conversation uuid, resolved by glob — codex has no per-cwd encoded dir like
claude_code, exactly like antigravity's agy).

The listing hooks (``enrich_project_session`` / ``resolve_native_file`` /
``list_native_sessions``) and read hooks (``owns_session`` / ``get_messages`` /
``set_session_directory``) are what ``engine/sessions_io`` calls instead of
branching on the backend name.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.adapters.codex import paths as _paths
from services.cowork_agent.engine.sessions_io import find_session_file
from services.cowork_agent.engine import sessions_io as _session_index
from services.cowork_agent.helpers import iso_now, strip_workspace_preamble

USES_PROJECT_SESSIONS = True

# The ``backend`` tag codex writes on every sessionslist row it publishes;
# used to tell our rows apart from the other project-tied backends' in a shared
# index.
_BACKEND = "codex"


# ── Rollout reader ────────────────────────────────────────────────────────────


def _iter_rollout(path: Path):
    """Yield parsed rollout line dicts, skipping blanks/bad JSON (robust reader,
    mirrors antigravity/sessions.py's raw-step loop). Each line is
    ``{"timestamp":ISO8601, "type":<TOP>, "payload":{…}}``."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


# ── Listing hooks ─────────────────────────────────────────────────────────────


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Locate the codex rollout for a project-tied session (by conversation uuid).

    Cannot build a deterministic path (the rollout name embeds a date+timestamp),
    so glob by UUID via paths.find_rollout. Called with the real meta for a
    project-tied row (sessions_io.py:204-209) and with an empty dict for the
    native fallback (sessions_io.py:220) — the empty case has no nativeSessionId
    and returns None, like claude_code (claude_code/sessions.py:66-68)."""
    native = (meta or {}).get("nativeSessionId") or ""
    if not native:
        return None
    path = _paths.find_rollout(native)
    return path if (path and path.exists()) else None


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Return ``(time_created, title, effective_agent)`` by reading the rollout.

    ``time_created`` = the ``session_meta`` line's ``timestamp``; ``title`` =
    first ``event_msg/user_message.message`` (raw prompt), preamble-stripped and
    80-char truncated. Either override may be None (caller keeps its defaults,
    sessions_io.py:114-117)."""
    time_created = None
    title = None
    native = (meta or {}).get("nativeSessionId") or ""
    if native:
        path = _paths.find_rollout(native)
        if path and path.exists():
            for obj in _iter_rollout(path):
                top = obj.get("type")
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if top == "session_meta" and time_created is None:
                    time_created = payload.get("timestamp") or obj.get("timestamp")
                elif top == "event_msg" and payload.get("type") == "user_message":
                    raw = payload.get("message")
                    text = strip_workspace_preamble(raw if isinstance(raw, str) else "").strip()
                    if text and not text.startswith("<environment_context>"):
                        title = text[:80]
                        break
    return time_created, title, default_agent


def list_native_sessions() -> list[dict]:
    """codex has no non-project native session store — all sessions are project-tee'd."""
    return []


# ── Read hooks ────────────────────────────────────────────────────────────────


def owns_session(session_id: str) -> bool:
    """codex sessions are detected via the sessionslist ``backend`` tag, not a scan."""
    return False


# ── Local rollout → MessageResponse converter ─────────────────────────────────

# token_count.info.last_token_usage sub-fields (per-turn delta). §1.4: sum
# last_token_usage, NOT total_token_usage (the latter is session-cumulative).
# (UNVERIFIED) exact field names captured from 7 rollouts — confirm on an
# authed live turn (see plan §B3 risks).
_USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


def _add_usage(acc: dict | None, last: dict | None) -> dict | None:
    """Sum a token_count.last_token_usage delta onto the turn accumulator
    (§1.4: sum last_token_usage, never total_token_usage — the latter is
    session-cumulative and would multiply)."""
    if not isinstance(last, dict):
        return acc
    if acc is None:
        acc = {k: 0 for k in _USAGE_FIELDS}
    for k in _USAGE_FIELDS:
        try:
            acc[k] += int(last.get(k) or 0)
        except (TypeError, ValueError):
            pass
    return acc


def _message_tokens(acc: dict | None) -> dict | None:
    """Turn accumulator → the MessageResponse.data.tokens shape
    (engine/messages.py:409-415). §1.4 containment: cached ⊆ input, so subtract
    to avoid double-counting; reasoning ⊆ output, surfaced for display only."""
    if not acc:
        return None
    inp = int(acc.get("input_tokens") or 0)
    cr = int(acc.get("cached_input_tokens") or 0)
    return {
        "input": max(inp - cr, 0),
        "output": int(acc.get("output_tokens") or 0),
        "reasoning": int(acc.get("reasoning_output_tokens") or 0),
        "cache_read": cr,
        "cache_write": int(acc.get("cache_write_input_tokens") or 0),
    }


def _text_from_output(output) -> str:
    """Flatten a *_output / content list ([{type,text}] or str) into plain text."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(
            b.get("text", "") for b in output
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def _tool_input(payload: dict) -> dict:
    """Best-effort tool ``input`` dict for the chip. function_call.arguments is a
    JSON string; custom_tool_call.input is a shell command string."""
    if "arguments" in payload:                       # function_call
        raw = payload.get("arguments")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {"arguments": raw}
            return parsed if isinstance(parsed, dict) else {"arguments": raw}
        return raw if isinstance(raw, dict) else {}
    raw = payload.get("input")                        # custom_tool_call ("exec")
    if isinstance(raw, str):
        return {"command": raw}
    return raw if isinstance(raw, dict) else {}


def _convert(session_id: str, path: Path) -> list[dict]:
    """Convert a codex rollout ``.jsonl`` into xo-cowork MessageResponse dicts
    (same shape engine/messages.convert_native_claude_messages produces).

    Walks rollout lines in order; one codex turn → one user bubble (from
    event_msg/user_message) then one assistant message whose parts are the
    turn's tool chips (function_call/custom_tool_call paired to *_output by
    call_id) followed by the final assistant text (response_item output_text).
    reasoning is encrypted and skipped; agent_message is a dup of the assistant
    output_text and skipped; usage is the summed token_count for the turn."""
    messages: list[dict] = []
    counter = [0]
    current_model: list[str | None] = [None]   # latest turn_context.model
    a_parts: list[dict] = []                    # assistant part-data dicts (this turn)
    a_ts: list[str | None] = [None]             # first assistant timestamp (this turn)
    a_usage: list[dict | None] = [None]         # summed token_count (this turn)
    tools_by_call: dict[str, dict] = {}         # call_id → tool part-data (fill output)

    def _flush_assistant() -> None:
        if not a_parts:
            a_ts[0] = None
            a_usage[0] = None
            tools_by_call.clear()
            return
        counter[0] += 1
        mid = f"{session_id}_m{counter[0]}"
        ts = a_ts[0] or iso_now()
        parts = [
            {"id": f"{mid}_p{i}", "message_id": mid, "session_id": session_id,
             "time_created": ts, "data": data}
            for i, data in enumerate(a_parts)
        ]
        messages.append({
            "id": mid, "session_id": session_id, "time_created": ts,
            "data": {
                "role": "assistant", "model_id": current_model[0], "provider_id": None,
                "cost": None, "tokens": _message_tokens(a_usage[0]),
                "finish": "stop", "error": None,
            },
            "parts": parts,
        })
        a_parts.clear()
        a_ts[0] = None
        a_usage[0] = None
        tools_by_call.clear()

    for obj in _iter_rollout(path):
        top = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ts = obj.get("timestamp") or iso_now()

        if top == "turn_context":
            model = payload.get("model")
            if model:
                current_model[0] = model
            continue

        if top == "event_msg":
            ptype = payload.get("type")
            if ptype == "user_message":
                # New user turn → close the previous assistant turn, emit the bubble.
                _flush_assistant()
                raw = payload.get("message")
                text = strip_workspace_preamble(raw if isinstance(raw, str) else "").strip()
                if not text or text.startswith("<environment_context>"):
                    continue
                counter[0] += 1
                mid = f"{session_id}_m{counter[0]}"
                messages.append({
                    "id": mid, "session_id": session_id, "time_created": ts,
                    "data": {"role": "user"},
                    "parts": [{
                        "id": f"{mid}_p0", "message_id": mid, "session_id": session_id,
                        "time_created": ts, "data": {"type": "text", "text": text},
                    }],
                })
            elif ptype == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                a_usage[0] = _add_usage(a_usage[0], info.get("last_token_usage"))
            # agent_message (dup of response_item output_text), task_started,
            # patch_apply_end, task_complete → ignored for message rendering.
            continue

        if top == "response_item":
            ptype = payload.get("type")
            if a_ts[0] is None:
                a_ts[0] = ts

            if ptype == "message":
                if payload.get("role") != "assistant":
                    continue                     # user/developer handled via event_msg
                for block in payload.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        text = block.get("text") or ""
                        if text:
                            a_parts.append({"type": "text", "text": text})

            elif ptype in ("function_call", "custom_tool_call"):
                call_id = payload.get("call_id") or payload.get("id") or ""
                name = payload.get("name") or "tool"
                data = {
                    "type": "tool", "tool": name, "call_id": call_id,
                    "state": {
                        "status": "completed", "input": _tool_input(payload),
                        "output": None, "metadata": None, "title": name,
                        "time_start": ts, "time_end": ts, "time_compacted": None,
                    },
                }
                a_parts.append(data)
                if call_id:
                    tools_by_call[call_id] = data

            elif ptype in ("function_call_output", "custom_tool_call_output"):
                data = tools_by_call.get(payload.get("call_id") or "")
                if data is not None:
                    data["state"]["output"] = _text_from_output(payload.get("output"))
                    # TODO(codex): function_call_output error signalling is UNVERIFIED
                    # (codex may use neither is_error nor success:false). Harmless if
                    # absent — status stays "completed".
                    if payload.get("is_error") or payload.get("success") is False:
                        data["state"]["status"] = "error"

            # reasoning (encrypted_content) → skipped entirely.
            continue

        # session_meta / turn / world_state / unknown → ignored.

    _flush_assistant()
    return messages


def get_messages(session_id: str) -> list:
    """Converted messages for a codex session (empty if no rollout)."""
    path = find_session_file(session_id)
    if not path:
        return []
    return _convert(session_id, path)


# ── Directory update (verbatim from antigravity/sessions.py) ───────────────────


def _persist_session_directory(session_id: str, directory: str) -> bool:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for project_id, _project_dir, index in _session_index.iter_project_session_indexes():
        for key, meta in index.items():
            if meta.get("sessionId") != session_id:
                continue
            # Same-index rows from the other project-tied backends are not ours:
            # the PATCH route loops adapters and takes the first non-None, so
            # without this we would service (and rewrite) another backend's row.
            # Untagged legacy rows stay claimable — a missing/empty ``backend``
            # predates the tag, so first-adapter-wins still applies there.
            backend = meta.get("backend")
            if isinstance(backend, str) and backend and backend != _BACKEND:
                continue
            # One row, one shard file, one atomic replace. The whole-document
            # rewrite this used to do is what made two concurrent writers lose
            # a row (syncplan T19, inherited from T4).
            row = dict(meta)
            history = list(row.get("directoryHistory") or [])
            history.append({"directory": directory, "selectedAt": now_ms})
            row["directoryHistory"] = history[-200:]
            row["directory"] = directory
            row["updatedAt"] = now_ms
            if _session_index.write_session_row(project_id, key, row):
                return True
    return False


def set_session_directory(session_id: str, directory: str) -> dict | None:
    if _persist_session_directory(session_id, directory):
        return {"ok": True, "session_id": session_id, "directory": directory}
    return None


__all__ = [
    "USES_PROJECT_SESSIONS", "resolve_native_file", "enrich_project_session",
    "list_native_sessions", "owns_session", "get_messages", "set_session_directory",
]
