"""Levanto Sage's ``POST /decide``: the only module that knows Sage's host
and key name."""

from __future__ import annotations

from services.decision_models import transport

BASE_URL = "https://sage.levanto.ai"
ENV_API_KEY = "LEVANTO_API_KEY"
#: Sage's error statuses (https://docs.levanto.ai).
KINDS = {400: "invalid", 401: "auth", 402: "balance", 422: "invalid", 429: "rate"}


async def decide(content: str, question: dict, *, timeout: float) -> transport.Reply:
    """``content`` plus one ``question``."""
    return await transport.post(f"{BASE_URL}/decide", {"content": content, "question": question},
                                env_key=ENV_API_KEY, timeout=timeout, kinds=KINDS)
