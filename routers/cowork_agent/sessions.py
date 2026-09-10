"""
Session + message routes.

Covers the `/api/sessions/*` CRUD surface and `/api/messages/{id}`. Route
order matters here: `/api/sessions/search` must register before
`/api/sessions/{session_id}` so the literal path isn't swallowed by the
path-parameter route.
"""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent import session_transcript
from services.cowork_agent.registry.adapter_registry import list_adapters
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine.sessions_io import (
    find_session_backend,
    load_all_sessions,
)

router = APIRouter()


# ── Read side (GET) — order matters so /search beats /{session_id} ──────────


@router.get("/api/sessions")
def list_sessions(limit: int = 50, offset: int = 0):
    all_sessions = load_all_sessions()
    return all_sessions[offset : offset + limit]


@router.get("/api/sessions/search")
def search_sessions(q: str = "", limit: int = 20, offset: int = 0):
    all_sessions = load_all_sessions()
    q_lower = q.lower()
    results = []
    for s in all_sessions:
        if q_lower in (s.get("title") or "").lower():
            results.append({"session": s, "snippet": None})
    return results[offset : offset + limit]


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    all_sessions = load_all_sessions()
    for s in all_sessions:
        if s["id"] == session_id:
            return s
    return JSONResponse(status_code=404, content={"detail": "Session not found"})


@router.get("/api/messages/{session_id}")
def get_messages(session_id: str, limit: int = 50, offset: int = -1):
    # Resolve the owning backend, then read messages via that adapter's
    # sessions capability. Each adapter knows its own source (JSONL vs
    # state.db) and converter — this router stays backend-agnostic.
    backend = find_session_backend(session_id)
    mod = try_load_capability("sessions", agent=backend) if backend else None
    fn = getattr(mod, "get_messages", None) if mod else None
    all_messages = fn(session_id) if fn else []
    total = len(all_messages)

    # ``offset=-1`` means "latest page". We deliberately return ALL messages
    # in that mode (no upper bound on ``page``). The old behaviour returned
    # ``all_messages[total - limit : total]`` which slid forward as ``total``
    # grew on the next poll, dropping messages that were previously visible.
    # Returning everything keeps the rendered list stable across the 10s
    # ``refetchInterval`` and SSE-driven ``invalidateQueries``. For very long
    # sessions the response is bigger, but the conversation length is bounded
    # by the model's context window in practice; a future cursor-pagination
    # pass can revisit this if needed.
    if offset == -1:
        start = 0
        page = all_messages
    else:
        start = offset
        page = all_messages[start : start + limit]

    return {
        "total": total,
        "offset": start,
        "messages": page,
    }


# ── Write side ───────────────────────────────────────────────────────────────


@router.post("/api/sessions")
async def create_session(request: Request):
    # Frontend may call this but we create sessions via chat/prompt instead
    return {"id": str(uuid.uuid4()), "title": "New Chat"}


@router.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    directory = body.get("directory")
    if directory is None:
        return {"ok": True}

    directory = str(directory).strip()
    if not directory:
        return JSONResponse(status_code=400, content={"detail": "directory must be a non-empty string"})

    # Ask each adapter's sessions capability to set the directory; the owning
    # backend returns a result dict, the rest return None. Mutually exclusive
    # per session, so order doesn't matter and no backend is named here.
    for name in list_adapters():
        mod = try_load_capability("sessions", agent=name)
        fn = getattr(mod, "set_session_directory", None) if mod else None
        if fn is None:
            continue
        result = fn(session_id, directory)
        if result is not None:
            return result

    return JSONResponse(status_code=404, content={"detail": "Session not found"})


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    return {"ok": True}


# ── Per-session read-only extras ─────────────────────────────────────────────


@router.get("/api/sessions/{session_id}/transcript")
def session_transcript_view(session_id: str, tools: bool = False):
    """{title, messages:[{id, role, content}]} — one text bubble per turn.
    The full record (tool calls, reasoning, usage) stays on /api/messages."""
    try:
        return session_transcript.load_transcript(session_id, include_tools=tools)
    except session_transcript.SessionNotFound:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})


@router.get("/api/sessions/{session_id}/todos")
def session_todos(session_id: str):
    return {"todos": []}


@router.get("/api/sessions/{session_id}/files")
def session_files(session_id: str):
    return {"files": []}
