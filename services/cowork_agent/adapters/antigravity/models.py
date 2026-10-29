"""
antigravity model listing (`/api/models`).

agy's own models, from its runtime (a model-free ``agy models``, kept per
installed binary and account), in the rows ``/api/models`` has always
served: ``{id, name, provider_id, capabilities, pricing, metadata}``.
Replaces the re-export of openclaw's listing (storm ch 05 §5, ch 04 §10),
so no adapter imports another adapter's models, and so the stale manifest
names ("Gemini 3.5 Flash", which agy 1.3.5 already rejected) are no longer
what the UI offers.
"""

from __future__ import annotations

import asyncio
import logging

from services.cowork_agent.adapters.antigravity.runtime import runtime
from services.cowork_agent.adapters.contract import ModelInfo, RuntimeFailure
from services.cowork_agent.registry.agent_registry import get_agent

log = logging.getLogger(__name__)

AGENT = "antigravity"


def model_rows(models: list[ModelInfo]) -> list[dict]:
    manifest = get_agent(AGENT)
    return [{
        "id": f"{manifest.model_prefix}/{m.id}",
        "name": m.name or m.id,
        "provider_id": AGENT,
        "capabilities": dict(manifest.model_capabilities),
        "pricing": {"prompt": 0, "completion": 0},
        "metadata": {"model": m.id, "efforts": list(m.efforts), "default": m.is_default},
    } for m in models]


def list_models() -> list[dict]:
    """The route is a plain ``def``: FastAPI runs it in a worker thread with no
    event loop of its own, so the runtime's coroutine runs here. A CLI that
    can't answer lists nothing (logged), like an agent with no models."""
    try:
        models = asyncio.run(runtime().models())
    except RuntimeFailure as exc:
        log.warning("antigravity models: %s (%s)", exc.message, exc.code)
        return []
    return model_rows(models)


__all__ = ["list_models"]
