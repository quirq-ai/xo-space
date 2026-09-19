"""
Skill install catalog endpoints.

``GET /api/skills/catalog`` lists installable skills; ``POST
/api/skills/install`` runs a catalogued skill's server-defined shell commands
(blocking) and returns a pass/fail summary with step counts. Clients send only
a skill name — commands live in ``config/skills/catalog.json``, and neither
command text nor command output is echoed back (output can leak embedded
paths/tokens); failed-step output goes to the server log instead.

The ``GET /api/skills`` stub in ``misc.py`` is a separate frontend contract
and stays untouched.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.cowork_agent import skill_catalog

router = APIRouter()


class InstallRequest(BaseModel):
    name: str


@router.get("/catalog")
def list_catalog():
    return [
        {
            "name": entry["name"],
            "description": entry["description"],
            "steps_total": len(entry["commands"]),
            "timeout_seconds": entry["timeout_seconds"],
        }
        for entry in skill_catalog.load_catalog().values()
    ]


@router.post("/install")
async def install_skill(req: InstallRequest):
    try:
        result = await skill_catalog.install(req.name)
    except skill_catalog.UnknownSkillError:
        raise HTTPException(
            status_code=404,
            detail=f"unknown skill {req.name!r}: add it to config/skills/catalog.json first",
        )
    except skill_catalog.InstallInProgressError:
        raise HTTPException(
            status_code=409,
            detail=f"an install for {req.name!r} is already running",
        )
    # Per-step stdout/stderr stays server-side (see module docstring).
    return {key: result[key] for key in ("name", "ok", "summary", "steps_total", "steps_run")}


@router.get("")
def list_skills():
    # Empty-list stub the frontend polls.
    return []
