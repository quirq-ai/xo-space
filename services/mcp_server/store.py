"""Persistent opt-in and revocable credential hash; never stores the bearer token."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from services.errors import ServiceError
from services.storage.atomic_write import write_json_atomic
from services.storage.flock import locked
from services.storage.layout import settings_dir


def config_path() -> Path:
    return settings_dir() / "mcp-server.json"


def read() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": 1, "enabled": False, "token_hash": None}
    except (OSError, ValueError) as exc:
        raise ServiceError("mcp_settings_unavailable", "MCP server settings could not be read.", 503) from exc
    if (
        not isinstance(data, dict)
        or type(data.get("schema")) is not int or data["schema"] != 1
        or type(data.get("enabled")) is not bool
        or (data.get("token_hash") is not None and (
            not isinstance(data["token_hash"], str)
            or re.fullmatch(r"[a-f0-9]{64}", data["token_hash"]) is None
        ))
        or (data["enabled"] and not data.get("token_hash"))
    ):
        raise ServiceError("mcp_settings_invalid", "MCP server settings are invalid; repair the saved settings before enabling access.", 503)
    return data


def update(*, enabled: bool | None = None, rotate: bool = False) -> tuple[dict, str | None]:
    path = config_path()
    try:
        with locked(path):
            data = read()
            if rotate and not data["enabled"]:
                raise ServiceError("mcp_server_disabled", "Enable the MCP server before regenerating its token.", 409)
            token = None
            desired = data["enabled"] if enabled is None else enabled
            if desired and (rotate or not data["enabled"]):
                token = secrets.token_urlsafe(32)
                data["token_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not desired:
                data["token_hash"] = None
            data["enabled"] = desired
            write_json_atomic(path, data)
            path.chmod(0o600)
            return data, token
    except OSError as exc:
        raise ServiceError("mcp_settings_write_failed", "MCP server settings could not be saved. Check the Space data folder permissions.", 503) from exc


def authenticate(authorization: str) -> None:
    """Re-read on every request so disabling/rotation applies to all workers."""
    data = read()
    if not data["enabled"]:
        raise ServiceError("mcp_server_disabled", "The Space MCP server is disabled.", 404)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        raise ServiceError("mcp_unauthorized", "A valid Space MCP bearer token is required.", 401)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(digest, data["token_hash"]):
        raise ServiceError("mcp_unauthorized", "A valid Space MCP bearer token is required.", 401)
