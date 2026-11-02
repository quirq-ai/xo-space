"""The env one agy process runs with, built from scratch at spawn time
(storm ch 04 §10, ch 16 §1), and where agy keeps its files.

Base: the agent-neutral whitelist (``adapters/env_whitelist.py``), plus
agy's own variables (its sign-in switches and gateway settings). Then what
every XO session sets: ``AGY_CLI_DISABLE_AUTO_UPDATE=true`` (agy 1.4.10
honours ``true`` and ignores ``1``, which updated the CLI under a running
server twice), and the session's socket (``SOCKET_ENV``), through which XO's
plugin reaches this runtime. Then ``spec.env``.

No credential goes in: XO's MCP servers are reached through the socket, and
the runtime itself makes their HTTP calls, so a token never sits in agy's env
where its shell commands would inherit it. agy's own login stays in its token
file, which agy reads and refreshes itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.env_whitelist import base_env, log_dropped

#: agy's own variables, passed through as set (Application Default
#: Credentials sign-in, a Gemini key with ``modelProvider: gemini``, a custom
#: endpoint, and its gateway settings).
OWN_VARS = ("AGY_ADC_AUTH", "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS",
            "GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "AGY_LLM_GATEWAY_URL", "AGY_LLM_GATEWAY_API_KEY",
            "AGY_LLM_GATEWAY_HEADERS", "AGY_LLM_GATEWAY_CA_CERT", "AGY_LLM_GATEWAY_PROXY_URL")
#: The variables that carry agy's credentials when set (``auth_env``): storm
#: redacts their values from what it stores.
AUTH_VARS = ("GEMINI_API_KEY", "AGY_LLM_GATEWAY_API_KEY")
#: The variable that tells XO's plugin which session it serves. Without it the
#: plugin's hook and MCP relay do nothing, so the person's own agy is untouched.
SOCKET_ENV = "XO_AGY_SOCKET"
#: Set on every agy process the runtime starts, probes included.
ALWAYS = {"AGY_CLI_DISABLE_AUTO_UPDATE": "true", "NO_COLOR": "1"}


def _home(environ: Mapping[str, str] | None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("HOME") or str(Path.home()))


def agy_home(environ: Mapping[str, str] | None = None) -> Path:
    """agy's state folder: ``~/.gemini/antigravity-cli`` under the live
    ``HOME`` (its token file, settings, conversations, transcripts)."""
    return _home(environ) / ".gemini" / "antigravity-cli"


def config_root(environ: Mapping[str, str] | None = None) -> Path:
    """agy's global customization root, ``~/.gemini/config``: its plugins,
    agents and MCP servers."""
    return _home(environ) / ".gemini" / "config"


def token_file(environ: Mapping[str, str] | None = None) -> Path:
    return agy_home(environ) / "antigravity-oauth-token"


def brain_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where agy keeps each conversation's transcript and artifacts."""
    return agy_home(environ) / "brain"


def session_env(*, spec_env: Mapping[str, str], socket: str | None, label: str,
                environ: Mapping[str, str] | None = None, store: Mapping[str, str] | None = None) -> dict[str, str]:
    """The whole env for one agy process: whitelist, agy's own variables,
    ``ALWAYS``, the session's socket, then ``spec.env``."""
    environ = os.environ if environ is None else environ
    env = base_env(names=OWN_VARS, environ=environ, store=store)
    env.update(ALWAYS)
    env.pop(SOCKET_ENV, None)
    if socket:
        env[SOCKET_ENV] = socket
    env.update(spec_env)
    log_dropped(label, env, environ)
    return env


def probe_env(label: str) -> dict[str, str]:
    """For a model-free agy call (``--version``, ``models``, ``/usage``):
    no session socket, so XO's plugin stays out of it."""
    return session_env(spec_env={}, socket=None, label=label)
