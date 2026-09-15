"""
OpenClaw gateway streaming.

POSTs one user message to the gateway's OpenAI-compatible
``/v1/chat/completions`` with the session key in the session header, and
yields normalized events. Keepalives are the dispatcher's job (the chat
router emits heartbeats while a turn is quiet), so this reads the stream
directly.

The request names the OpenClaw agent that owns the session (the ``<agent>``
of ``agent:<agent>:web:<8hex>``) as ``model: openclaw/<agent>`` and in
``x-openclaw-agent-id``. The session key alone does not select an owner: with
more than one agent configured (``agents.ownership: "explicit"``) OpenClaw
rejects ``openclaw/default`` with "has no explicit owner".
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx

from services.cowork_agent.adapters.openclaw.agent_db import agent_from_session_key
from services.cowork_agent.adapters.openclaw.paths import (
    OPENCLAW_API_URL,
    OPENCLAW_GATEWAY_TOKEN,
    OPENCLAW_MODEL,
    OPENCLAW_SESSION_HEADER,
)

_DEFAULT_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)
_AGENT_HEADER = "x-openclaw-agent-id"
_READY_TIMEOUT_SECONDS = 20.0
_READY_POLL_SECONDS = 0.25


async def wait_for_agent(agent_id: str, timeout: float = _READY_TIMEOUT_SECONDS) -> str | None:
    """Wait until the gateway serves ``openclaw/<agent_id>`` (its
    ``/v1/models`` list).

    An agent the CLI has just added reaches the running gateway on its next
    config hot-reload, a moment after the write; a turn sent before that fails
    with "Unknown agent". Returns None once the agent is served, or when the
    gateway cannot be asked (the turn then reports the gateway's own error);
    otherwise, after ``timeout`` seconds, a message saying so.
    """
    url = OPENCLAW_API_URL.replace("/chat/completions", "/models")
    wanted = f"openclaw/{agent_id}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
        while True:
            try:
                response = await client.get(url, headers={"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}"})
                response.raise_for_status()
                served = {m.get("id") for m in response.json().get("data") or [] if isinstance(m, dict)}
            except (httpx.HTTPError, ValueError, AttributeError):
                return None
            if wanted in served:
                return None
            if loop.time() >= deadline:
                return (
                    f"OpenClaw has not loaded agent {agent_id!r} after {timeout:.0f}s. "
                    "Check `openclaw config validate` and the gateway log for a skipped reload."
                )
            await asyncio.sleep(_READY_POLL_SECONDS)


def request_target(session_key: str) -> tuple[str, dict[str, str]]:
    """The ``model`` and extra headers that route a turn to the session's
    agent; the configured model alias when the key names no agent."""
    agent_id = agent_from_session_key(session_key)
    if not agent_id:
        return OPENCLAW_MODEL, {}
    return f"openclaw/{agent_id}", {_AGENT_HEADER: agent_id}


async def stream_to_normalized(question: str, session_key: str) -> AsyncIterator[dict]:
    """Yield ``{type: "token", token}`` per delta; on an HTTP, transport or
    in-stream gateway error, a single ``{type: "error", error}``. The caller
    emits ``done``."""
    model, agent_headers = request_target(session_key)
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            async with client.stream(
                "POST",
                OPENCLAW_API_URL,
                headers={
                    "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                    "Content-Type": "application/json",
                    OPENCLAW_SESSION_HEADER: session_key,
                    **agent_headers,
                },
                json={
                    "model": model,
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
                    if chunk.get("error"):
                        # A turn that fails after the 200 ends with an
                        # ``{"error": {...}}`` chunk and no choices.
                        err = chunk["error"]
                        message = err.get("message") if isinstance(err, dict) else str(err)
                        yield {"type": "error", "error": f"OpenClaw error: {message or err}"}
                        return
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    content = (choices[0].get("delta") or {}).get("content")
                    if content:
                        yield {"type": "token", "token": content}
    except httpx.HTTPError as exc:
        yield {"type": "error", "error": f"OpenClaw transport error: {exc}"}
