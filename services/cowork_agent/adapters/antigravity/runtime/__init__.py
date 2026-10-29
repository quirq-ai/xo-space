"""The agy runtime: storm's contract on agy's headless stream-json (storm ch 04).

Loaded as capability ``runtime``: ``load_capability("runtime",
agent="antigravity").runtime()`` returns the one ``AgyRuntime``. This file is
what makes agy a storm runtime (discovery looks for it); the modules beside
it are the runtime itself.
"""

from services.cowork_agent.adapters.antigravity.runtime.core import AgyRuntime, runtime

__all__ = ["AgyRuntime", "runtime"]
