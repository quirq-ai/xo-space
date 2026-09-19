"""
OpenClaw chat capability.

OpenClaw uses a bespoke direct-streaming path (rather than the shared
AgentDispatcher): ``handle_prompt`` prefetches the new-session bootstrap and
registers the stream, and ``get_sse_generator`` replays it (new session) or
live-streams from the gateway (existing session). The chat router resolves
these via ``load_capability('chat', agent=<backend>)`` so it names no backend.

This is a faithful relocation of the openclaw branch that previously lived in
api/chat/routes.py — behavior is unchanged.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi.responses import JSONResponse

from services.cowork_agent.engine.chat_state import active_streams
from services.cowork_agent.adapters.openclaw.sessions import find_session_key
from services.cowork_agent.adapters.openclaw.direct_stream import (
    create_new_session,
    emit_prefetched_sse,
    find_session_id_by_key,
    openclaw_agent_id_from_prompt_body,
    stream_openclaw_to_sse,
)


async def handle_prompt(*, body: dict, text: str, session_id, agent_id, is_new_session: bool):
    """Handle POST /api/chat/prompt for openclaw. Returns the response dict."""
    if is_new_session:
        oc_agent = openclaw_agent_id_from_prompt_body(body)
        session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
        task = asyncio.create_task(
            create_new_session(text, session_key=session_key, xo_agent_id=agent_id)
        )

        new_session_id = None
        for _ in range(20):
            await asyncio.sleep(1.0)
            new_session_id = find_session_id_by_key(session_key)
            if new_session_id:
                break

        stream_id = str(uuid.uuid4())
        active_streams[stream_id] = {
            "task": task,
            "prefetched": True,
            "agent_id": agent_id,
            "backend": "openclaw",
        }
        return {"stream_id": stream_id, "session_id": new_session_id}

    # Existing openclaw session: look up the session key and stream directly
    session_key = find_session_key(session_id)
    if not session_key:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})

    stream_id = str(uuid.uuid4())
    active_streams[stream_id] = {
        "session_id": session_id,
        "text": text,
        "session_key": session_key,
        "agent_id": agent_id,
        "backend": "openclaw",
    }
    return {"stream_id": stream_id, "session_id": session_id}


def get_sse_generator(stream_id: str, stream_info: dict):
    """Return the SSE generator for an openclaw stream registered by handle_prompt."""
    if stream_info.get("prefetched"):
        # New session — replay the bootstrap response as fake SSE.
        return emit_prefetched_sse(stream_id)
    # Existing session — live stream from the gateway.
    return stream_openclaw_to_sse(stream_id)
