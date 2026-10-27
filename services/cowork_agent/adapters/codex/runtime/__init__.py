"""The codex runtime: storm's contract on codex's app-server (storm ch 03).

Loaded as capability ``runtime``: ``load_capability("runtime",
agent="codex").runtime()`` returns the one ``CodexRuntime``. This file is
what makes codex a storm runtime (discovery looks for it); the modules beside
it are the runtime itself.
"""

from services.cowork_agent.adapters.codex.runtime.core import CodexRuntime, runtime

__all__ = ["CodexRuntime", "runtime"]
