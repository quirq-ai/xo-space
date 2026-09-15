"""
OpenClaw gateway streaming.

POSTs one user message to the gateway's OpenAI-compatible
``/v1/chat/completions`` with the session key in the session header, and
yields normalized events. Keepalives are the dispatcher's job (the chat
router emits heartbeats while a turn is quiet), so this reads the stream
directly.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from services.cowork_agent.adapters.openclaw.paths import (
    OPENCLAW_API_URL,
    OPENCLAW_GATEWAY_TOKEN,
    OPENCLAW_MODEL,
    OPENCLAW_SESSION_HEADER,
)

_DEFAULT_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)


async def stream_to_normalized(question: str, session_key: str) -> AsyncIterator[dict]:
    """Yield ``{type: "token", token}`` per delta; on an HTTP or transport
    error, a single ``{type: "error", error}``. The caller emits ``done``."""
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            async with client.stream(
                "POST",
                OPENCLAW_API_URL,
                headers={
                    "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                    "Content-Type": "application/json",
                    OPENCLAW_SESSION_HEADER: session_key,
                },
                json={
                    "model": OPENCLAW_MODEL,
                    "stream": True,
                    "messages": [{"role": "user", "content": question}],
                },
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    yield {
                        "type": "error",
                        "error": f"OpenClaw API error: {response.status_code} {body.decode(errors='replace')}",
                    }
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    content = (choices[0].get("delta") or {}).get("content")
                    if content:
                        yield {"type": "token", "token": content}
    except httpx.HTTPError as exc:
        yield {"type": "error", "error": f"OpenClaw transport error: {exc}"}
