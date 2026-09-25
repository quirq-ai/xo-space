"""Read-only project profiles; fly identity belongs to model selection."""

from fastapi.responses import JSONResponse

from services.cowork_agent.project_layout import list_projects


def list_agents() -> list[dict]:
    return [{
        "name": project["name"],
        "description": project.get("description") or project["display_name"],
        "mode": "primary", "tools": ["workspace.list", "workspace.read-json-csv", "report.produce"],
        "permissions": {"rules": []}, "system_prompt": None, "temperature": None,
        "metadata": {"backend": "fly", "display_name": project["display_name"],
                     "workspace": project["path"], "read_only": True},
    } for project in list_projects()]


def create_agent(body):
    return JSONResponse(status_code=405, content={"detail": "Select an existing project, then choose a deployed fly model. Train new flies in the lab and copy their exported JSON into the Quirq flies directory."})
