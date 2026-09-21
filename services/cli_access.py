"""Settings and shared read operations for the Space command-line client."""

from __future__ import annotations

from pathlib import Path

from services import space_tools
from services.access_tokens import TokenAccessSettings

settings = TokenAccessSettings("cli-access.json", "cli", "CLI access")
COMMANDS = ["projects", "document", "todos", "inbox", "status"]
DOWNLOAD_PATH = "/api/cli-access/client"


def status(data: dict | None = None) -> dict:
    data = settings.read() if data is None else data
    return {"enabled": data["enabled"], "token_configured": bool(data.get("token_hash")),
            "commands": COMMANDS, "download_path": DOWNLOAD_PATH}


def configure(*, enabled: bool | None = None, rotate: bool = False) -> dict:
    data, token = settings.update(enabled=enabled, rotate=rotate)
    result = status(data)
    if token is not None:
        result["token"] = token
    return result


def authenticate(authorization: str) -> None:
    settings.authenticate(authorization)


def client_path() -> Path:
    return Path(__file__).resolve().parents[1] / "space"


async def list_projects(limit: int, offset: int) -> dict:
    return await space_tools.read(space_tools.list_projects, limit, offset)


async def read_document(project_id: str, document: space_tools.ProjectDocument) -> dict:
    return await space_tools.read(space_tools.read_project_document, project_id, document)


async def list_todos(project_id: str, limit: int) -> dict:
    return await space_tools.read(space_tools.list_todos, project_id, limit)


async def list_inbox(status: space_tools.InboxStatus, limit: int) -> dict:
    return await space_tools.read(space_tools.list_inbox, status, limit)
