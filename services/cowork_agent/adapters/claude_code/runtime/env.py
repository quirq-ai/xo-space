"""The env one ``claude`` session runs with, built from scratch at spawn
time (storm ch 02 §2.2, ch 16 §1).

Base: the agent-neutral whitelist (``adapters/env_whitelist.py``), plus
``CLAUDE_CONFIG_DIR``, the provider-gateway variables and the variables the
person's own MCP servers reference. Auth: today's function
(``ClaudeCodeAdapter.apply_auth``), reused unchanged, so a session and the
``/providers/status`` tile resolve auth identically. Never: a ``CLAUDE_CODE_*``
or ``CLAUDECODE`` the server inherited (a parent Claude Code's session
variables); the runtime sets its own after that step.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
from services.cowork_agent.adapters.env_whitelist import base_env, log_dropped, referenced_vars


#: The variables today's auth function decides about.
AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
#: Passed through as set: claude's config folder and an Anthropic-compatible gateway.
OWN_VARS = ("CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
#: Set on every session: no self-update mid-run (ch 02 §9), and
#: ``session_state_changed`` lines, the only source of ``SessionIdle`` (§4.3).
ALWAYS = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1"}


def _inherited(name: str) -> bool:
    return name.startswith("CLAUDE_CODE_") or name == "CLAUDECODE" or name in AUTH_VARS


def mcp_var_names(config_file: Path) -> set[str]:
    """Every ``${VAR}`` the person's own MCP servers name in ``~/.claude.json``
    (user scope and every project's local scope). Names only; an unreadable
    file names nothing."""
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    servers = [data.get("mcpServers")]
    projects = data.get("projects")
    if isinstance(projects, dict):
        servers += [p.get("mcpServers") for p in projects.values() if isinstance(p, dict)]
    return referenced_vars([s for s in servers if isinstance(s, dict)])


def session_env(*, switches: Mapping[str, str], spec_env: Mapping[str, str], label: str,
                environ: Mapping[str, str] | None = None, store: Mapping[str, str] | None = None,
                home: Path | None = None) -> dict[str, str]:
    """The whole env for one ``claude`` process: whitelist, auth, the
    session's own switches, then ``spec.env``."""
    environ = os.environ if environ is None else environ
    home = Path(environ.get("HOME") or Path.home()) if home is None else home
    names = {*OWN_VARS, *mcp_var_names(home / ".claude.json")}
    env = base_env(names=names, drop=_inherited, environ=environ, store=store)
    env.update(ClaudeCodeAdapter({}).apply_auth({k: environ[k] for k in AUTH_VARS if k in environ}))
    env.update(ALWAYS)
    env.update(switches)
    env.update(spec_env)
    log_dropped(label, env, environ)
    return env
