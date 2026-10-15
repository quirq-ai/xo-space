"""The shared part of every storm runtime's process environment (storm ch 16 §1).

A runtime builds its CLI's env **from scratch**, never as a copy of the
server's: the server holds secrets (the XO key, decision-model keys loaded from
an ancestor ``.env``, a parent agent's session variables) that must not reach an
agent's shell. Each adapter owns one function that starts from ``base_env()``
and adds its CLI's own auth and switches; this module names no agent.

What ``base_env()`` passes, read from the **live** ``os.environ`` at spawn time
(``PATH`` and the secrets store change while the server runs):

* the OS baseline: ``OS_NAMES`` and every name starting with ``OS_PREFIXES``;
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
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from services.cowork_agent.registry.agent_env import load_env_entries, parse_env_file
from services.storage.layout import settings_dir
from services.storage.paths import quirq_state_dir

log = logging.getLogger(__name__)

#: The OS baseline every CLI needs to run: its tools, locale, proxy and CA.
OS_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TZ", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "NODE_OPTIONS",
})
OS_PREFIXES = ("LC_", "XDG_", "NVM_")
#: Where XO itself listens, for the agent's calls back to the Space.
XO_ADDRESS = frozenset({"HOST", "PORT"})

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


def base_env(*, names: Iterable[str] = (), drop: Callable[[str], bool] = lambda name: False,
             environ: Mapping[str, str] | None = None,
             store: Mapping[str, str] | None = None) -> dict[str, str]:
    """The whitelisted part of a CLI's env. ``names`` are extra variables to
    pass when set. ``drop`` removes what the server only *inherited* (a parent
    agent session's variables, ch 16 §1.2); a key of the secrets store or
    ``runtime.env`` is the person's own configuration and always passes.
    ``environ`` and ``store`` are the live ``os.environ`` and ``stored_env()``
    unless given."""
    environ = os.environ if environ is None else environ
    store = stored_env() if store is None else store
    wanted = set(names) | XO_ADDRESS
    env = {k: v for k, v in environ.items()
           if (k in OS_NAMES or k in wanted or k.startswith(OS_PREFIXES)) and not drop(k)}
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
