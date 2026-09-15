"""
SSE stream parser for Hermes's OpenAI-compatible `/v1/chat/completions`.

Hermes streams chunks like::

    data: {"id": "chatcmpl-...", "object": "chat.completion.chunk", "choices":
           [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]}
    data: {"id": ..., "choices": [{"delta": {"content": "Hello"}, ...}]}
    ...
    data: {"id": ..., "choices": [{"delta": {}, "finish_reason": "stop"}],
           "usage": {...}}
    data: [DONE]

The session id travels in the ``X-Hermes-Session-Id`` header both ways: the
adapter sends XO's session id (hermes creates a new session under it) and the
response echoes it; this module hands that back as ``native_session_id`` on the
final done event. The HTTP-level call lives here so the adapter stays thin.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from services.cowork_agent.adapters.hermes.paths import (
    HERMES_API_TOKEN,
    HERMES_API_URL,
    HERMES_MODEL,
    HERMES_SESSION_HEADER,
)


_DEFAULT_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)


def _build_headers(session_id: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {HERMES_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    # The api_server uses a supplied id as the session id, creating the session
    # under it when it is new, and echoes it back in the response header.
    if session_id and HERMES_SESSION_HEADER:
        headers[HERMES_SESSION_HEADER] = session_id
    return headers


def _build_body(question: str, *, stream: bool) -> dict[str, Any]:
    return {
        "model": HERMES_MODEL,
        "stream": stream,
        "messages": [{"role": "user", "content": question}],
    }


def _completions_url(gateway_base: str | None) -> str:
    """Resolve the ``/v1/chat/completions`` endpoint on the target gateway.

    When ``gateway_base`` is None, fall back to the env-configured default
    gateway (the one hermes.sh manages on port 8642). When it's a base URL
    like ``http://127.0.0.1:8643``, append the chat-completions path. This
    is how we route per-profile chats to per-profile gateways.
    """
    if gateway_base:
        return gateway_base.rstrip("/") + "/v1/chat/completions"
    return HERMES_API_URL


async def stream_to_normalized(
    question: str,
    session_id: str | None,
    *,
    gateway_base: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield normalized events for a hermes streaming call.

    Emits ``{type: "token", token: <str>}`` per delta and finishes with
    exactly one ``{done: True, native_session_id: <id|None>}``. On HTTP
    error, emits a single ``{type: "error", error: <msg>}`` followed by
    the done event with ``native_session_id`` set to whatever (if anything)
    the header surfaced.
    """
    if not HERMES_API_TOKEN:
        yield {"type": "error", "error": "Hermes API_SERVER_KEY is not set"}
        yield {"done": True, "native_session_id": None}
        return

    native_session_id: str | None = None
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        try:
            async with client.stream(
                "POST",
                _completions_url(gateway_base),
                headers=_build_headers(session_id),
                json=_build_body(question, stream=True),
            ) as response:
                native_session_id = (
                    response.headers.get(HERMES_SESSION_HEADER) or session_id or None
                )

                if response.status_code != 200:
                    body = await response.aread()
                    yield {
                        "type": "error",
                        "error": f"Hermes API error: {response.status_code} {body.decode(errors='replace')}",
                    }
                    yield {"done": True, "native_session_id": native_session_id}
                    return

                # Hermes interleaves OpenAI-shape `data: {...}` chunks with
                # custom progress events (``event: hermes.tool.progress``
                # followed by a `data:` payload describing the running tool).
                # We track the most recent ``event:`` name so we can route the
                # next `data:` line correctly: OpenAI chunks have no preceding
                # ``event:`` (or one named ``message``), tool progress lines
                # do. Without forwarding the progress events, the frontend
                # sees nothing for the 10-30 s tool-call phase and assumes
                # the chat hung.
                current_event: str | None = None
                async for line in response.aiter_lines():
                    if not line:
                        # Blank line terminates an SSE event. Reset the event
                        # tag so the next data: chunk isn't mis-attributed.
                        current_event = None
                        continue
                    if line.startswith("event: "):
                        current_event = line[7:].strip()
                        continue
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Hermes tool progress: emit a model-loading event with
                    # the tool label so the UI shows live activity.
                    if current_event and current_event.startswith("hermes.tool."):
                        label = chunk.get("label") or chunk.get("tool") or ""
                        yield {"type": "model-loading", "label": label}
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    token = delta.get("content")
                    if token:
                        yield {"type": "token", "token": token}
        except httpx.HTTPError as exc:
            yield {"type": "error", "error": f"Hermes transport error: {exc}"}

    yield {"done": True, "native_session_id": native_session_id}
