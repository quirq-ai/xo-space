"""The person's agents (plan B5, B7):

  GET  /api/personas?project=              each agent the project sees (the Space's without one), with
                                           its level, which agent replaces which, the jobs that use it and
                                           its file's mistakes; and once, the form's three "May" choices
  GET  /api/personas/importable?project=   the agents each runtime already has, mapped to XO's file
  POST /api/personas/import                {adapter, id, project?, without_dropped?}: one of them, copied in

Reading, saving, versions, delete and rename of an agent's file are the
project-files routes' (``/api/xo-projects/{id}/file`` and the rest), with
``personas.SPACE_ID`` as the Space's project id; nothing here writes
another way. Import is an optional adapter capability, ``persona_import``,
asked of every installed adapter through the loader; this module names no
runtime. Each file is checked on its own: one that maps fully imports as it
is; one with settings XO can't carry lists them (``dropped``), and imports
only with ``without_dropped: true``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.bff.read_host import require_read_host
from routers.cowork_agent.bff.ui_writer import require_ui_writer
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.adapters.loader import list_capability_providers, load_capability
from services.cowork_agent.storm import jobs, personas
from services.cowork_agent.storm import chats
from services.cowork_agent.storm.jobs.tries import TryRefused, problems_view
from services.errors import ServiceError
from services.project_files import ops
from services.project_files.address import AGENTS_REL, FileRefused, project_root
from services.project_files.save import save
from services.project_files.watch import touch

router = APIRouter()

CAPABILITY = "persona_import"


class ImportRefused(TryRefused):
    """An import XO won't do; ``what`` lists the mistakes or the settings left out."""


def _view(p: Any, used: list[dict[str, Any]], chat_id: Optional[str]) -> dict[str, Any]:
    """An agent as the list shows it; the jobs that use it are its project's, or any for a Space agent."""
    mine = [{"id": j["id"], "name": j["name"]} for j in used if j["persona"] == {"name": p.name, "level": p.level}
            and (p.level == personas.SPACE or j["chat"] == chat_id)]
    return {"name": p.name, "description": p.description, "level": p.level, "path": p.path, "version": p.version,
            "runtime": p.runtime, "model": p.model, "effort": p.effort, "tools": p.tools,
            "disallowedTools": p.disallowed_tools, "mcpServers": p.mcp_servers, "shadows": p.shadows,
            "shadowed": p.shadowed, "used_by": {"jobs": mine}, "problems": problems_view(p.problems)}


@router.get("/api/personas")
async def list_personas(project: Optional[str] = Query(None, max_length=200)) -> dict:
    try:
        found = await asyncio.to_thread(personas.listing, project)
        used = [j for j in await jobs.views() if j.get("persona")]
        chat_id = (await chats.current().project(project))["id"] if project else None
    except ServiceError as exc:
        raise storm_error(exc)
    return {"personas": [_view(p, used, chat_id) for p in found], "may": personas.MAY}


# ── import (B7) ──────────────────────────────────────────────────────────


def _candidates(project: Optional[str]) -> list[dict[str, Any]]:
    """What every installed adapter offers, each checked as a saved file is."""
    folder = project_root(project)[1] if project else None     # 404 for a project that isn't there
    out = []
    for adapter in list_capability_providers(CAPABILITY):
        module = load_capability(CAPABILITY, agent=adapter)
        for item in module.candidates(folder):
            _persona, problems = personas.parse(item["text"])
            out.append({**item, "adapter": adapter, "from": module.DISPLAY_NAME, "problems": problems_view(problems)})
    return out


@router.get("/api/personas/importable")
async def importable(project: Optional[str] = Query(None, max_length=200)) -> dict:
    try:
        return {"importable": await asyncio.to_thread(_candidates, project)}
    except ServiceError as exc:
        raise storm_error(exc)


class ImportBody(ForbidExtra):
    adapter: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=300)
    project: Optional[str] = Field(None, max_length=200)
    without_dropped: bool = False


def _import(body: ImportBody) -> dict[str, Any]:
    item = next((i for i in _candidates(body.project) if (i["adapter"], i["id"]) == (body.adapter, body.id)), None)
    if item is None:
        raise ImportRefused("not_importable", f"{body.adapter} offers no agent {body.id!r} to import; list them "
                                              "with GET /api/personas/importable.", status=404)
    if item["problems"]:
        raise ImportRefused("invalid_persona", "This agent doesn't map to XO's file without mistakes, so it can't "
                                               "be imported.", what=item["problems"])
    if item["dropped"] and not body.without_dropped:
        raise ImportRefused("settings_dropped", "XO can't carry some of this agent's settings: import it without "
                                                "them (without_dropped: true), or skip it.", status=409,
                            what=item["dropped"])
    owner = personas.SPACE_ID if item["level"] == personas.SPACE else body.project
    if owner != personas.SPACE_ID:
        try:
            ops.mkdir(owner, AGENTS_REL)             # a project's first agent makes its folder
        except FileRefused as exc:
            if exc.code != "exists":
                raise
    touch(owner)
    saved = save(owner, personas.rel_path(item["level"], item["name"]), item["text"], None)
    return {"name": item["name"], "level": item["level"], "relative_path": saved.relative_path,
            "version": saved.version, "dropped": item["dropped"]}


@router.post("/api/personas/import", dependencies=[Depends(require_read_host), Depends(require_ui_writer)])
async def import_persona(body: ImportBody) -> dict:
    try:
        return await asyncio.to_thread(_import, body)
    except ServiceError as exc:
        raise storm_error(exc)
