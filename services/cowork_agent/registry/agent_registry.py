"""
Agent manifest registry.

Loads JSON manifests from `config/agents/<agent>/manifest.json` — one
subdirectory per supported agent tool. Each manifest declares the binary
name, filesystem layout, API env-var names, command templates, and
provider/channel recipes. Call sites go through `get_active_agent()` so the
binary, paths, and argv shapes are never hardcoded.

The file is named `manifest.json` (legacy name `commands.json` is still read
as a fallback) so it cannot be confused with the agent's own config file.

A manifest with a `boot_rank` is a **runtime** (it runs storm threads); one
without is an **exception** with its own gateway. Two seams, the only readers
of `AGENT_NAME`, `DEFAULT_AGENT` and the Space's preferred runtime, resolve
which agent answers, on every call:

* `space_agent()`: the agent that owns the Space's chat, its default
  capabilities and its own routes. The exception `AGENT_NAME` names (or
  `DEFAULT_AGENT`, when `AGENT_NAME` is unset), else `default_runtime()`.
* `default_runtime()`: what a thread runs on when nobody chose. The first
  runtime among the Space setting (the runtime policy's `default`, mirrored
  here by storm), `AGENT_NAME` and `DEFAULT_AGENT`; else the first runtime by
  `boot_rank` the space advisory knows is signed in, then the first
  installed; else the `claude_code` safe-boot default.

A name in either variable that matches no manifest raises, so a typo stops the
boot instead of turning the Space into another agent's.
"""

import json
import os
import shutil
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
    #: Where this runtime comes in the first-boot order; None for an exception.
    boot_rank: int | None = None

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
        boot_rank=raw.get("boot_rank"),
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
_SAFE_BOOT = "claude_code"
_preferred: str | None = None


def _ensure_loaded() -> dict[str, AgentManifest]:
    global _MANIFESTS
    if _MANIFESTS is None:
        _MANIFESTS = _discover_manifests()
    return _MANIFESTS


def _env_agents() -> list[str]:
    """`AGENT_NAME` then `DEFAULT_AGENT`, as set; raises for a name with no manifest."""
    manifests = _ensure_loaded()
    names = []
    for var in ("AGENT_NAME", "DEFAULT_AGENT"):
        name = (os.getenv(var) or "").strip()
        if name and name not in manifests:
            raise ValueError(f"{var}='{name}' does not match any manifest. Available: {', '.join(sorted(manifests))}")
        if name:
            names.append(name)
    return names


def preferred_runtime() -> str | None:
    """The Space setting, or None while nobody has chosen."""
    return _preferred


def set_preferred_runtime(name: str | None) -> None:
    """Mirror the Space setting (the runtime policy's `default`); storm calls this when it loads or writes it."""
    global _preferred
    _preferred = name


def runtimes() -> list[AgentManifest]:
    """Every runtime's manifest, in `boot_rank` order."""
    return sorted((m for m in _ensure_loaded().values() if m.boot_rank is not None), key=lambda m: m.boot_rank)


class UnknownRuntime(ValueError):
    """A name that is no runtime's."""


def runtime(name: str) -> AgentManifest:
    """The runtime named `name`; raises `UnknownRuntime` for any other name."""
    found = next((m for m in runtimes() if m.name == name), None)
    if found is None:
        raise UnknownRuntime(f"No storm runtime is named {name!r}; GET /api/chat/runtimes lists them.")
    return found


def installed(binary: str) -> bool:
    """Whether `binary`, a path or a name on PATH, can be run."""
    if os.path.isabs(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


def installed_runtimes() -> list[AgentManifest]:
    """Every runtime whose CLI is installed, in `boot_rank` order."""
    return [m for m in runtimes() if installed(m.binary)]


def default_runtime() -> str:
    """The runtime a thread runs on when nobody chose (see the module docstring)."""
    names = {m.name for m in runtimes()}
    chosen = next((n for n in (_preferred, *_env_agents()) if n in names), None)
    if chosen:
        return chosen
    from services import advisory     # imported here: the advisory loads adapters, whose loader imports this module

    facts = advisory.current().snapshot().runtimes
    return next((m.name for m in runtimes()
                 if (facts[m.name].signed_in if m.name in facts else installed(m.binary))), _SAFE_BOOT)


def resolve_runtime(name: str) -> str:
    """The runtime ``name`` names, or is an alias of (the manifest's ``aliases``); else ``name``."""
    return next((m.name for m in runtimes() if name in m.raw.get("aliases", ())), name)


def space_agent() -> str:
    """The agent that owns the Space's chat, default capabilities and own routes."""
    named = _env_agents()[:1]
    return named[0] if named and get_agent(named[0]).boot_rank is None else default_runtime()


def named_agent() -> str | None:
    """The agent `AGENT_NAME` (else `DEFAULT_AGENT`) names: the template's, the one setup may install."""
    return next(iter(_env_agents()), None)


def local_agents() -> list[str]:
    """`named_agent()`, then every other installed runtime: the agents this Space sets up at boot."""
    named = named_agent()
    others = [m.name for m in installed_runtimes() if m.name != named]
    return [named, *others] if named else others


def get_active_agent() -> AgentManifest:
    """The manifest of `space_agent()`."""
    return get_agent(space_agent())


def get_agent(name: str) -> AgentManifest:
    """Return a specific manifest by name (raises if unknown)."""
    manifests = _ensure_loaded()
    if name not in manifests:
        raise KeyError(f"unknown agent '{name}'. Available: {', '.join(sorted(manifests))}")
    return manifests[name]


def all_agents() -> list[AgentManifest]:
    """Every discovered agent manifest.

    For host-level bootstraps that target all installed agents (e.g. the
    bundled-skill installer), so they iterate manifests instead of hardcoding
    backend names or home-dir paths.
    """
    return list(_ensure_loaded().values())
