"""Decision-model API keys: saved in the Space's secret store, never shown again.

A decision model's key configures the router (a Space-level service), not
the active agent, so it is kept where every Space secret is kept: the store
behind Setup → Secrets (``scopes.SecretsScope`` over ``agent_env``). With an
install's ``QUIRQ_SECRETS_FILE`` that is ``<state>/secrets/secrets.env``
(owner-only, loaded again at every start), so a saved key persists across
restarts. Saving also updates this process's environment, and every provider
reads its key per call, so a key works from the next routing decision on,
with no restart.

The env var a key lives in is declared by its provider module (``ENV_KEY``),
so this module never names one. Nothing here returns a key's value: status
says only whether one is set and where it came from.
"""

from __future__ import annotations

import os
from typing import Optional

import services.decision_models as dm
from services.cowork_agent import scopes
from services.errors import ServiceError

_FORBIDDEN = ("\n", "\r", "\x00")
MAX_KEY_CHARS = 512


class KeyError_(ServiceError):
    """A decision-model key request was refused."""


def _module(provider: str):
    try:
        return dm.provider_module(provider)
    except ValueError:
        raise KeyError_("unknown_provider", f"There is no decision model {provider!r} "
                                            f"(choose one of: {', '.join(dm.PROVIDERS)}).", status=404) from None


def _stored(env_key: str) -> Optional[str]:
    for entry in scopes.SecretsScope().load():
        if (entry.get("key") or "").strip() == env_key:
            return entry.get("value") or ""
    return None


def status(provider: str) -> dict:
    mod = _module(provider)
    stored = _stored(mod.ENV_KEY)
    in_env = bool((os.environ.get(mod.ENV_KEY) or "").strip())
    source = "saved" if stored else ("environment" if in_env else None)
    return {"provider": provider, "label": mod.LABEL, "docs_url": mod.DOCS_URL, "env_key": mod.ENV_KEY,
            "configured": mod.configured(), "source": source}


def list_status() -> list[dict]:
    return [status(provider) for provider in dm.PROVIDERS]


def set_key(provider: str, api_key: object) -> dict:
    """Save ``provider``'s key to the secret store and this process."""
    mod = _module(provider)
    value = api_key.strip() if isinstance(api_key, str) else ""
    if not value:
        raise KeyError_("missing_key", "api_key is required.")
    if len(value) > MAX_KEY_CHARS or any(ch in value for ch in _FORBIDDEN) or any(ch.isspace() for ch in value):
        raise KeyError_("invalid_key", "That does not look like an API key (one line, no spaces).")
    scopes.SecretsScope().upsert(mod.ENV_KEY, value)
    return status(provider)


def clear_key(provider: str) -> dict:
    """Remove a saved key. A key exported in the server's environment stays in use
    until the server restarts without it (status then says ``environment``)."""
    mod = _module(provider)
    scopes.SecretsScope().delete(mod.ENV_KEY)
    return status(provider)
