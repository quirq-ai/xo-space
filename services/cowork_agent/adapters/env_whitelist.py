"""The shared part of every storm runtime's process environment (storm ch 16 §1).

A runtime builds its CLI's env **from scratch**, never as a copy of the
server's: the server holds secrets (the XO key, decision-model keys loaded from
an ancestor ``.env``, a parent agent's session variables) that must not reach an
agent's shell. Each adapter owns one function that starts from ``base_env()``
and adds its CLI's own auth and switches; this module names no agent.

What ``base_env()`` passes, read from the **live** ``os.environ`` at spawn time
(``PATH`` and the secrets store change while the server runs):

* the OS baseline: ``OS_NAMES`` (git's sign-in helpers among them) and every
  name starting with ``OS_PREFIXES``;
* XO's address, ``HOST`` and ``PORT`` (the ``xo-projects`` skill calls
  ``http://${HOST:-localhost}:${PORT:-5002}``);
* every key currently in the Space's secrets store, so a secret added in
  xo-swarm still reaches the agent (its value from ``os.environ``, where the
  store syncs it, else from the store itself);
* every key of the Setup tab's ``runtime.env`` (the server loads it into
  ``os.environ`` at boot);
* the names the adapter adds: its CLI's own variables and those its MCP config
  references (``referenced_vars``).

Anything else is dropped, and ``log_dropped`` records the dropped **names**
(never values) at debug level, so a missing variable is explained in one line.

``never_list()`` (ch 16 §1.2) is applied last, to every name taken from the
server's env whatever asked for it, so an MCP config an agent edits can't name
its way to a server secret. It holds XO's own credentials (``XO_SECRET_NAMES``),
the router's decision-model keys (every key of ``router.env``, so no provider is
named here) and the variables each agent's manifest declares a session of its
CLI hands its children (``inherited_env``: ``names``, ``prefixes`` and the
``keep`` exceptions), unioned over every installed agent, so no session passes
another CLI's either. A key of the secrets store or ``runtime.env`` is the
person's own configuration and passes (ch 16 §1.2; swarm contract (b)4).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from services.cowork_agent.registry.agent_env import load_env_entries, parse_env_file
from services.cowork_agent.registry.agent_registry import AgentManifest, all_agents
from services.storage.layout import secrets_dir, settings_dir
from services.storage.paths import quirq_state_dir

log = logging.getLogger(__name__)

#: The OS baseline every CLI needs to run: its tools, locale, proxy and CA,
#: and how git authenticates (§0.111: the person's askpass helper, ssh command
#: and agent, so git in a session signs in as in their terminal; on Coder the
#: first two run ``coder gitssh``). ``GIT_EDITOR`` stays out: nothing in a
#: session may open an editor.
OS_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TZ", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "NODE_OPTIONS",
    "GIT_ASKPASS", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK",
})
OS_PREFIXES = ("LC_", "XDG_", "NVM_")
#: Where XO itself listens, for the agent's calls back to the Space.
XO_ADDRESS = frozenset({"HOST", "PORT"})
#: XO's own credentials, never passed from the server's env: a session's
#: comms and session tokens (the runtime sets the session's own after the
#: whitelist; one in the server's env can only be another session's), the
#: Space's account key and the browser sign-in pair.
XO_SECRET_NAMES = ("XO_COMMS_TOKEN", "XO_SESSION_TOKEN", "XO_API_KEY", "XO_AUTH_SESSION_ID", "XO_POLL_TOKEN")
#: The workspace's own agent credentials on Coder (``CODER_AGENT_TOKEN``,
#: ``_AUTH``, ``_URL``): never a session's, even where an MCP config names
#: them (§0.113).
HOST_SECRET_PREFIXES = ("CODER_AGENT_",)
#: The manifest block where an agent declares what a session of its CLI hands
#: its children: ``{"names": [...], "prefixes": [...], "keep": [...]}``.
INHERITED_ENV_KEY = "inherited_env"

_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def stored_env() -> dict[str, str]:
    """The Space's secrets store as ``{key: value}``, plus the Setup tab's
    ``runtime.env``; empty when neither exists yet."""
    entries = list(load_env_entries())
    runtime_file = _runtime_settings_file()
    try:
        entries += parse_env_file(runtime_file.read_text(errors="replace"))
    except OSError:
        pass                    # no runtime.env: nothing to add
    return {e["key"]: e["value"] for e in entries if (e.get("key") or "").strip()}


def _runtime_settings_file() -> Path:
    """The Setup tab's ``runtime.env``, resolved as the server resolves it at
    boot (``QUIRQ_RUNTIME_FILE``, else its home under ``settings/``, else the
    older place at the state root)."""
    configured = (os.environ.get("QUIRQ_RUNTIME_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    new = settings_dir() / "runtime.env"
    old = quirq_state_dir() / "runtime.env"
    return old if not new.is_file() and old.is_file() else new


@dataclass(frozen=True)
class NeverList:
    """The names no session gets from the server's env (ch 16 §1.2)."""

    names: frozenset[str]
    prefixes: tuple[str, ...]
    keep: frozenset[str]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and (
            name in self.names or (name.startswith(self.prefixes) and name not in self.keep))


def _router_env_file() -> Path:
    """Where the router keeps its decision-model keys (§0.81)."""
    return secrets_dir() / "router.env"


def decision_model_key_names() -> set[str]:
    """The **names** of the router's decision-model keys: every key of
    ``router.env``. No file, no names; any other read failure raises, so a
    spawn fails rather than guess."""
    try:
        text = _router_env_file().read_text(errors="replace")
    except FileNotFoundError:
        return set()
    return {e["key"] for e in parse_env_file(text) if e["key"]}


def _declared(manifest: AgentManifest, field: str) -> list[str]:
    block = manifest.raw.get(INHERITED_ENV_KEY) or {}
    values = block.get(field, []) if isinstance(block, dict) else None
    if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
        raise ValueError(f"agent {manifest.name!r}: manifest field {INHERITED_ENV_KEY}.{field} "
                         "must be a list of variable names")
    return values


def never_list() -> NeverList:
    """ch 16 §1.2's never-list, read at spawn time: ``XO_SECRET_NAMES``,
    ``HOST_SECRET_PREFIXES``, the router's key names and every installed
    agent's ``inherited_env``."""
    manifests = all_agents()
    return NeverList(
        names=frozenset({*XO_SECRET_NAMES, *decision_model_key_names(),
                         *(n for m in manifests for n in _declared(m, "names"))}),
        prefixes=(*HOST_SECRET_PREFIXES, *(p for m in manifests for p in _declared(m, "prefixes"))),
        keep=frozenset(k for m in manifests for k in _declared(m, "keep")),
    )


def base_env(*, names: Iterable[str] = (), environ: Mapping[str, str] | None = None,
             store: Mapping[str, str] | None = None) -> dict[str, str]:
    """The whitelisted part of a CLI's env. ``names`` are extra variables to
    pass when set. ``never_list()`` removes its names from whatever the
    server's env gives, whichever list asked for them; a key of the secrets
    store or ``runtime.env`` is the person's own configuration and always
    passes. ``environ`` and ``store`` are the live ``os.environ`` and
    ``stored_env()`` unless given."""
    environ = os.environ if environ is None else environ
    store = stored_env() if store is None else store
    never = never_list()
    wanted = set(names) | XO_ADDRESS
    env = {k: v for k, v in environ.items()
           if (k in OS_NAMES or k in wanted or k.startswith(OS_PREFIXES)) and k not in never}
    for key, value in store.items():
        env[key] = environ.get(key, value)
    return env


def referenced_vars(config: Any) -> set[str]:
    """Every ``${VAR}`` (or ``${VAR:-default}``) named anywhere in a parsed
    config: the variables an MCP server needs from the env. Names only."""
    found: set[str] = set()
    stack = [config]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.update(_VAR_REF.findall(item))
        elif isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return found


def log_dropped(label: str, env: Mapping[str, str], environ: Mapping[str, str] | None = None) -> None:
    """Debug-log the names of the server's variables a child does not get."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    environ = os.environ if environ is None else environ
    dropped = sorted(set(environ) - set(env))
    log.debug("%s: env drops %d variable(s): %s", label, len(dropped), ", ".join(dropped))
