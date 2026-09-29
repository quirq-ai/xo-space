"""Grok Bot Plane-B adapter — chat through a running local host gateway."""
from __future__ import annotations

from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter


class GrokbotAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "grokbot"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from services.cowork_agent.adapters.grokbot.oneshot import run_turn

        space_session_id = session_id or kwargs.get("our_session_id")
        return await run_turn(
            question,
            space_session_id,
            is_new_session=bool(kwargs.get("is_new_session", not session_id)),
            timeout_s=float(self.config.get("timeout", 600)),
        )

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from services.cowork_agent.adapters.grokbot.gateway import GrokbotGatewayError
        from services.cowork_agent.adapters.grokbot.oneshot import run_turn

        space_session_id = session_id or kwargs.get("our_session_id")
        is_new = bool(kwargs.get("is_new_session", not session_id))
        try:
            yield {"type": "model-loading", "label": "Waiting for Grok Bot host"}
            result = await run_turn(
                question,
                space_session_id,
                is_new_session=is_new,
                timeout_s=float(self.config.get("timeout", 600)),
            )
        except GrokbotGatewayError as exc:
            yield {"type": "error", "error": str(exc)}
            yield {"done": True, "native_session_id": None}
            return
        except Exception as exc:
            yield {"type": "error", "error": str(exc)}
            yield {"done": True, "native_session_id": None}
            return

        native_id = result.get("native_session_id")
        if is_new and native_id:
            yield {"type": "session-id-resolved", "session_id": native_id}
        message = result.get("message") or ""
        if message:
            yield {"type": "token", "token": message}
        yield {"done": True, "native_session_id": native_id}

    async def setup(self) -> bool:
        """The Grok Bot host is an external process; nothing to start here."""
        return True

    async def health(self) -> dict[str, Any]:
        """Unauthenticated GET /health. Missing token is reported, not hidden."""
        from services.cowork_agent.adapters.grokbot.gateway import (
            GrokbotGateway,
            GrokbotGatewayError,
        )

        try:
            gateway = GrokbotGateway()
            payload = await gateway.health()
            ok = bool(payload.get("ok"))
            info: dict[str, Any] = {
                "ok": ok,
                "gateway": "up" if ok else "unhealthy",
                "url": gateway.base_url,
                "has_token": gateway.discovery.has_token,
            }
            if payload.get("activeAgentId") is not None:
                info["active_agent_id"] = payload.get("activeAgentId")
            if payload.get("isBusy") is not None:
                info["busy"] = payload.get("isBusy")
            if not gateway.discovery.has_token:
                info["token"] = "missing"
            return info
        except GrokbotGatewayError as exc:
            return {"ok": False, "gateway": str(exc)}
        except Exception as exc:
            return {"ok": False, "gateway": str(exc)}


# Stable discovery alias — the dynamic loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter.
Adapter = GrokbotAdapter
