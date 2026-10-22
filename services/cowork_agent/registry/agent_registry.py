"""
Agent manifest registry.

Loads JSON manifests from `config/agents/<agent>/manifest.json` — one
subdirectory per supported agent tool. Each manifest declares the binary
name, filesystem layout, API env-var names, command templates, and
provider/channel recipes. Call sites go through `get_active_agent()` so the
binary, paths, and argv shapes are never hardcoded.

The file is named `manifest.json` (legacy name `commands.json` is still read
as a fallback) so it cannot be confused with the agent's own config file.

Which manifest is active is resolved in this order:

1. `AGENT_NAME` env var — runtime override (takes precedence when set).
2. `DEFAULT_AGENT` env var — baseline default (what ships in `.env.example`).
3. If neither is set and only one manifest exists, that one is used.
4. If neither is set and multiple manifests exist, loading raises — we
   do not guess.

Both env vars are matched against the `name` field inside each manifest.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]  # registry/ → cowork_agent/ → services/ → repo root
_MANIFEST_DIR = _REPO_ROOT / "config" / "agents"


def _expand(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expanduser(value))


@dataclass(frozen=True)
class AgentManifest:
    """In-memory view of one agent manifest file.

    Paths are pre-expanded (`~` → home). `raw` keeps the original JSON so
    call sites can reach into provider/channel recipes without the loader
    needing to know their exact shape.
    """

    name: str
    binary: str
    home_dir: Path
    env_file: Path
    config_file: Path
    agents_dir: Path
    workspace_dir: Path
    provisioning_log: Path
    cwd: Path
    cli_timeout_seconds: int
    api_url: str
    api_token: str
    api_model: str
    session_header: str
    model_prefix: str
    model_capabilities: dict
    command_templates: dict[str, list[str]]
    providers: dict[str, dict]
    channels: dict[str, dict]
    raw: dict
    #: The files in a checkout that make this agent's CLI run code on its own
    #: (its project settings, hooks, MCP servers), as globs relative to the
    #: checkout. Declared here, not only by a runtime, so storm watches and
    #: protects them whichever agent runs, one with no runtime included
    #: (storm ch 16 §2.1).
    project_config_paths: tuple[str, ...] = ()

    # ── Command rendering ────────────────────────────────────────────────

    def command(self, key: str, /, **params: Any) -> list[str]:
        """Render a command template to an argv list.

        `{binary}` is always injected. Other placeholders come from `params`.
        Missing placeholders raise `KeyError` so recipes can't silently
        render to half-substituted argv (which would reach the shell).
        """
        template = self.command_templates.get(key)
        if template is None:
            raise KeyError(f"agent '{self.name}' has no command template '{key}'")
        merged = {"binary": self.binary, **params}
        return [part.format_map(merged) for part in template]

    def render_recipe_commands(self, commands: list[dict]) -> list[list[str]]:
        """Render a list of `{template, params}` entries to argv lists.

        Used by provider/channel recipes so the recipe refers to named
        templates instead of hand-building argv — changing a template
        shape in the manifest propagates to every recipe that uses it.
        """
        out: list[list[str]] = []
        for entry in commands or []:
            template_key = entry.get("template")
            params = entry.get("params") or {}
            if not template_key:
                raise ValueError(f"recipe command missing 'template' field: {entry!r}")
            out.append(self.command(template_key, **params))
        return out


def _build_manifest(path: Path) -> AgentManifest:
    raw = json.loads(path.read_text())

    def req(key: str) -> Any:
        if key not in raw:
            raise ValueError(f"manifest {path.name} is missing required field '{key}'")
        return raw[key]

    api = raw.get("api") or {}
    api_url = os.getenv(api.get("url_env", "")) or api.get("url_default", "")
    api_token = os.getenv(api.get("token_env", "")) or api.get("token_default", "")
    api_model = os.getenv(api.get("model_env", "")) or api.get("model_default", "")

    return AgentManifest(
        name=req("name"),
        binary=req("binary"),
        home_dir=_expand(req("home_dir")),
        env_file=_expand(req("env_file")),
        config_file=_expand(req("config_file")),
        agents_dir=_expand(req("agents_dir")),
        workspace_dir=_expand(raw.get("workspace_dir")),
        provisioning_log=_expand(raw.get("provisioning_log", "")) or _expand(req("home_dir")) / "provisioning.log",
        cwd=_expand(raw.get("cwd", "~")),
        cli_timeout_seconds=int(raw.get("cli_timeout_seconds", 30)),
        api_url=api_url,
        api_token=api_token,
        api_model=api_model,
        session_header=api.get("session_header", ""),
        model_prefix=raw.get("model_prefix", raw["name"]),
        model_capabilities=dict(raw.get("model_capabilities") or {}),
        command_templates=dict(raw.get("commands") or {}),
        providers=dict(raw.get("providers") or {}),
        channels=dict(raw.get("channels") or {}),
        raw=raw,
        project_config_paths=_globs(raw.get("project_config_paths"), path, "project_config_paths"),
    )


def _globs(value: Any, path: Path, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"manifest {path} field '{key}' must be a list of path globs (strings)")
    return tuple(value)


def _discover_manifests() -> dict[str, AgentManifest]:
    if not _MANIFEST_DIR.exists():
        raise FileNotFoundError(
            f"agent manifest directory not found: {_MANIFEST_DIR}. "
            "Add a `config/agents/<name>/manifest.json` describing the default agent tool."
        )
    manifests: dict[str, AgentManifest] = {}
    # One subdir per agent, each containing a `manifest.json` file (legacy name
    # `commands.json` still read as a one-cycle fallback).
    # Files that lack the `binary` field are adapter configs, not agent manifests — skip them.
    for subdir in sorted(p for p in _MANIFEST_DIR.iterdir() if p.is_dir()):
        manifest_path = subdir / "manifest.json"
        if not manifest_path.exists():
            manifest_path = subdir / "commands.json"
        if not manifest_path.exists():
            continue
        try:
            raw_check = json.loads(manifest_path.read_text())
        except Exception:
            continue
        if "binary" not in raw_check:
            continue
        manifest = _build_manifest(manifest_path)
        if manifest.name in manifests:
            raise ValueError(f"duplicate manifest name '{manifest.name}' in {_MANIFEST_DIR}")
        manifests[manifest.name] = manifest
    if not manifests:
        raise FileNotFoundError(
            f"no agent manifests found in {_MANIFEST_DIR} "
            "(expected `<agent>/manifest.json` subdirectories)."
        )
    return manifests


_MANIFESTS: dict[str, AgentManifest] | None = None
_DEFAULT: AgentManifest | None = None


def _ensure_loaded() -> None:
    global _MANIFESTS, _DEFAULT
    if _MANIFESTS is not None:
        return
    _MANIFESTS = _discover_manifests()
    # AGENT_NAME wins; DEFAULT_AGENT is the baseline fallback that ships in
    # .env.example. The var-name in the error message reflects which one the
    # user set (or AGENT_NAME if both are empty) so it's obvious where to fix.
    agent_name = (os.getenv("AGENT_NAME") or "").strip()
    default_agent = (os.getenv("DEFAULT_AGENT") or "").strip()
    requested = agent_name or default_agent
    if requested:
        if requested not in _MANIFESTS:
            available = ", ".join(sorted(_MANIFESTS))
            src = "AGENT_NAME" if agent_name else "DEFAULT_AGENT"
            raise ValueError(
                f"{src}='{requested}' does not match any manifest. Available: {available}"
            )
        _DEFAULT = _MANIFESTS[requested]
    elif len(_MANIFESTS) == 1:
        _DEFAULT = next(iter(_MANIFESTS.values()))
    elif "openclaw" in _MANIFESTS:
        import warnings
        available = ", ".join(sorted(_MANIFESTS))
        warnings.warn(
            f"Neither AGENT_NAME nor DEFAULT_AGENT is set. Defaulting to 'openclaw'. "
            f"Add AGENT_NAME={'/'.join(sorted(_MANIFESTS))} (any among the supported) to your .env to silence this warning.",
            stacklevel=2,
        )
        _DEFAULT = _MANIFESTS["openclaw"]
    else:
        available = ", ".join(sorted(_MANIFESTS))
        raise ValueError(
            f"multiple agent manifests found ({available}) but neither AGENT_NAME nor DEFAULT_AGENT is set."
        )


def get_active_agent() -> AgentManifest:
    """Return the manifest for the active agent.

    Resolved from `AGENT_NAME` (override) → `DEFAULT_AGENT` (baseline) →
    single-manifest auto-pick. See module docstring for the full chain.
    """
    _ensure_loaded()
    assert _DEFAULT is not None
    return _DEFAULT


def get_agent(name: str) -> AgentManifest:
    """Return a specific manifest by name (raises if unknown)."""
    _ensure_loaded()
    assert _MANIFESTS is not None
    if name not in _MANIFESTS:
        available = ", ".join(sorted(_MANIFESTS))
        raise KeyError(f"unknown agent '{name}'. Available: {available}")
    return _MANIFESTS[name]


def all_agents() -> list[AgentManifest]:
    """Every discovered agent manifest.

    For host-level bootstraps that target all installed agents (e.g. the
    bundled-skill installer), so they iterate manifests instead of hardcoding
    backend names or home-dir paths.
    """
    _ensure_loaded()
    assert _MANIFESTS is not None
    return list(_MANIFESTS.values())
