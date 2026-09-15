"""
Stateless utility helpers shared across the bridge.

Kept free of FastAPI / httpx imports so any module can use them without pulling
in server dependencies. Anything here is pure-Python and side-effect free
(except `parse_jsonl`, which reads from disk).
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.registry.settings import (
    _INVALID_AGENT_ID_CHARS,
    _LEADING_DASHES,
    _MAX_AGENT_PAYLOAD_BYTES,
    _TRAILING_DASHES,
    _VALID_AGENT_ID,
)


# ── Time / id helpers ────────────────────────────────────────────────────────


def ms_to_iso(ms: int | float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def short_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Agent id normalization ───────────────────────────────────────────────────


def normalize_agent_id(value: str | None) -> str:
    """Match OpenClaw's normalizeAgentId (session-key) rules."""
    if value is None:
        return "main"
    trimmed = value.strip()
    if not trimmed:
        return "main"
    normalized = trimmed.lower()
    if _VALID_AGENT_ID.fullmatch(normalized):
        return normalized
    cleaned = _INVALID_AGENT_ID_CHARS.sub("-", normalized)
    cleaned = _LEADING_DASHES.sub("", cleaned)
    cleaned = _TRAILING_DASHES.sub("", cleaned)
    cleaned = cleaned[:64]
    return cleaned if cleaned else "main"


# ── Path / file safety ───────────────────────────────────────────────────────


def _path_must_be_under_home(path: Path) -> bool:
    home = Path.home().resolve()
    try:
        path.resolve().relative_to(home)
        return True
    except ValueError:
        return False


def parse_jsonl(path: Path) -> list[dict]:
    lines = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(json.loads(line))
    return lines


# ── Workspace-context preamble ───────────────────────────────────────────────
#
# The xo-coworker frontend prepends a "Project context" block to user prompts
# in project-scoped chats (use-chat.ts) so the model knows about AGENTS.md
# and the boot ritual. The model needs that text — it stays on disk in the
# JSONL — but rendering it as part of the user bubble in the UI is noisy and
# makes the first user message look like a "skill" injection. Strip it from
# the visible text wherever we surface user-facing strings (message
# conversion, title derivation).
#
# The marker is stable across preamble revisions; only the body text below
# the header has changed over time.
_WORKSPACE_PREAMBLE_MARKER = "\n\n---\n\n> **Project context**"


def strip_workspace_preamble(text: str) -> str:
    """Return text with the trailing workspace-context preamble removed."""
    if not text:
        return text
    idx = text.find(_WORKSPACE_PREAMBLE_MARKER)
    if idx < 0:
        return text
    return text[:idx].rstrip()


def derive_title(records: list[dict]) -> str:
    """Extract a title from the first user message text (OpenClaw/simplified format).

    OpenClaw stores a user message's ``content`` as a plain string when the
    turn came through its OpenAI-compatible endpoint, and as text blocks
    otherwise; both are read.
    """
    for r in records:
        message = r.get("message")
        if r.get("type") != "message" or not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                # Strip the preamble first: its marker starts with the blank
                # line that a leading .strip() would remove.
                text = strip_workspace_preamble(block["text"]).strip()
                if not text or text.startswith("Read HEARTBEAT.md"):
                    continue
                return text[:80] + ("..." if len(text) > 80 else "")
    return "Untitled Session"


def derive_title_native_claude(records: list[dict]) -> str:
    """Extract a title from native Claude Code JSONL records (type=user/assistant)."""
    for r in records:
        if r.get("type") != "user":
            continue
        # Skip runtime-injected meta records (Skill payloads etc.) so the
        # sidebar title is never sourced from a several-thousand-character
        # skill body when the user's first real prompt happened to load one.
        if r.get("isMeta") is True:
            continue
        msg = r.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        else:
            continue
        text = strip_workspace_preamble(text)
        if text and not text.startswith("Read HEARTBEAT.md"):
            return text[:80] + ("..." if len(text) > 80 else "")
    return "Untitled Session"


# ── Bounded file readers (used by agent-detail endpoints) ────────────────────


def _read_text_limited(path: Path, max_bytes: int = _MAX_AGENT_PAYLOAD_BYTES) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_bytes]
    except Exception:
        return None


def _read_json_file_safe(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── Secret redaction / masking ───────────────────────────────────────────────


def _redact_secrets_nested(obj):
    """Replace obvious credential fields; never return raw API keys."""
    sensitive_keys = frozenset(
        {"key", "token", "secret", "password", "accesstoken", "refreshtoken", "authorization", "apikey"}
    )
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in sensitive_keys:
                out[k] = "[configured]" if v else None
            else:
                out[k] = _redact_secrets_nested(v)
        return out
    if isinstance(obj, list):
        return [_redact_secrets_nested(x) for x in obj]
    return obj


def _summarize_auth_profiles(profiles_obj) -> dict[str, dict]:
    """Non-secret view of auth profile entries."""
    if not isinstance(profiles_obj, dict):
        return {}
    out: dict[str, dict] = {}
    for pid, p in profiles_obj.items():
        if not isinstance(p, dict):
            continue
        row = {"provider": p.get("provider"), "mode": p.get("mode")}
        if any(p.get(k) for k in ("key", "token", "secret", "password")):
            row["credentials"] = "configured"
        out[str(pid)] = row
    return out


_SENSITIVE_KEYS = {"botToken", "apiKey", "api_key", "token", "secret", "password"}


def _mask_value(v: str) -> str:
    if len(v) <= 8:
        return "****"
    return v[:4] + "*" * (len(v) - 8) + v[-4:]


def _mask_sensitive(obj: object) -> object:
    """Recursively mask sensitive fields in a JSON-like structure."""
    if isinstance(obj, dict):
        return {
            k: (_mask_value(v) if isinstance(v, str) and k in _SENSITIVE_KEYS else _mask_sensitive(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_sensitive(item) for item in obj]
    return obj
