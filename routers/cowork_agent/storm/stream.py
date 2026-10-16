"""Storm's streams and its state log (ch 17 §2.4; ch 09 §6):

  GET /api/chat/stream?chat=&session=&kinds=&after_seq=
        the one global SSE stream; resume with ``Last-Event-ID`` (sent by
        EventSource on its own reconnects, so it wins) or ``after_seq``
  GET /api/chat/sessions/{id}/stream?kinds=&after_seq=
        one session's events, the same shapes; 404 for an unknown session
  GET /api/chat/events?chat=&session=&kinds=&after_seq=&before_seq=&limit=
        the state log as the stream sends it, paged by seq (``limit`` counts
        stored events; a step comes back as the blocks it gives); with only
        ``before_seq``, the newest page before it

Every frame from a stored event has an SSE ``id:``: ``N`` for the last (or
only) frame of seq N, ``N:k`` for an earlier one. ``Last-Event-ID`` takes
either; ``after_seq=N`` means after all of seq N. A malformed id is 400
``bad_last_event_id``. 503 ``engine_elsewhere`` when another server runs the
engine.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import stream as storm_stream
from services.cowork_agent.storm.stream import Filter, Frame
from services.cowork_agent.storm.stream.frames import MAX_SEQ, resume_point
from services.errors import ServiceError

router = APIRouter()

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def _sse(frames: AsyncIterator[Frame]) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        async for frame in frames:
            yield frame.sse()
    return StreamingResponse(body(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/api/chat/stream")
async def global_stream(chat: Optional[str] = None, session: Optional[str] = None, kinds: Optional[str] = None,
                        after_seq: Optional[int] = Query(None, ge=0, le=MAX_SEQ),
                        last_event_id: Optional[str] = Header(None, alias="Last-Event-ID")) -> StreamingResponse:
    try:
        point = resume_point(after_seq, last_event_id)
        bus = storm_stream.current()
    except ServiceError as exc:
        raise storm_error(exc)
    return _sse(storm_stream.stream(bus, Filter.parse(chat=chat, session=session, kinds=kinds), point))


@router.get("/api/chat/sessions/{session_id}/stream")
async def session_stream(session_id: str, kinds: Optional[str] = None,
                         after_seq: Optional[int] = Query(None, ge=0, le=MAX_SEQ),
                         last_event_id: Optional[str] = Header(None, alias="Last-Event-ID")) -> StreamingResponse:
    try:
        point = resume_point(after_seq, last_event_id)
        bus = storm_stream.current()
        await storm_stream.require_session(bus, session_id)
    except ServiceError as exc:
        raise storm_error(exc)
    return _sse(storm_stream.stream(bus, Filter.parse(session=session_id, kinds=kinds), point))


@router.get("/api/chat/events")
async def state_log(chat: Optional[str] = None, session: Optional[str] = None, kinds: Optional[str] = None,
                    after_seq: int = Query(0, ge=0, le=MAX_SEQ),
                    before_seq: Optional[int] = Query(None, ge=1, le=MAX_SEQ),
                    limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        return await storm_stream.events_page(storm_stream.current(), Filter.parse(chat=chat, session=session,
                                                                                   kinds=kinds),
                                              after=after_seq, before=before_seq, limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)
