"""BFF routes for the knowledge brain (services/brain).

  GET    /api/brain/status                          model, loop, counts, last run
  GET    /api/brain/sources                         sources, and projects that could be
  POST   /api/brain/sources                         {project}               local only
  DELETE /api/brain/sources/{source_id}                                     local only
  POST   /api/brain/learn                           {source_id?, force?}    local only, 202
  GET    /api/brain/runs?limit=
  GET    /api/brain/pieces?q=&source_id=&novel=&shared=&limit=&offset=
  GET    /api/brain/pieces/{piece_id}               notes, evidence, links, hypotheses apart
  GET    /api/brain/pieces/{piece_id}/similar
  GET    /api/brain/relations
  POST   /api/brain/recall                          {cue, context_source?, context?, limit?, hops?, explore?, reinforce?, answer?}
  POST   /api/brain/use                             {piece_ids}             used together: links grow
  GET    /api/brain/graph?source_id=&limit=
  GET    /api/brain/patterns
  GET    /api/brain/analogies
  GET    /api/brain/hypotheses
  GET    /api/brain/findings?status=open|done|all
  PATCH  /api/brain/findings/{finding_id}           {status}
  POST   /api/brain/discover                                                local only
  POST   /api/brain/goals                           {goal, context_source?} 501 without a model
  GET    /api/brain/goals
  GET    /api/brain/goals/{goal_id}
  POST   /api/brain/designs/{design_id}/approve                             local only
  POST   /api/brain/designs/{design_id}/build                               local only, 202
  GET    /api/brain/experiences
  POST   /api/brain/experiences                     {goal, result, lessons?, design_id?, source_id?, piece_ids?, pattern_ids?}

Thin over :mod:`services.brain.service`; typed errors become HTTP through
``bff/errors.py``. Routes that run programs (git, a build, a test command)
or approve work also require a same-machine client, like the scheduler's.
Bodies are strict: an unknown key is a 422.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, StrictInt, StrictStr

from routers.browser_guard import is_local_mutation
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services.brain import service
from services.brain.store import BrainError

router = APIRouter()


def _require_local(request: Request) -> None:
    if not is_local_mutation(request):
        raise HTTPException(status_code=403, detail={
            "code": "local_only", "message": "This brain action needs a local client and a same-origin browser request."})


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except BrainError as exc:
        raise http_error(exc)


async def _acall(coro):
    try:
        return await coro
    except BrainError as exc:
        raise http_error(exc)


class AddSourceBody(ForbidExtra):
    project: StrictStr


class LearnBody(ForbidExtra):
    source_id: Optional[StrictStr] = None
    force: bool = False


class RecallBody(ForbidExtra):
    cue: StrictStr
    context_source: Optional[StrictStr] = None
    context: Optional[StrictStr] = Field(None, max_length=2000)
    limit: int = Field(10, ge=1, le=50)
    hops: int = Field(2, ge=0, le=4)
    explore: bool = False
    reinforce: bool = True
    answer: bool = False   # with a model: also write an answer from what was recalled


class UseBody(ForbidExtra):
    piece_ids: list[StrictInt]


class FindingBody(ForbidExtra):
    status: StrictStr


class GoalBody(ForbidExtra):
    goal: StrictStr
    context_source: Optional[StrictStr] = None


class ExperienceBody(ForbidExtra):
    goal: StrictStr = Field(..., max_length=1000)
    result: StrictStr
    lessons: StrictStr = Field("", max_length=2000)
    design_id: Optional[StrictInt] = None
    source_id: Optional[StrictStr] = None
    piece_ids: list[StrictInt] = Field(default_factory=list, max_length=200)
    pattern_ids: list[StrictInt] = Field(default_factory=list, max_length=200)


@router.get("/api/brain/status")
def brain_status() -> dict:
    return _call(service.status)


@router.get("/api/brain/sources")
def brain_sources() -> dict:
    return _call(service.list_sources)


@router.post("/api/brain/sources", status_code=201, dependencies=[Depends(_require_local)])
def brain_add_source(body: AddSourceBody) -> dict:
    return _call(service.add_source, body.project)


@router.delete("/api/brain/sources/{source_id}", dependencies=[Depends(_require_local)])
def brain_remove_source(source_id: str) -> dict:
    return _call(service.remove_source, source_id)


@router.post("/api/brain/learn", status_code=202, dependencies=[Depends(_require_local)])
async def brain_learn(body: LearnBody) -> dict:
    return _call(service.start_learning, body.source_id, force=body.force)


@router.get("/api/brain/runs")
def brain_runs(limit: int = Query(20, ge=1, le=200)) -> list:
    return _call(service.runs, limit)


@router.get("/api/brain/pieces")
def brain_pieces(q: str = Query("", max_length=200), source_id: Optional[str] = None, novel: bool = False,
                 shared: bool = False, limit: int = Query(50, ge=1, le=500),
                 offset: int = Query(0, ge=0)) -> dict:
    return _call(service.list_pieces, q=q, source_id=source_id, novel=novel, shared=shared, limit=limit,
                 offset=offset)


@router.get("/api/brain/pieces/{piece_id}")
def brain_piece(piece_id: int) -> dict:
    return _call(service.get_piece, piece_id)


@router.get("/api/brain/pieces/{piece_id}/similar")
def brain_similar(piece_id: int, limit: int = Query(10, ge=1, le=50)) -> list:
    return _call(service.similar, piece_id, limit)


@router.get("/api/brain/relations")
def brain_relations() -> list:
    return _call(service.relations)


@router.post("/api/brain/recall")
async def brain_recall(body: RecallBody) -> dict:
    return await _acall(service.recall_cue(body.cue, context_source=body.context_source, context=body.context,
                                           limit=body.limit, hops=body.hops, explore=body.explore,
                                           reinforce=body.reinforce, answer=body.answer))


@router.post("/api/brain/use")
def brain_use(body: UseBody) -> dict:
    return _call(service.use, body.piece_ids)


@router.get("/api/brain/graph")
def brain_graph(source_id: Optional[str] = None, limit: int = Query(120, ge=1, le=500)) -> dict:
    return _call(service.graph_view, source_id=source_id, limit=limit)


@router.get("/api/brain/patterns")
def brain_patterns() -> list:
    return _call(service.list_patterns)


@router.get("/api/brain/analogies")
def brain_analogies() -> list:
    return _call(service.list_analogies)


@router.get("/api/brain/hypotheses")
def brain_hypotheses(limit: int = Query(100, ge=1, le=500)) -> list:
    return _call(service.list_hypotheses, limit)


@router.get("/api/brain/findings")
def brain_findings(status: str = Query("open"), limit: int = Query(100, ge=1, le=500)) -> list:
    return _call(service.list_findings, status, limit)


@router.patch("/api/brain/findings/{finding_id}")
def brain_update_finding(finding_id: int, body: FindingBody) -> dict:
    return _call(service.update_finding, finding_id, body.status)


@router.post("/api/brain/discover", dependencies=[Depends(_require_local)])
async def brain_discover() -> dict:
    return await _acall(service.discover_now())


@router.post("/api/brain/goals", status_code=201)
async def brain_propose(body: GoalBody) -> dict:
    return await _acall(service.propose(body.goal, context_source=body.context_source))


@router.get("/api/brain/goals")
def brain_goals(limit: int = Query(30, ge=1, le=200)) -> list:
    return _call(service.list_goals, limit)


@router.get("/api/brain/goals/{goal_id}")
def brain_goal(goal_id: int) -> dict:
    return _call(service.get_goal, goal_id)


@router.post("/api/brain/designs/{design_id}/approve", dependencies=[Depends(_require_local)])
def brain_approve(design_id: int) -> dict:
    return _call(service.approve, design_id)


@router.post("/api/brain/designs/{design_id}/build", status_code=202, dependencies=[Depends(_require_local)])
async def brain_build(design_id: int) -> dict:
    return _call(service.start_build, design_id)


@router.get("/api/brain/experiences")
def brain_experiences(limit: int = Query(50, ge=1, le=500)) -> list:
    return _call(service.list_experiences, limit)


@router.post("/api/brain/experiences", status_code=201)
def brain_add_experience(body: ExperienceBody) -> dict:
    return _call(service.add_experience, goal=body.goal, result=body.result, lessons=body.lessons,
                 design_id=body.design_id, source_id=body.source_id, piece_ids=body.piece_ids,
                 pattern_ids=body.pattern_ids)
