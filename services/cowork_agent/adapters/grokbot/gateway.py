"""Thin async HTTP client for a running Grok Bot host gateway.

Reimplements the community SDK's transport only: ``GET /health`` (no auth)
and ``POST /api/<command>`` with a Bearer token. Tokens are never logged.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from services.cowork_agent.adapters.grokbot.paths import (
    GatewayDiscovery,
    discover_gateway,
    redact_secret,
)

HEALTH_PATH = "/health"
API_PREFIX = "/api"
REQUEST_ID_HEADER = "x-sand-request-id"

MISSING_TOKEN_HINT = (
    "Grok Bot gateway token is missing. Set SAND_GATEWAY_TOKEN in Setup "
    "secrets, or copy it from sand-data/gateway.json (also accepted as "
    "agent-data/gateway.json)."
)
UNREACHABLE_HINT = (
    "Grok Bot gateway is unreachable at {url}. The host must be running "
    "(typical http://127.0.0.1:1340). Check GROKBOT_GATEWAY_URL / "
    "SAND_GATEWAY_URL and that GET /health responds."
)


class GrokbotGatewayError(RuntimeError):
    """Host gateway call failed. Message never includes the token."""


def _headers(token: str | None, *, auth: bool) -> dict[str, str]:
    headers = {REQUEST_ID_HEADER: str(uuid.uuid4())}
    if auth:
        if not token:
            raise GrokbotGatewayError(MISSING_TOKEN_HINT)
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _raise_http(command: str, status: int, body: str, token: str | None) -> None:
    snippet = redact_secret(body[:500], token)
    raise GrokbotGatewayError(
        f"Grok Bot gateway {command} failed: HTTP {status} {snippet}".strip()
    )


class GrokbotGateway:
    """One discovery snapshot + httpx helpers."""

    def __init__(self, discovery: GatewayDiscovery | None = None):
        self.discovery = discovery or discover_gateway()

    @property
    def base_url(self) -> str:
        return self.discovery.base_url.rstrip("/")

    @property
    def token(self) -> str | None:
        return self.discovery.token

    def require_token(self) -> str:
        if not self.discovery.has_token or not self.token:
            raise GrokbotGatewayError(MISSING_TOKEN_HINT)
        return self.token

    async def health(self, *, timeout: float = 5.0) -> dict[str, Any]:
        url = f"{self.base_url}{HEALTH_PATH}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0)) as client:
                resp = await client.get(url, headers=_headers(None, auth=False))
        except httpx.HTTPError as exc:
            raise GrokbotGatewayError(
                UNREACHABLE_HINT.format(url=self.base_url)
            ) from exc
        if resp.status_code >= 500:
            _raise_http("health", resp.status_code, resp.text, None)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"raw": payload}
        payload.setdefault("ok", resp.status_code < 400)
        return payload

    async def command(
        self,
        name: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        if name == "broadcastToAgents":
            raise GrokbotGatewayError(
                "Refusing broadcastToAgents — Space never broadcasts to all host agents."
            )
        token = self.require_token()
        url = f"{self.base_url}{API_PREFIX}/{name}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                resp = await client.post(
                    url,
                    headers={**_headers(token, auth=True), "Content-Type": "application/json"},
                    json=body if body is not None else {},
                )
        except httpx.HTTPError as exc:
            raise GrokbotGatewayError(
                redact_secret(UNREACHABLE_HINT.format(url=self.base_url), token)
            ) from exc
        if resp.status_code >= 400:
            _raise_http(name, resp.status_code, resp.text, token)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise GrokbotGatewayError(
                redact_secret(f"Grok Bot gateway {name} returned non-JSON", token)
            ) from exc

    async def list_agents(self) -> list[dict[str, Any]]:
        payload = await self.command("listAgents", {})
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            rows = payload.get("agents") or payload.get("items") or []
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    async def create_agent(self, *, name: str, description: str = "") -> dict[str, Any]:
        payload = await self.command(
            "createAgent",
            {
                "name": name,
                "description": description,
                "isIntroductionSuppressed": True,
            },
        )
        if isinstance(payload, dict):
            agent = payload.get("agent")
            if isinstance(agent, dict) and agent.get("id"):
                return agent
            if payload.get("id"):
                return payload
        raise GrokbotGatewayError("Grok Bot createAgent did not return an agent id.")

    async def send_prompt(self, *, prompt: str, agent_id: str, client_nonce: str) -> dict[str, Any]:
        if not agent_id or agent_id.strip().lower() == "all":
            raise GrokbotGatewayError(
                "Refusing to send a prompt without a specific agent id "
                "(will not broadcast to all agents)."
            )
        payload = await self.command(
            "sendPrompt",
            {"prompt": prompt, "agentId": agent_id, "clientNonce": client_nonce},
        )
        if not isinstance(payload, dict):
            return {"accepted": False}
        return payload

    async def get_async_tasks(self, agent_id: str) -> list[Any]:
        payload = await self.command("getAsyncTasks", {"id": agent_id})
        return payload if isinstance(payload, list) else []

    async def get_subagents(self, agent_id: str) -> list[Any]:
        payload = await self.command("getSubagents", {"id": agent_id})
        return payload if isinstance(payload, list) else []

    async def prompt_acceptance_status(self, client_nonce: str) -> dict[str, Any] | None:
        payload = await self.command(
            "promptAcceptanceStatus",
            {"accountSlot": "host", "clientNonce": client_nonce},
        )
        return payload if isinstance(payload, dict) else None

    async def get_agent_transcript_tail(self, agent_id: str, *, limit: int = 50) -> Any:
        return await self.command("getAgentTranscriptTail", {"id": agent_id, "limit": limit})

    async def get_agent_transcript(self, agent_id: str) -> Any:
        return await self.command("getAgentTranscript", {"id": agent_id})

    async def delete_agent(self, agent_id: str) -> Any:
        return await self.command("deleteAgent", {"id": agent_id})
