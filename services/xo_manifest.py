"""
xo.json — agent capability + live-status manifest.

The xo-cowork frontend reads <projects root>/.xo/xo.json to decide which UI
sections (Models / Data / Channels / Secrets) and which sub-items inside
each (Slack, Telegram, Google Drive, OAuth providers, API-key providers)
to render.

Two layers live in the file:

1. Static capability flags — sourced per agent from
   `config/agents/<agent>/capabilities.json`. Written once at server
   startup, overwriting any prior file.
2. Live status (openclaw only, for now) — the full response from
   `/openclaw/models/status` and `/openclaw/channels/status`, embedded
   under a `status` key in the corresponding section. Refreshed in a
   background task at startup and on every endpoint hit. Deferred for
   claude_code and hermes until they have their own status sources.

Each agent's `capabilities.json` is the source of truth; the cascade rule
(parent `enabled=false` → children false) is applied at build time so future
toggle changes propagate without hand-editing leaves.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

from services.cowork_agent.project_layout import xo_projects_root




_AGENTS_DIR = Path(__file__).resolve().parents[1] / "config" / "agents"


def _capabilities_path(agent: str) -> Path:
    """``config/agents/<agent>/capabilities.json`` — the per-agent static
    capability flags (Models / Data / Channels / Secrets and their leaves).

    Sourced from the agent's own config dir, not a hardcoded table, so adding
    a backend is "drop a folder" and no core file names an agent.
    """
    return _AGENTS_DIR / agent / "capabilities.json"


def _load_capabilities(agent: str) -> dict | None:
    """Return the parsed capabilities dict for ``agent``, or None if the agent
    has no ``capabilities.json`` (unknown / not installed / malformed)."""
    path = _capabilities_path(agent)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _known_agents() -> list[str]:
    """Agents that ship a ``capabilities.json`` (for valid-list messages)."""
    if not _AGENTS_DIR.is_dir():
        return []
    return sorted(
        d.name for d in _AGENTS_DIR.iterdir()
        if d.is_dir() and (d / "capabilities.json").is_file()
    )


_LOCK = asyncio.Lock()


def _xo_path() -> Path:
    return xo_projects_root() / ".xo" / "xo.json"


def _force_false_children(node: Any) -> None:
    """Recursively set every `enabled` flag inside `node` to False."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "enabled":
            node[key] = False
        elif isinstance(value, dict):
            _force_false_children(value)


def _apply_cascade(manifest: dict) -> dict:
    """If a top-level section's `enabled` is False, force every child
    `enabled` inside that section to False as well."""
    for section_name, section in manifest.items():
        if not isinstance(section, dict):
            continue
        if section.get("enabled") is False:
            for child_key, child_val in section.items():
                if child_key == "enabled":
                    continue
                _force_false_children(child_val)
    return manifest


def build_static_manifest(agent: str) -> dict:
    """Return a fresh manifest dict for `agent` with cascade applied and
    the `agent` field set. Caller owns the returned dict."""
    capabilities = _load_capabilities(agent)
    if capabilities is None:
        raise KeyError(f"unknown agent '{agent}' (valid: {', '.join(_known_agents())})")
    manifest: dict[str, Any] = {"agent": agent}
    manifest.update(copy.deepcopy(capabilities))
    return _apply_cascade(manifest)


def resolve_agent_name() -> str:
    """The active agent name — the single resolver used across core code.

    ``AGENT_NAME`` (if set) wins and is returned verbatim, even if it names no
    manifest (callers map that to a 501). When unset, the default is resolved
    by the registry (``DEFAULT_AGENT`` env → single-manifest → documented
    ``claude_code`` fallback). The agent-name default literal lives only there —
    no core file hardcodes an agent name.
    """
    explicit = (os.getenv("AGENT_NAME", "") or "").strip()
    if explicit:
        return explicit
    from services.cowork_agent.registry.agent_registry import get_active_agent
    return get_active_agent().name


async def _write_atomic(manifest: dict) -> Path:
    """Write `manifest` to xo.json via temp-file + rename. Caller holds the lock."""
    path = _xo_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


async def write_static_manifest() -> None:
    """Resolve AGENT_NAME, build the static manifest, write to disk.

    Non-fatal on every failure: logs and returns. Unknown agent value
    means we deliberately do NOT touch any existing file.
    """
    agent = resolve_agent_name()
    if _load_capabilities(agent) is None:
        print(
            f"⚠️ xo.json: AGENT_NAME='{agent}' is not a known agent "
            f"(valid: {', '.join(_known_agents())}) — skipping"
        )
        return

    try:
        manifest = build_static_manifest(agent)
        async with _LOCK:
            path = await _write_atomic(manifest)
        print(f"✅ xo.json: wrote static manifest for agent={agent} at {path}")
    except Exception as exc:
        print(f"⚠️ xo.json: write failed (non-fatal): {exc}")


async def patch_status(section: str, status_payload: dict) -> None:
    """Read xo.json, set `<section>.status = status_payload`, write atomically.

    No-op (with a log) when the file doesn't exist yet, when the section
    isn't a dict, or when serialisation fails. The endpoint hooks fire
    this as a background task, so failures must never propagate.
    """
    try:
        path = _xo_path()
        async with _LOCK:
            if not path.exists():
                print(f"⚠️ xo.json: patch_status({section}) skipped — file not present yet")
                return
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"⚠️ xo.json: patch_status({section}) read failed: {exc}")
                return
            if not isinstance(manifest, dict) or not isinstance(manifest.get(section), dict):
                print(f"⚠️ xo.json: patch_status({section}) skipped — section missing")
                return
            manifest[section]["status"] = status_payload
            await _write_atomic(manifest)
    except Exception as exc:
        print(f"⚠️ xo.json: patch_status({section}) failed (non-fatal): {exc}")


def _resolve_status_fetchers(agent: str):
    """Return (fetch_models, fetch_channels) for the active agent, or
    (None, None) if the agent has no live-status sources yet.

    Resolved through the ``models_status`` / ``channels_status`` capabilities so
    no backend is named here; an agent missing either capability falls through
    to a no-op. Loaded lazily so a missing adapter module never breaks the
    import graph for callers that don't need it.
    """
    from services.cowork_agent.adapters.loader import try_load_capability

    ms = try_load_capability("models_status", agent=agent)
    cs = try_load_capability("channels_status", agent=agent)
    fetch_models = getattr(ms, "get_models_status", None) if ms else None
    fetch_channels = getattr(cs, "get_channels_status", None) if cs else None
    return fetch_models, fetch_channels


async def seed_agent_status() -> None:
    """Background task: fetch the current agent's models + channels status in
    parallel and patch them into xo.json. Logs and continues on per-section
    failures so one bad fetch doesn't poison the other.

    No-op (with a log line) for agents without a status source (e.g.
    claude_code today). Driven by the current AGENT_NAME env var so a
    restart with a different agent automatically routes to the right
    adapter.
    """
    agent = resolve_agent_name()
    fetch_models, fetch_channels = _resolve_status_fetchers(agent)
    if fetch_models is None or fetch_channels is None:
        print(f"   xo.json: no live-status source for agent={agent} — skipping seed")
        return

    async def _seed(section: str, fetch) -> None:
        print(f"   xo.json: {section} status seed → fetching…")
        try:
            result = await fetch()
        except Exception as exc:
            print(f"⚠️ xo.json: {section} status seed failed: {exc}")
            return
        await patch_status(section, result)
        print(f"✅ xo.json: {section} status seeded")

    await asyncio.gather(
        _seed("models", fetch_models),
        _seed("channels", fetch_channels),
        return_exceptions=True,
    )
