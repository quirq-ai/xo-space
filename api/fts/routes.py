"""
Full-text-search index endpoints (currently stubbed).

The frontend calls these during workspace indexing; the bridge just reports
`idle` for now. Both GET and POST exist because the UI pings GET to check
status and POST to trigger a rebuild.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/index/{workspace:path}")
def fts_index_get(workspace: str, session_id: str = ""):
    return {"status": "idle", "progress": 0}


@router.post("/index/{workspace:path}")
def fts_index_post(workspace: str, session_id: str = ""):
    return {"status": "idle", "progress": 0}
