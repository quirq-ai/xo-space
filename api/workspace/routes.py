"""``GET /api/workspace/workitems``: every workitem across the workspace.

The handler lives with the workspace rollups in
``api/xo_projects/workspace_visualizer.py``; this folder only gives it its URL.
"""

from fastapi import APIRouter

from api.xo_projects.workspace_visualizer import WorkspaceWorkitemsResponse, workspace_workitems

router = APIRouter()
router.get("/workitems", response_model=WorkspaceWorkitemsResponse)(workspace_workitems)
