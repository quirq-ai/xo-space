from __future__ import annotations
import json
import pathlib
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator


# Repo root, resolved from this file so manifest lookups are independent of the
# process CWD. The workspace-boot launcher starts ``server.py`` from ``$HOME``
# (not the project dir), so a CWD-relative ``config/agents`` path silently
# resolved to nothing and every adapter loaded an empty manifest — dropping
# skills, flags, and the agent ``env_file`` used for auth. Anchoring to
# ``__file__`` (adapters → cowork_agent → services → repo root) fixes it.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class BaseAgentAdapter(ABC):
    """
    All agent adapters must subclass this.
    'config' is a plain dict loaded by settings.load_agent_config(adapter_name).
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config

    # ── Abstract (must implement) ──────────────────────────────────────────────

    @abstractmethod
    async def run(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Non-streaming execution.
        Must return a dict with at minimum:
          { "message": str, "native_session_id": str | None }
        """

    @abstractmethod
    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Streaming execution.
        Must yield dicts of shape { "type": "token", "token": str }
        and end with exactly one { "done": True, "native_session_id": str | None }.
        Optional keywords from the intelligence layer (an adapter may ignore
        them): "intelligence" ({profile, model, effort, source}, the setup to
        run with) and "context" (text to hand the agent this turn, beside the
        user's message, never inside it).
        The done event may carry "outcome": what the turn cost and did
        (turns, duration_ms, api_duration_ms, cost_usd, is_error, stop,
        models, tokens), recorded by the intelligence outcome log.
        """

    # ── Concrete (override when needed) ───────────────────────────────────────

    async def setup(self) -> bool:
        """One-time credential or gateway setup. Return True when ready."""
        return True

    async def health(self) -> dict[str, Any]:
        """Lightweight liveness check surfaced by /health."""
        return {"ok": True}

    def load_commands(self) -> dict[str, Any]:
        """Read config/agents/{adapter_name}/manifest.json (legacy name
        commands.json read as a one-cycle fallback). Returns {} if absent."""
        base = _REPO_ROOT / "config" / "agents" / self.adapter_name
        for fname in ("manifest.json", "commands.json"):
            p = base / fname
            if p.exists():
                return json.loads(p.read_text())
        return {}

    # ── Required class attribute ───────────────────────────────────────────────

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Snake-case name matching the config/agents/ directory, e.g. 'claude_code'."""

