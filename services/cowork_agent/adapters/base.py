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

    # ── Chat (override to chat through AgentDispatcher) ─────────────────────────
    # An agent whose plain chat runs on a storm runtime has no chat here; the
    # defaults refuse, and ``chats()`` says so without building the adapter.

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Non-streaming chat turn. An override returns a dict with at minimum:
          { "message": str, "native_session_id": str | None }
        """
        raise NotImplementedError(f"{self.adapter_name} has no chat of its own")

    def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Streaming chat turn. An override yields dicts of shape
        { "type": "token", "token": str } and ends with exactly one
        { "done": True, "native_session_id": str | None }.
        """
        raise NotImplementedError(f"{self.adapter_name} has no chat of its own")

    @classmethod
    def chats(cls) -> bool:
        """Whether this adapter streams a chat turn itself (it overrides
        ``stream``), which is what ``AgentDispatcher`` runs."""
        return cls.stream is not BaseAgentAdapter.stream

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

