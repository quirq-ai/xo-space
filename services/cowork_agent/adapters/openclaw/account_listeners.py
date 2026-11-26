"""What a sign-in to an account sets up for OpenClaw (the ``account_listeners``
capability, run only on an OpenClaw Space). An ``openai`` sign-in (Codex's
ChatGPT login) becomes the gateway's OpenAI profile and default model, the
main agent's ``openai-codex`` OAuth profile, and ``OPENAI_CODEX_ACCESS_TOKEN``
in OpenClaw's own ``env_file`` (its manifest's, even when ``QUIRQ_SECRETS_FILE``
moves the secrets store)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR, OPENCLAW_JSON
from services.cowork_agent.registry.agent_env import upsert_env_entry
from services.cowork_agent.registry.agent_registry import get_active_agent
from services.runtime_lifecycle.contract import Credential
from services.storage.atomic_write import write_text_durable

log = logging.getLogger(__name__)

PRIMARY_MODEL = "openai/gpt-5.5"
TOKEN_VAR = "OPENAI_CODEX_ACCESS_TOKEN"


def _read(path: Path) -> dict[str, Any] | None:
    """The JSON object at ``path``; None (logged) when it is broken, so a hand edit is never clobbered."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("openclaw: %s unreadable (%s); left alone", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _branch(data: dict, *keys: str) -> dict:
    for key in keys:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    return data


def _write(path: Path, data: dict) -> None:
    write_text_durable(path, json.dumps(data, indent=2) + "\n")


def _gateway_profile(email: str) -> None:
    """``auth.profiles["openai:<email>"]`` and the default model, in an existing ``openclaw.json``."""
    if not OPENCLAW_JSON.is_file() or (data := _read(OPENCLAW_JSON)) is None:
        return
    _branch(data, "auth", "profiles")[f"openai:{email}"] = {"provider": "openai", "mode": "oauth", "email": email}
    _branch(data, "agents", "defaults", "model")["primary"] = PRIMARY_MODEL
    _write(OPENCLAW_JSON, data)


def _agent_profile(credential: Credential) -> None:
    """The main agent's ``openai-codex:<email>`` OAuth profile, the store OpenClaw reads at run time."""
    path = AGENTS_DIR / "main" / "agent" / "auth-profiles.json"
    data = _read(path) if path.is_file() else {"version": 1, "profiles": {}}
    if data is None:
        return
    _branch(data, "profiles")[f"openai-codex:{credential.email}"] = {
        "type": "oauth", "provider": "openai-codex", "access": credential.access, "refresh": credential.refresh,
        "expires": credential.expires_ms, "email": credential.email}
    _write(path, data)


def _adopt(credential: Credential) -> None:
    upsert_env_entry(TOKEN_VAR, credential.access, path=get_active_agent().env_file)
    if credential.email:
        _gateway_profile(credential.email)
        if credential.refresh and credential.expires_ms:
            _agent_profile(credential)


async def openai_signed_in(runtime: str, credential: Credential | None) -> None:
    if credential is not None:
        await asyncio.to_thread(_adopt, credential)


ON_SIGN_IN = {"openai": openai_signed_in}
