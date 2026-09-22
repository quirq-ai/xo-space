"""
Grok Bot on-disk layout + gateway discovery.

Env-first, matching the community SDK (do not vendor it):

* ``SAND_DATA_ROOT`` — absolute sand-data root
* ``SAND_USER_DATA_DIR`` / ``sand-data`` — desktop-style override
* else the first existing candidate: ``/home/box/sand-data``,
  ``/home/box/agent-data``, ``~/sand-data``
* else ``/home/box/sand-data``

Gateway origin: ``GROKBOT_GATEWAY_URL`` (alias ``SAND_GATEWAY_URL``) wins,
then ``SAND_HOST_PORT`` / ``SAND_GATEWAY_BIND_HOST`` / ``gateway.json``.
Wildcard binds (``0.0.0.0`` / ``::``) rewrite to ``127.0.0.1``.

Token: ``SAND_GATEWAY_TOKEN``, else ``gateway.json``. Never log the token.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_SAND_ROOT = Path("/home/box/sand-data")
AGENT_DATA_ALIAS = Path("/home/box/agent-data")
SAND_DATA_DIRNAME = "sand-data"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:1340"
DEFAULT_GATEWAY_PORT = 1340

ENV_SAND_DATA_ROOT = "SAND_DATA_ROOT"
ENV_SAND_USER_DATA_DIR = "SAND_USER_DATA_DIR"
ENV_SAND_GATEWAY_TOKEN = "SAND_GATEWAY_TOKEN"
ENV_SAND_HOST_PORT = "SAND_HOST_PORT"
ENV_SAND_GATEWAY_BIND_HOST = "SAND_GATEWAY_BIND_HOST"
ENV_GROKBOT_GATEWAY_URL = "GROKBOT_GATEWAY_URL"
ENV_SAND_GATEWAY_URL = "SAND_GATEWAY_URL"
ENV_GROKBOT_DEFAULT_AGENT_ID = "GROKBOT_DEFAULT_AGENT_ID"

_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})
_SUBAGENT_PREFIX = "sand-subagent-"


class SandInvalidAgentIdError(ValueError):
    """Host ``assertValidSandAgentId`` — folder name is not a safe agent id."""


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        trimmed = value.strip()
        if trimmed:
            return trimmed
    return None


def _expand(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).expanduser()


def sand_root_candidates() -> list[Path]:
    """Ordered candidate roots, env overrides first."""
    out: list[Path] = []
    data_root = (os.getenv(ENV_SAND_DATA_ROOT) or "").strip()
    if data_root:
        out.append(_expand(data_root))
    user_data = (os.getenv(ENV_SAND_USER_DATA_DIR) or "").strip()
    if user_data:
        out.append(_expand(user_data) / SAND_DATA_DIRNAME)
    out.extend((DEFAULT_SAND_ROOT, AGENT_DATA_ALIAS, Path.home() / SAND_DATA_DIRNAME))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in out:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def resolve_sand_root() -> Path:
    """Pick the sand-data root. Env wins; otherwise the first existing candidate."""
    data_root = (os.getenv(ENV_SAND_DATA_ROOT) or "").strip()
    if data_root:
        return _expand(data_root)
    user_data = (os.getenv(ENV_SAND_USER_DATA_DIR) or "").strip()
    if user_data:
        return _expand(user_data) / SAND_DATA_DIRNAME
    for candidate in (DEFAULT_SAND_ROOT, AGENT_DATA_ALIAS, Path.home() / SAND_DATA_DIRNAME):
        if candidate.exists():
            return candidate
    return DEFAULT_SAND_ROOT


def agents_dir(root: Path | None = None) -> Path:
    return (root or resolve_sand_root()) / "agents"


def transcripts_dir(root: Path | None = None) -> Path:
    return (root or resolve_sand_root()) / "agent-transcripts"


def gateway_json_paths(root: Path | None = None) -> list[Path]:
    """``gateway.json`` under the resolved root, plus the sand-data/agent-data alias."""
    primary = root or resolve_sand_root()
    paths = [primary / "gateway.json"]
    alias = AGENT_DATA_ALIAS if primary == DEFAULT_SAND_ROOT else DEFAULT_SAND_ROOT
    alias_path = alias / "gateway.json"
    if alias_path not in paths:
        paths.append(alias_path)
    return paths


def is_safe_folder_id(agent_id: str) -> bool:
    return (
        bool(agent_id)
        and "/" not in agent_id
        and "\\" not in agent_id
        and "\0" not in agent_id
        and agent_id not in {".", ".."}
    )


def is_valid_sand_agent_id(agent_id: str) -> bool:
    return is_safe_folder_id(agent_id) and agent_id == agent_id.strip()


def assert_valid_sand_agent_id(agent_id: str) -> None:
    if not is_valid_sand_agent_id(agent_id):
        raise SandInvalidAgentIdError(f"Invalid Sand agent id: {agent_id}")


def is_subagent_id(agent_id: str) -> bool:
    return agent_id.startswith(_SUBAGENT_PREFIX)


def transcript_path(agent_id: str, root: Path | None = None) -> Path:
    assert_valid_sand_agent_id(agent_id)
    return transcripts_dir(root) / agent_id / f"{agent_id}.jsonl"


def connect_host_for(bind_host: str) -> str:
    trimmed = (bind_host or "").strip()
    if not trimmed or trimmed in _WILDCARD_HOSTS:
        return "127.0.0.1"
    return trimmed


def _format_base_url(scheme: str, host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{authority}:{port}"


def normalize_gateway_url(raw: str) -> str:
    """Drop path/userinfo/trailing slash and rewrite wildcard binds to loopback."""
    parsed = _parse_gateway_url(raw)
    return _format_base_url(parsed["scheme"], connect_host_for(parsed["host"]), parsed["port"])


def _parse_gateway_url(raw: str) -> dict[str, Any]:
    try:
        url = urlparse(raw.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid gateway URL. Set {ENV_GROKBOT_GATEWAY_URL} to an http(s) "
            f"origin such as {DEFAULT_GATEWAY_URL}."
        ) from exc
    if url.scheme not in {"http", "https"}:
        raise RuntimeError(
            f"Gateway URL must be http or https. Use {ENV_GROKBOT_GATEWAY_URL} "
            f"(alias {ENV_SAND_GATEWAY_URL})."
        )
    host = (url.hostname or "").strip()
    if not host:
        raise RuntimeError("Gateway URL is missing a host.")
    if url.port:
        port = url.port
    else:
        port = 443 if url.scheme == "https" else 80
    return {"scheme": url.scheme, "host": host, "port": port}


def _read_port(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        parsed = int(str(raw).strip(), 10)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _read_gateway_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    port = raw.get("port")
    if not isinstance(port, int) or port <= 0:
        return None
    token = raw.get("token")
    host = raw.get("host")
    scheme = raw.get("scheme")
    return {
        "port": port,
        "host": host if isinstance(host, str) else None,
        "scheme": scheme if scheme in {"http", "https"} else None,
        "token": token if isinstance(token, str) and token else None,
        "path": path,
    }


def load_gateway_file(root: Path | None = None) -> dict[str, Any] | None:
    for path in gateway_json_paths(root):
        parsed = _read_gateway_file(path)
        if parsed is not None:
            return parsed
    return None


def redact_secret(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


@dataclass(frozen=True)
class GatewayDiscovery:
    """Resolved gateway origin. ``token`` is in-memory only — never log it."""

    base_url: str
    token: str | None
    has_token: bool
    bind_host: str
    connect_host: str
    port: int
    scheme: str
    discovery_path: str | None

    def public_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "has_token": self.has_token,
            "bind_host": self.bind_host,
            "connect_host": self.connect_host,
            "port": self.port,
            "scheme": self.scheme,
            "discovery_path": self.discovery_path,
        }


def discover_gateway(root: Path | None = None) -> GatewayDiscovery:
    """Resolve URL + token from env, then gateway.json, then the documented default."""
    sand = root or resolve_sand_root()
    file_info = load_gateway_file(sand)

    override = _first_nonempty(
        os.getenv(ENV_GROKBOT_GATEWAY_URL),
        os.getenv(ENV_SAND_GATEWAY_URL),
    )
    from_url = _parse_gateway_url(override) if override else None

    scheme = (
        (from_url["scheme"] if from_url else None)
        or (file_info["scheme"] if file_info else None)
        or "http"
    )
    bind_host = (
        (from_url["host"] if from_url else None)
        or (os.getenv(ENV_SAND_GATEWAY_BIND_HOST) or "").strip()
        or (file_info["host"] if file_info else None)
        or "127.0.0.1"
    )
    port = (
        (from_url["port"] if from_url else None)
        or _read_port(os.getenv(ENV_SAND_HOST_PORT))
        or (file_info["port"] if file_info else None)
        or DEFAULT_GATEWAY_PORT
    )
    env_token = (os.getenv(ENV_SAND_GATEWAY_TOKEN) or "").strip()
    token = env_token or (file_info["token"] if file_info else None)
    host = connect_host_for(from_url["host"] if from_url else bind_host)
    return GatewayDiscovery(
        base_url=_format_base_url(scheme, host, port),
        token=token or None,
        has_token=bool(token),
        bind_host=bind_host,
        connect_host=host,
        port=port,
        scheme=scheme,
        discovery_path=str(file_info["path"]) if file_info else None,
    )


def default_agent_id() -> str | None:
    value = (os.getenv(ENV_GROKBOT_DEFAULT_AGENT_ID) or "").strip()
    return value or None
