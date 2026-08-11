"""Validated machine-local runtime configuration and diagnostics.

Secrets and runtime controls are deliberately separate:

* ``secrets.env`` contains write-only credentials.
* ``runtime.env`` contains a small allowlisted set of non-secret controls.

Both live below the machine-local Quirq state root, never inside a project's
portable ``.xo`` directory. Runtime controls are read at process startup, so
changing them produces a truthful ``restart_required`` state instead of
pretending import-time configuration changed live.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from services.cowork_agent.local_state import quirq_state_dir
from services.cowork_agent.registry.agent_env import load_env_entries
from services.cowork_agent.registry.agent_registry import all_agents, get_active_agent


RUNTIME_CONFIG_KEYS = frozenset(
    {
        "AGENT_NAME",
        "QUIRQ_WATCHER_ENABLED",
        "QUIRQ_WATCHER_INTERVAL_SECONDS",
        "QUIRQ_WATCHER_SOURCE_MODE",
    }
)
ROOT_CONFIG_KEYS = frozenset({"XO_PROJECTS_ROOT", "QUIRQ_STATE_ROOT"})
# The canonical one-liner from the website, not a raw GitHub URL: the site
# bootstrapper follows repo renames and branch selection, so this string
# cannot rot the way a hardcoded raw.githubusercontent.com path did.
INSTALL_COMMAND = "curl -fsSL https://www.quirq.ai/install | sh"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SESSION_SCAN_CAP = 10_000


def _secrets_fingerprint() -> str:
    """Hash the write-only secret store without retaining or returning values."""
    digest = hashlib.sha256()
    rows = sorted(
        (
            str(entry.get("key") or "").strip(),
            str(entry.get("value") or ""),
        )
        for entry in load_env_entries()
        if str(entry.get("key") or "").strip()
    )
    for key, value in rows:
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


_STARTUP_SECRETS_FINGERPRINT = _secrets_fingerprint()


def runtime_config_file() -> Path:
    configured = (os.getenv("QUIRQ_RUNTIME_FILE", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return quirq_state_dir() / "runtime.env"


def root_config_file() -> Path:
    return quirq_state_dir() / "roots.env"


def _parse_env_file(
    path: Path,
    allowed_keys: frozenset[str] = RUNTIME_CONFIG_KEYS,
) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in allowed_keys:
            values[key] = value.strip()
    return values


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUE_VALUES


def _as_interval(value: str | None, *, default: float = 1.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(60.0, max(0.25, parsed))


def _listen_port() -> int:
    try:
        return int(os.getenv("PORT", "5002") or "5002")
    except ValueError:
        return 5002


def effective_settings() -> dict[str, Any]:
    return {
        "agent_name": get_active_agent().name,
        "watcher_enabled": _as_bool(
            os.getenv("QUIRQ_WATCHER_ENABLED"),
            default=True,
        ),
        "watcher_interval_seconds": _as_interval(
            os.getenv("QUIRQ_WATCHER_INTERVAL_SECONDS")
        ),
        "watcher_source_mode": (
            "all"
            if (os.getenv("QUIRQ_WATCHER_SOURCE_MODE", "active") or "")
            .strip()
            .lower()
            == "all"
            else "active"
        ),
    }


def saved_settings() -> dict[str, Any] | None:
    values = _parse_env_file(runtime_config_file())
    if not values:
        return None
    return {
        "agent_name": values.get("AGENT_NAME", get_active_agent().name),
        "watcher_enabled": _as_bool(
            values.get("QUIRQ_WATCHER_ENABLED"),
            default=True,
        ),
        "watcher_interval_seconds": _as_interval(
            values.get("QUIRQ_WATCHER_INTERVAL_SECONDS")
        ),
        "watcher_source_mode": (
            "all"
            if (
                values.get(
                    "QUIRQ_WATCHER_SOURCE_MODE",
                    os.getenv("QUIRQ_WATCHER_SOURCE_MODE", "active"),
                )
                or ""
            )
            .strip()
            .lower()
            == "all"
            else "active"
        ),
    }


def configured_settings() -> dict[str, Any]:
    return saved_settings() or effective_settings()


def _runtime_settings_restart_required() -> bool:
    saved = saved_settings()
    if saved is None:
        return False
    # The registry has a safe fallback when AGENT_NAME is absent, but the
    # startup bootstrap intentionally requires an explicit value. Saving the
    # same visible backend therefore still needs one restart when the process
    # was running only on the fallback.
    if (os.getenv("AGENT_NAME", "") or "").strip() != saved["agent_name"]:
        return True
    return saved != effective_settings()


def secrets_restart_required() -> bool:
    """Return whether the write-only store changed since process startup."""
    return _secrets_fingerprint() != _STARTUP_SECRETS_FINGERPRINT


def restart_reasons() -> list[str]:
    reasons: list[str] = []
    if _runtime_settings_restart_required():
        reasons.append("runtime")
    if secrets_restart_required():
        reasons.append("secrets")
    return reasons


def restart_required() -> bool:
    return bool(restart_reasons())


def validate_settings(payload: dict[str, Any]) -> dict[str, Any]:
    agent_name = str(payload.get("agent_name") or "").strip()
    available = {manifest.name for manifest in all_agents()}
    if agent_name not in available:
        raise ValueError(
            f"Unknown agent backend '{agent_name}'. Available: "
            + ", ".join(sorted(available))
        )

    watcher_enabled = payload.get("watcher_enabled")
    if not isinstance(watcher_enabled, bool):
        raise ValueError("watcher_enabled must be true or false")

    raw_interval = payload.get("watcher_interval_seconds")
    if isinstance(raw_interval, bool):
        raise ValueError("watcher_interval_seconds must be a number")
    try:
        interval = float(raw_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("watcher_interval_seconds must be a number") from exc
    if not 0.25 <= interval <= 60:
        raise ValueError("watcher_interval_seconds must be between 0.25 and 60")

    source_mode = str(payload.get("watcher_source_mode") or "").strip().lower()
    if source_mode not in {"active", "all"}:
        raise ValueError("watcher_source_mode must be 'active' or 'all'")

    return {
        "agent_name": agent_name,
        "watcher_enabled": watcher_enabled,
        "watcher_interval_seconds": interval,
        "watcher_source_mode": source_mode,
    }


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    clean = validate_settings(payload)
    interval = f"{clean['watcher_interval_seconds']:g}"
    text = (
        "# Managed by Quirq Runtime Setup. Non-secret settings only.\n"
        f"AGENT_NAME={clean['agent_name']}\n"
        f"QUIRQ_WATCHER_ENABLED={'true' if clean['watcher_enabled'] else 'false'}\n"
        f"QUIRQ_WATCHER_INTERVAL_SECONDS={interval}\n"
        f"QUIRQ_WATCHER_SOURCE_MODE={clean['watcher_source_mode']}\n"
    )
    _write_private_text(runtime_config_file(), text)
    return clean


def _validate_host_root(value: Any, *, label: str) -> str:
    path = str(value or "").strip()
    if not path:
        raise ValueError(f"{label} is required")
    if "\n" in path or "\r" in path or "\0" in path:
        raise ValueError(f"{label} contains unsupported characters")
    if not Path(path).is_absolute():
        raise ValueError(f"{label} must be an absolute host path")
    normalized = os.path.normpath(path)
    if normalized == os.path.sep:
        raise ValueError(f"{label} cannot be the filesystem root")
    return normalized


def validate_root_settings(payload: dict[str, Any]) -> dict[str, str]:
    projects_root = _validate_host_root(
        payload.get("xo_projects_root"),
        label="XO root",
    )
    state_root = _validate_host_root(
        payload.get("quirq_state_root"),
        label="Quirq root",
    )
    try:
        common = os.path.commonpath([projects_root, state_root])
    except ValueError as exc:
        raise ValueError("XO root and Quirq root must be on compatible paths") from exc
    if common == state_root:
        raise ValueError(
            "XO root and Quirq root must be separate, non-nested directories"
        )
    if common == projects_root:
        # Mirror install.sh's validate_separate_roots: the one allowed nesting
        # is the state root as a hidden directory directly inside the projects
        # root — the default ./ and ./.quirq layout the installer itself
        # creates. quirq_catalog skips dot-prefixed entries when enumerating
        # projects, so ".quirq" there can never be mistaken for one. Anything
        # deeper, not hidden, or equal stays forbidden.
        relative = os.path.relpath(state_root, projects_root)
        if (
            relative == os.curdir
            or os.sep in relative
            or not relative.startswith(".")
        ):
            raise ValueError(
                "XO root and Quirq root must be separate, non-nested directories"
            )
    return {
        "xo_projects_root": projects_root,
        "quirq_state_root": state_root,
    }


def save_root_settings(payload: dict[str, Any]) -> dict[str, str]:
    clean = validate_root_settings(payload)
    text = (
        "# Managed by Quirq Runtime Setup. Applied by the installer.\n"
        f"XO_PROJECTS_ROOT={clean['xo_projects_root']}\n"
        f"QUIRQ_STATE_ROOT={clean['quirq_state_root']}\n"
    )
    _write_private_text(root_config_file(), text)
    return clean


def root_settings() -> dict[str, Any]:
    # QUIRQ_HOST_* are the Docker installer's container→host translations and
    # win when set. Native runs never set them, so fall back to the roots the
    # process is actually using — otherwise Setup shows empty fields and
    # "not reported" for an install that is running fine.
    current = {
        "xo_projects_root": (
            (os.getenv("QUIRQ_HOST_PROJECTS_ROOT", "") or "").strip()
            or (os.getenv("XO_PROJECTS_ROOT", "") or "").strip()
            or str(Path("~/xo-projects").expanduser())
        ),
        "quirq_state_root": (
            (os.getenv("QUIRQ_HOST_STATE_ROOT", "") or "").strip()
            or str(quirq_state_dir())
        ),
    }
    saved = _parse_env_file(root_config_file(), ROOT_CONFIG_KEYS)
    configured = {
        "xo_projects_root": saved.get(
            "XO_PROJECTS_ROOT",
            current["xo_projects_root"],
        ),
        "quirq_state_root": saved.get(
            "QUIRQ_STATE_ROOT",
            current["quirq_state_root"],
        ),
    }
    return {
        "applied": current,
        "configured": configured,
        "change_required": configured != current,
        "apply_command": INSTALL_COMMAND,
        "config_file": str(root_config_file()),
    }


def _path_status(container_path: Path, host_path: str = "") -> dict[str, Any]:
    exists = container_path.exists()
    return {
        "container_path": str(container_path),
        "host_path": host_path,
        "exists": exists,
        "readable": exists and os.access(container_path, os.R_OK),
        "writable": exists and os.access(container_path, os.W_OK),
    }


def _count_session_files(home_dir: Path, patterns: list[str]) -> tuple[int, str | None]:
    count = 0
    latest_path: Path | None = None
    latest_mtime = -1.0
    for pattern in patterns:
        try:
            matches = home_dir.glob(pattern)
        except (OSError, ValueError):
            continue
        for path in matches:
            if not path.is_file():
                continue
            count += 1
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = -1.0
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = path
            if count >= _SESSION_SCAN_CAP:
                return count, str(latest_path) if latest_path else None
    return count, str(latest_path) if latest_path else None


def _declared_secrets(manifest: Any) -> list[dict[str, str]]:
    setup = manifest.raw.get("runtime_setup") or {}
    rows = setup.get("secrets") or []
    out: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, str):
            out.append({"key": row, "label": row, "description": ""})
            continue
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        out.append(
            {
                "key": key,
                "label": str(row.get("label") or key),
                "description": str(row.get("description") or ""),
            }
        )
    return out


def runtime_sources() -> list[dict[str, Any]]:
    applied_agent = get_active_agent().name
    source_mode = effective_settings()["watcher_source_mode"]
    host_home = (os.getenv("QUIRQ_HOST_HOME", "") or "").rstrip("/")
    configured_keys = {
        str(entry.get("key") or "")
        for entry in load_env_entries()
        if str(entry.get("value") or "").strip()
    }
    sources: list[dict[str, Any]] = []
    for manifest in sorted(all_agents(), key=lambda item: item.name):
        setup = manifest.raw.get("runtime_setup") or {}
        patterns = [
            str(pattern)
            for pattern in setup.get("session_globs") or []
            if isinstance(pattern, str) and pattern
        ]
        session_files, latest_session_file = _count_session_files(
            manifest.home_dir,
            patterns,
        )
        try:
            relative_home = manifest.home_dir.relative_to(Path.home())
            host_dir = str(Path(host_home) / relative_home) if host_home else ""
        except ValueError:
            host_dir = ""
        secret_rows = _declared_secrets(manifest)
        sources.append(
            {
                "name": manifest.name,
                "active": manifest.name == applied_agent,
                "watched": source_mode == "all" or manifest.name == applied_agent,
                "binary": manifest.binary,
                "binary_available": shutil.which(manifest.binary) is not None,
                "bootstrap_available": bool(setup.get("installs_cli")),
                "home": _path_status(manifest.home_dir, host_dir),
                "session_files": session_files,
                "latest_session_file": latest_session_file,
                "secrets": [
                    {**row, "configured": row["key"] in configured_keys}
                    for row in secret_rows
                ],
            }
        )
    return sources


def runtime_status() -> dict[str, Any]:
    projects_root = Path(
        (os.getenv("XO_PROJECTS_ROOT", "") or "").strip() or "~/xo-projects"
    ).expanduser()
    state_root = quirq_state_dir()
    configured = configured_settings()
    applied = effective_settings()
    reasons = restart_reasons()
    return {
        "configured": configured,
        "applied": applied,
        "restart_required": bool(reasons),
        "restart_reasons": reasons,
        "restart_supported": _as_bool(
            os.getenv("QUIRQ_ALLOW_SELF_RESTART"),
            default=False,
        ),
        "managed_container": _as_bool(
            os.getenv("QUIRQ_MANAGED_CONTAINER"),
            default=False,
        ),
        "network": {
            "listen_port": _listen_port(),
            "public_url": (
                os.getenv("QUIRQ_PUBLIC_URL", "") or ""
            ).strip(),
        },
        "roots": root_settings(),
        "agents": runtime_sources(),
        "paths": {
            "projects": _path_status(
                projects_root,
                (os.getenv("QUIRQ_HOST_PROJECTS_ROOT", "") or "").strip(),
            ),
            "ai_workspace": _path_status(
                Path(
                    (os.getenv("AI_WORKSPACE_ROOT", "") or "").strip()
                    or projects_root
                ).expanduser()
            ),
            "state": _path_status(
                state_root,
                (os.getenv("QUIRQ_HOST_STATE_ROOT", "") or "").strip(),
            ),
            "runtime_file": _path_status(runtime_config_file()),
            "secrets_file": _path_status(
                Path(
                    (os.getenv("QUIRQ_SECRETS_FILE", "") or "").strip()
                    or state_root / "secrets.env"
                )
            ),
        },
    }
