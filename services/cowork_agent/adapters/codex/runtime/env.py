"""The env one codex app-server runs with, built from scratch at spawn time
(storm ch 03 §3.3, ch 16 §1).

Base: the agent-neutral whitelist (``adapters/env_whitelist.py``), plus
``CODEX_HOME`` (the person's own codex home, so the login and their settings
are codex's as always), the OpenAI key and gateway variables, every
``model_providers.*.env_key`` the person's ``config.toml`` names and every
variable their own MCP servers there take (``env_vars``,
``bearer_token_env_var``, ``${VAR}``). The whitelist's never-list drops
every other ``CODEX_*`` the server inherited (codex's manifest declares the
prefix and keeps ``CODEX_HOME``). Then the session's own: each HTTP MCP
server's token (``McpServer.env``, read by codex through
``bearer_token_env_var``, never written anywhere), and ``spec.env``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Iterable, Mapping

from services.cowork_agent.adapters.env_whitelist import base_env, log_dropped, referenced_vars

#: codex's own variables, passed through as set.
OWN_VARS = ("CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
            "CODEX_API_KEY")
#: The variables that log codex in, whatever the person's config adds
#: (``credential_vars``): the feature list names them (``auth_env``) so storm
#: redacts their values from what it stores (ch 16 §3).
AUTH_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")
#: Set on every session by the runtime (none yet: codex checks for updates
#: only in its TUI, never in the app-server).
ALWAYS: dict[str, str] = {}


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """The codex home this runtime runs on: ``$CODEX_HOME``, else ``~/.codex``
    under the live ``HOME``."""
    environ = os.environ if environ is None else environ
    configured = (environ.get("CODEX_HOME") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    home = environ.get("HOME") or str(Path.home())
    return Path(home) / ".codex"


def read_config(home: Path) -> dict:
    """The person's ``config.toml``, parsed; ``{}`` when it's missing or
    unreadable (codex itself reports a broken file when it starts)."""
    try:
        with (home / "config.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def provider_key_vars(config: Mapping) -> set[str]:
    """Every ``model_providers.<id>.env_key`` the person's config names."""
    providers = config.get("model_providers")
    if not isinstance(providers, Mapping):
        return set()
    return {p["env_key"] for p in providers.values()
            if isinstance(p, Mapping) and isinstance(p.get("env_key"), str) and p["env_key"]}


def mcp_var_names(config: Mapping) -> set[str]:
    """Every variable the person's own MCP servers take from the env."""
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return set()
    names = referenced_vars([s for s in servers.values() if isinstance(s, Mapping)])
    for server in servers.values():
        if not isinstance(server, Mapping):
            continue
        if isinstance(server.get("bearer_token_env_var"), str):
            names.add(server["bearer_token_env_var"])
        listed = server.get("env_vars")
        if isinstance(listed, list):
            names.update(v for v in listed if isinstance(v, str))
    return names


_CREDENTIALS: dict[tuple[str, int], tuple[str, ...]] = {}


def credential_vars(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """``auth_env``: codex's own key variables and the provider keys the
    person's config names; read again only when the config changes."""
    path = codex_home(environ) / "config.toml"
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), 0)
    if key not in _CREDENTIALS:
        _CREDENTIALS.clear()
        _CREDENTIALS[key] = tuple(sorted({*AUTH_VARS, *provider_key_vars(read_config(path.parent))}))
    return _CREDENTIALS[key]


def session_env(*, spec_env: Mapping[str, str], server_env: Iterable[Mapping[str, str]] = (), label: str,
                environ: Mapping[str, str] | None = None, store: Mapping[str, str] | None = None) -> dict[str, str]:
    """The whole env for one ``codex app-server``: whitelist, the session's
    MCP tokens, then ``spec.env``. Blocking (reads the person's config)."""
    environ = os.environ if environ is None else environ
    config = read_config(codex_home(environ))
    names = {*OWN_VARS, *provider_key_vars(config), *mcp_var_names(config)}
    env = base_env(names=names, environ=environ, store=store)
    env.update(ALWAYS)
    for extra in server_env:
        env.update(extra)
    env.update(spec_env)
    log_dropped(label, env, environ)
    return env
