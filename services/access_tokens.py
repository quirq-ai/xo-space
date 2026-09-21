"""Independent opt-ins and revocable bearer hashes for Space tool interfaces."""

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


class TokenAccessSettings:
    def __init__(self, filename: str, prefix: str, label: str) -> None:
        self.filename, self.prefix, self.label = filename, prefix, label

    def path(self) -> Path:
        return settings_dir() / self.filename

    def error(self, suffix: str, message: str, status: int) -> ServiceError:
        return ServiceError(f"{self.prefix}_{suffix}", message, status)

    def read(self) -> dict:
        try:
            data = json.loads(self.path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": 1, "enabled": False, "token_hash": None}
        except (OSError, ValueError) as exc:
            raise self.error("settings_unavailable", f"{self.label} settings could not be read.", 503) from exc
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
            raise self.error("settings_invalid", f"{self.label} settings are invalid; repair the saved settings before enabling access.", 503)
        return data

    def update(self, *, enabled: bool | None = None, rotate: bool = False) -> tuple[dict, str | None]:
        if enabled is not None and type(enabled) is not bool:
            raise self.error("invalid_setting", "enabled must be a boolean.", 400)
        path = self.path()
        try:
            with locked(path):
                data = self.read()
                if rotate and not data["enabled"]:
                    raise self.error("server_disabled", f"Enable {self.label} before regenerating its token.", 409)
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
            raise self.error("settings_write_failed", f"{self.label} settings could not be saved. Check the Space data folder permissions.", 503) from exc

    def authenticate(self, authorization: str) -> None:
        # Reads on every request keep disable and rotation effective across workers.
        data = self.read()
        if not data["enabled"]:
            raise self.error("server_disabled", f"Space {self.label} is disabled.", 404)
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256:
            raise self.error("unauthorized", f"A valid {self.label} bearer token is required.", 401)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(digest, data["token_hash"]):
            raise self.error("unauthorized", f"A valid {self.label} bearer token is required.", 401)
