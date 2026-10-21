"""The env one ``claude`` session runs with, built from scratch at spawn
time (storm ch 02 §2.2, ch 16 §1).

Base: the agent-neutral whitelist (``adapters/env_whitelist.py``), plus
``CLAUDE_CONFIG_DIR``, the provider-gateway variables and the variables the
person's own MCP servers reference. Auth: today's function
(``ClaudeCodeAdapter.apply_auth``), reused unchanged, so a session and the
``/providers/status`` tile resolve auth identically. Never: the whitelist's
never-list, which drops every ``CLAUDE_CODE_*`` or ``CLAUDECODE`` the server
inherited (a parent Claude Code's session variables, declared in this agent's
manifest) even when an MCP config names it; the runtime sets its own after that
step. A ``CLAUDE_CODE_*`` key the person put in the secrets store or
``runtime.env`` passes, except the switches the runtime sets from the spec.
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
#: The variables that carry claude's credentials: the feature list names them
#: (``auth_env``) so storm redacts their values from what it stores (ch 16 §3).
CREDENTIAL_VARS = (*AUTH_VARS, "ANTHROPIC_AUTH_TOKEN")
#: Set on every session: no self-update mid-run (ch 02 §9), and
#: ``session_state_changed`` lines, the only source of ``SessionIdle`` (§4.3).
ALWAYS = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1"}


#: Set by the runtime from the spec (``command.session_switches``); never
#: taken from any other source, or a stored value would turn a plain session
#: into a coordinator.
RUNTIME_OWNED = ("CLAUDE_CODE_COORDINATOR_MODE", "CLAUDE_CODE_MCP_PREWAIT_SERVERS", "CLAUDE_CODE_TERMINAL_MCP_TOOLS",
                 "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH")


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
    env = base_env(names=names, environ=environ, store=store)
    for name in (*AUTH_VARS, *RUNTIME_OWNED):
        env.pop(name, None)             # decided below, whatever their source
    env.update(ClaudeCodeAdapter({}).apply_auth({k: environ[k] for k in AUTH_VARS if k in environ}))
    env.update(ALWAYS)
    env.update(switches)
    env.update(spec_env)
    log_dropped(label, env, environ)
    return env
