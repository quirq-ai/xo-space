from pathlib import Path

from .history import summaries

SOURCE_ID = "grok_build"
SOURCE_LABEL = "Grok Build"
COST_STATUS = "unavailable"
META_PRIORITY = 10
SOURCE_CONFIG = {
    "vendor": "xai", "path_env": "GROK_HOME", "path_default": "~/.grok", "path_kind": "dir",
    "path_label": "Grok Build home", "collects": ["Sessions and models"], "never": "No prompt text stored",
}


def collect_session_telemetry():
    sessions = []
    for summary, folder in summaries():
        native = folder.name
        cwd = summary["info"]["cwd"]
        sessions.append({
            "id": native, "key": f"{SOURCE_ID}:{native}", "agent": SOURCE_ID, "project": Path(cwd).name,
            "project_path": cwd, "started_at": summary.get("created_at"), "ended_at": summary.get("updated_at"),
            "model": summary.get("current_model_id"), "cost_known": False, "breakdown_known": False,
        })
    if not sessions:
        raise ValueError("No local Grok Build sessions")
    sessions.sort(key=lambda row: row["ended_at"] or "", reverse=True)
    sessions = sessions[:500]
    projects = sorted({row["project_path"] for row in sessions})
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL, "cost_status": COST_STATUS},
        "meta_priority": META_PRIORITY, "project_keys": projects, "sessions": sessions,
        "totals": {"sessions": len(sessions), "projects": len(projects), "sessions_by_agent": {SOURCE_ID: len(sessions)}},
    }
