"""Telemetry source configuration behind the Agents tab's Configure page.

Space aggregates session telemetry from every adapter that ships a
``session_telemetry`` capability. A provider may describe how it is
configured through an optional ``SOURCE_CONFIG`` dict on that module: the
environment variable that points at its data, the default location, what it
collects. This service reads those descriptors generically (core never
names an agent), reports the effective path, and writes changes to the env
store the providers already read through ``os.getenv`` at collection time,
so a saved path takes effect on the next telemetry rebuild, no restart.

Collection can be switched off per source with ``QUIRQ_TELEMETRY_DISABLED``,
a comma-separated list of source ids that the telemetry builder honors.

A person configures collection and the agent only reads it, so this is
Space-level: it lives in the telemetry module (``modules/telemetry``), which
serves it at ``/api/telemetry/sources``. Was ``services/telemetry_sources.py``;
that path is an alias of this module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from services.cowork_agent import scopes
from services.cowork_agent.adapters.loader import (
    list_capability_providers,
    try_load_capability,
)
from services.errors import ServiceError, Unavailable

CAPABILITY = "session_telemetry"
DISABLED_ENV = "QUIRQ_TELEMETRY_DISABLED"
_MAX_PATH_LENGTH = 1024
_FORBIDDEN_PATH_CHARS = ("\n", "\r", "\x00")


class UnknownTelemetrySource(ServiceError):
    """No provider with that id ships session telemetry."""

    def __init__(self, source_id: str) -> None:
        super().__init__(
            "unknown_source", f"Unknown telemetry source {source_id!r}.", 404
        )


class InvalidTelemetryPath(ServiceError):
    """The path is relative, too long, or carries characters .env cannot hold."""

    def __init__(self, message: str) -> None:
        super().__init__("invalid_path", message, 400)


def disabled_source_ids(environ: dict[str, str] | None = None) -> set[str]:
    raw = (environ if environ is not None else os.environ).get(DISABLED_ENV, "") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _load_providers() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for provider in list_capability_providers(CAPABILITY):
        module = try_load_capability(CAPABILITY, agent=provider)
        if module is not None:
            out.append((provider, module))
    return out


def _path_status(raw: str, kind: str) -> dict[str, Any]:
    path = Path(raw).expanduser() if raw else None
    exists = bool(path and path.exists())
    return {
        "effective": raw,
        "resolved": str(path) if path else "",
        "exists": exists,
        "readable": exists and os.access(path, os.R_OK),
        "is_expected_kind": (
            exists and (path.is_dir() if kind == "dir" else path.is_file())
        ),
    }


def describe_source(provider: str, module: Any) -> dict[str, Any]:
    config = getattr(module, "SOURCE_CONFIG", None) or {}
    source_id = str(getattr(module, "SOURCE_ID", provider))
    label = str(getattr(module, "SOURCE_LABEL", provider.replace("_", " ").title()))
    env_key = str(config.get("path_env") or "").strip() or None
    default = str(config.get("path_default") or "").strip()
    kind = "dir" if str(config.get("path_kind") or "dir") == "dir" else "file"
    configured = (os.getenv(env_key, "") if env_key else "") or ""
    effective = configured.strip() or default
    return {
        "id": source_id,
        "provider": provider,
        "label": label,
        "vendor": str(config.get("vendor") or "unknown"),
        "cost_status": str(getattr(module, "COST_STATUS", "unknown")),
        "enabled": source_id not in disabled_source_ids(),
        "collects": [str(row) for row in config.get("collects") or []],
        "never": str(config.get("never") or ""),
        "path": {
            "env": env_key,
            "label": str(config.get("path_label") or "Data location"),
            "kind": kind,
            "default": default,
            "configured": configured.strip(),
            "editable": env_key is not None,
            **_path_status(effective, kind),
        },
    }


def list_sources() -> list[dict[str, Any]]:
    return [describe_source(provider, module) for provider, module in _load_providers()]


def _find(source_id: str) -> tuple[str, Any]:
    for provider, module in _load_providers():
        if str(getattr(module, "SOURCE_ID", provider)) == source_id:
            return provider, module
    raise UnknownTelemetrySource(source_id)


def _clean_path(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    if len(path) > _MAX_PATH_LENGTH:
        raise InvalidTelemetryPath("Path is too long.")
    if any(ch in path for ch in _FORBIDDEN_PATH_CHARS):
        raise InvalidTelemetryPath("Path contains characters that cannot be saved.")
    if not (path.startswith("/") or path.startswith("~")):
        raise InvalidTelemetryPath("Use an absolute path, or one that starts with ~.")
    return path


def save_source(
    source_id: str,
    *,
    path: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Persist a source's path and/or its collection switch, then describe it.

    An empty path clears the override so the provider's default applies again.
    """
    provider, module = _find(source_id)
    handle = scopes.resolve_scope("secrets")
    try:
        if path is not None:
            env_key = ((getattr(module, "SOURCE_CONFIG", None) or {}).get("path_env") or "").strip()
            if not env_key:
                raise InvalidTelemetryPath(f"{source_id} has no configurable data location.")
            cleaned = _clean_path(path)
            if cleaned:
                handle.upsert(env_key, cleaned)
                os.environ[env_key] = cleaned
            else:
                handle.delete(env_key)
                os.environ.pop(env_key, None)
        if enabled is not None:
            disabled = disabled_source_ids()
            if enabled:
                disabled.discard(source_id)
            else:
                disabled.add(source_id)
            if disabled:
                joined = ",".join(sorted(disabled))
                handle.upsert(DISABLED_ENV, joined)
                os.environ[DISABLED_ENV] = joined
            else:
                handle.delete(DISABLED_ENV)
                os.environ.pop(DISABLED_ENV, None)
    except OSError as exc:
        raise Unavailable("store_unavailable", f"Could not write the env store: {exc}") from exc
    return describe_source(provider, module)
