"""
Shared building blocks for `/providers/status`.

Three things live here so the per-agent adapters can stay tiny:

1. ``_read_xo_models`` — pulls the current ``models`` section out of xo.json
   (with a DEFAULTS fallback when the file is missing). The endpoint reports
   only on providers the manifest currently marks ``enabled: true``.

2. OAuth probes — ``claude_oauth_connected`` runs ``claude auth status
   --json``; ``codex_oauth_connected`` checks ``$CODEX_HOME/auth.json``.
   Both are agent-independent because they query CLI-local state. Both are
   best-effort: any failure (binary missing, timeout, parse error) is
   treated as ``connected: false`` since this endpoint drives a frontend
   tile, not an operational alert — richer error paths already exist on
   ``/models/status``.

3. ``build_providers_status`` — composes the response. Adapters pass two
   callables that report whether each API key is present in *their* env
   source (openclaw `.env`, hermes `.env`, claude_code `os.environ`).
   Disabled providers are omitted from the response entirely; the frontend
   already drives section visibility off xo.json's `enabled` flags.
"""

from __future__ import annotations

import asyncio
from utils.commands import run
import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from services.cowork_agent.project_layout import xo_projects_root
from services.xo_manifest import build_static_manifest

CLAUDE_BIN_ENV = "CLAUDE_CLI_PATH"
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT_SECONDS = 10.0


# ── xo.json ──────────────────────────────────────────────────────────────────


def _read_xo_models(agent: str) -> dict[str, Any]:
    """Return xo.json's ``models`` section; fall back to DEFAULTS for `agent`.

    The cascade rule (parent ``enabled=false`` → children false) is already
    applied to whatever is on disk, so callers can trust leaf flags directly.
    """
    try:
        path = xo_projects_root() / ".xo" / "xo.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("models"), dict):
                return data["models"]
    except Exception:
        # Manifest unreadable — fall through to defaults so the endpoint
        # still returns *something* useful for the frontend.
        pass
    return build_static_manifest(agent).get("models", {})


def _leaf_enabled(node: Any, *keys: str) -> bool:
    """Walk `node` via `keys`; return True iff the final dict has ``enabled: true``."""
    cur: Any = node
    for k in keys:
        if not isinstance(cur, dict):
            return False
        cur = cur.get(k)
    return bool(isinstance(cur, dict) and cur.get("enabled"))


# ── .env parsing ─────────────────────────────────────────────────────────────


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` parser. Returns ``{}`` if the file is absent or unreadable.

    Mirrors hermes' gateway_pool helper in spirit — we re-implement instead of
    importing to keep the dependency arrows pointing one way.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


# ── OAuth probes (agent-independent) ─────────────────────────────────────────


def codex_oauth_connected() -> bool:
    """True iff ``$CODEX_HOME/auth.json`` (or ``~/.codex/auth.json``) exists."""
    home = (os.getenv("CODEX_HOME", "") or "").strip() or str(Path.home() / ".codex")
    return (Path(home) / "auth.json").is_file()


async def claude_auth_status(
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run ``claude auth status --json`` once and return the parsed payload.

    Returns ``{}`` on any failure (binary missing, non-zero exit, timeout,
    malformed JSON) so callers can key off ``loggedIn`` / ``authMethod`` /
    ``apiKeySource`` without their own error handling. This drives a frontend
    tile, not an operational alert, so every failure degrades to "empty".

    ``env`` is the environment the probe subprocess runs with (``None`` inherits
    the current process env). Pass the *same* env the chat subprocess uses so the
    status reflects the auth the CLI will actually resolve to — e.g. an adapter
    that strips a shadowed ``ANTHROPIC_API_KEY`` when a native login is present
    must strip it here too, or the probe reports a key the CLI would never use.
    """
    binary = (os.getenv(CLAUDE_BIN_ENV, "") or "").strip() \
        or shutil.which(DEFAULT_CLAUDE_BIN) \
        or DEFAULT_CLAUDE_BIN
    if os.path.isabs(binary) and not os.path.isfile(binary):
        return {}
    res = await run([binary, "auth", "status", "--json"], env=env, timeout=timeout, separate_stderr=True)
    if not res.ok:
        return {}
    try:
        payload = json.loads(res.output or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def claude_oauth_connected(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Return the ``loggedIn`` flag from ``claude auth status --json``.

    Any failure path → False, on purpose. The frontend tile only cares
    whether the user is authenticated *right now*; if the binary is missing
    or the JSON is malformed we treat that as "not connected" rather than
    bubbling an error.
    """
    return bool((await claude_auth_status(timeout)).get("loggedIn"))


# ── Composer ─────────────────────────────────────────────────────────────────


async def build_providers_status(
    agent: str,
    *,
    anthropic_key_present: Callable[[], bool],
    openai_key_present: Callable[[], bool],
    openrouter_key_present: Callable[[], bool],
    claude_oauth_present: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Compose the ``/providers/status`` response for `agent`.

    The ``*_key_present`` callables let each adapter resolve keys from its
    own env source without this module knowing where that source lives.

    ``claude_oauth_present``, when supplied, overrides the ``oauth.claude_code``
    verdict — letting an adapter that has already probed ``claude auth status``
    reuse that single result (and reflect the *resolved* auth method) instead of
    this module spawning the CLI again via ``claude_oauth_connected``.
    """
    models = _read_xo_models(agent)

    oauth: dict[str, dict] = {}
    if _leaf_enabled(models, "oauth", "claude_code"):
        connected = claude_oauth_present() if claude_oauth_present is not None \
            else await claude_oauth_connected()
        oauth["claude_code"] = {"connected": bool(connected)}
    if _leaf_enabled(models, "oauth", "codex"):
        oauth["codex"] = {"connected": codex_oauth_connected()}

    api_keys: dict[str, dict] = {}
    if _leaf_enabled(models, "api_keys", "anthropic"):
        api_keys["anthropic"] = {"connected": bool(anthropic_key_present())}
    if _leaf_enabled(models, "api_keys", "openai"):
        api_keys["openai"] = {"connected": bool(openai_key_present())}
    if _leaf_enabled(models, "api_keys", "openrouter"):
        api_keys["openrouter"] = {"connected": bool(openrouter_key_present())}

    return {"agent": agent, "oauth": oauth, "api_keys": api_keys}


__all__ = [
    "build_providers_status",
    "claude_auth_status",
    "claude_oauth_connected",
    "codex_oauth_connected",
    "parse_env_file",
]
