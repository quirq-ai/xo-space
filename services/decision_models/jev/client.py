"""TypeSafe Jev's SystemOne endpoint (``POST /v1/systemone``): the only
module that knows Jev's host and key name."""

from __future__ import annotations

from services.decision_models import transport

BASE_URL = "https://api.typesafe.ai"
ENV_API_KEY = "TYPESAFE_API_KEY"
#: Jev's error statuses (https://docs.typesafe.ai/api.md).
KINDS = {401: "auth", 422: "invalid", 429: "rate", 529: "overloaded"}


async def systemone(body: dict, *, timeout: float) -> transport.Reply:
    """``body`` is ``{state, model, questions}``."""
    return await transport.post(f"{BASE_URL}/v1/systemone", body, env_key=ENV_API_KEY, timeout=timeout,
                                kinds=KINDS)
