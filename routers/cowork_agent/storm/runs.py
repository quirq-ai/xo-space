"""Storm's sub-agent runs (ch 17 §2.3; ch 14 §4, §11):

  GET  /api/chat/sessions/{id}/runs?state=&kind=&tree=&depth=&after_seq=&before_seq=&limit=
       the session's runs (working ones and the log of ended ones), oldest
       first, paged by seq, with the session's counts ("14 sub-agents ran");
       ``state`` and ``kind`` take comma-separated values; ``tree=true``
       nests children to ``depth`` levels
  GET  /api/chat/runs/{run}
       one run with its ancestry (Workspace › Project › Thread › Sub-agent)
  GET  /api/chat/runs/{run}/transcript?after=&limit=
       its own steps, read from where its CLI keeps them, a page at a time
  POST /api/chat/runs/{run}/stop
       stop it now (the Undo is the UI's): 202 with the run while its stop
       is pending (its own end event confirms it), 200 if it had ended
  POST /api/chat/sessions/{id}/runs/stop
       stop every working sub-agent of the session, its root agent running
       on (ch 14 §4.3): 202 with the ids a stop was sent to, 200 if none

No route messages a sub-agent (§0.82). ``404 run_not_found``,
``session_not_found``; ``400 bad_filter``, ``bad_after``; ``410
transcript_gone`` with the stored ``summary``; ``409 run_not_live``; ``501
unsupported`` with ``feature`` (``runs.stop``, ``runs.read``); 503
``engine_elsewhere`` when another server runs the engine.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Response

from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import engine
from services.cowork_agent.storm.runs import operations
from services.errors import ServiceError

router = APIRouter()

#: Who a stop from this route names.
PERSON = "person"


def _values(text: str | None) -> list[str]:
    return [v.strip() for v in (text or "").split(",") if v.strip()]


@router.get("/api/chat/sessions/{session_id}/runs")
async def list_runs(session_id: str, state: Optional[str] = None, kind: Optional[str] = None, tree: bool = False,
                    depth: int = Query(1, ge=1), after_seq: int = Query(0, ge=0),
                    before_seq: Optional[int] = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        return await operations.list_runs(engine.current(), session_id, states=_values(state), kinds=_values(kind),
                                          tree=tree, depth=depth, after_seq=after_seq, before_seq=before_seq,
                                          limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    try:
        return await operations.get_run(engine.current(), run_id)
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/runs/{run_id}/transcript")
async def read_transcript(run_id: str, after: Optional[str] = None,
                          limit: int = Query(50, ge=1, le=500)) -> dict:
    try:
        return await operations.read_transcript(engine.current(), run_id, after=after, limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/sessions/{session_id}/runs/stop")
async def stop_all(session_id: str, response: Response) -> dict:
    try:
        stopping = await operations.stop_all(engine.current(), session_id, by=PERSON)
    except ServiceError as exc:
        raise storm_error(exc)
    response.status_code = 202 if stopping else 200
    return {"stopping": stopping}


@router.post("/api/chat/runs/{run_id}/stop")
async def stop_run(run_id: str, response: Response) -> dict:
    try:
        run, sent = await operations.stop_run(engine.current(), run_id, by=PERSON)
    except ServiceError as exc:
        raise storm_error(exc)
    response.status_code = 202 if sent else 200
    return {"run": run, "stopping": sent}
