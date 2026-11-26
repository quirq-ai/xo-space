"""What a sign-in to an account sets up for Hermes (the ``account_listeners``
capability, run only on a Hermes Space). An ``openai`` sign-in (Codex's
ChatGPT login) becomes the default profile's ``openai-codex`` provider, its
primary model, and the gateway restarts to load it. The CLI's own copy of the
login is then deleted: its refresh token is single-use, so two holders would
invalidate each other. Default profile only, for the same reason."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from services.cowork_agent.adapters.hermes.paths import HERMES_DIR
from services.cowork_agent.registry.agent_registry import get_agent
from services.runtime_lifecycle.contract import Credential
from services.storage.atomic_write import write_text_durable
from utils.commands import run

log = logging.getLogger(__name__)

PROVIDER = "openai-codex"
MODEL = {"model.provider": PROVIDER, "model.base_url": "https://chatgpt.com/backend-api/codex",
         "model.default": "gpt-5.4"}
CONFIG_TIMEOUT_S = 30.0
RESTART_TIMEOUT_S = 90.0


def _adopt(credential: Credential) -> None:
    """``providers["openai-codex"].tokens`` in hermes's ``auth.json``, made the active provider; the pool
    entry and any suppression derived from an older login are cleared so hermes rebuilds them."""
    path = HERMES_DIR / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError) as exc:
        log.warning("hermes: auth.json unreadable (%s); starting it afresh", exc)
        data = {}
    data = data if isinstance(data, dict) else {}
    data.setdefault("version", 1)
    for derived in ("credential_pool", "suppressed_sources"):
        if isinstance(data.get(derived), dict):
            data[derived].pop(PROVIDER, None)
    if not isinstance(data.get("providers"), dict):
        data["providers"] = {}
    now = datetime.now(timezone.utc)
    tokens = {"access_token": credential.access}
    if credential.refresh:
        tokens["refresh_token"] = credential.refresh
    data["providers"][PROVIDER] = {"tokens": tokens, "last_refresh": now.isoformat().replace("+00:00", "Z"),
                                   "auth_mode": "chatgpt"}
    data.update(active_provider=PROVIDER, updated_at=now.isoformat())
    write_text_durable(path, json.dumps(data, indent=2))


async def openai_signed_in(runtime: str, credential: Credential | None) -> None:
    if credential is None:
        return
    await asyncio.to_thread(_adopt, credential)
    env = {**os.environ, "HERMES_HOME": str(HERMES_DIR)}
    for key, value in MODEL.items():
        result = await run([get_agent("hermes").binary, "config", "set", key, value], cwd=HERMES_DIR, env=env,
                           timeout=CONFIG_TIMEOUT_S, log_output=False)
        if not result.ok:
            log.warning("hermes: `config set %s` failed (%s)", key, result.output.strip()[:200])
    await asyncio.to_thread(credential.release)
    from services.cowork_agent.adapters.hermes.routes import _run_hermes_sh

    code, output = await _run_hermes_sh("restart", timeout_s=RESTART_TIMEOUT_S)
    if code != 0:
        log.warning("hermes: gateway restart failed (%s): %s", code, output.strip()[-200:])


ON_SIGN_IN = {"openai": openai_signed_in}
