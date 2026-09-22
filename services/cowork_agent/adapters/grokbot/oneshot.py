"""One-shot / send-and-wait loops over the Grok Bot gateway.

The host has no ``waitForCompletion`` API. After ``sendPrompt`` (which only
returns ``{accepted: true}``) we poll roster + tasks + subagents until idle,
then read the last assistant line from the transcript tail.

Space never broadcasts. A durable seat is used when ``session_id`` or
``GROKBOT_DEFAULT_AGENT_ID`` names one; otherwise a throwaway seat is minted
and kept so follow-up turns can continue.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from services.cowork_agent.adapters.grokbot.gateway import (
    GrokbotGateway,
    GrokbotGatewayError,
)
from services.cowork_agent.adapters.grokbot.paths import (
    default_agent_id,
    is_valid_sand_agent_id,
)
from services.cowork_agent.adapters.grokbot.session_seats import (
    lookup_seat,
    remember_seat,
)

DEFAULT_WAIT_INTERVAL_S = 0.25
DEFAULT_WAIT_TIMEOUT_S = 600.0
CREATE_NAME_PREFIX = "xo-space-"


def _refuse_broadcast(agent_id: str) -> None:
    if agent_id.strip().lower() == "all":
        raise GrokbotGatewayError(
            "Refusing agent id 'all' — Space never broadcasts to all host agents."
        )


def resolve_target_agent(
    session_id: str | None,
    *,
    is_new_session: bool = False,
) -> tuple[str | None, bool]:
    """Return ``(agent_id, mint_throwaway)``.

    Space IDs are UUIDs, not host seats. Resolution order:

    1. remembered seat for this Space session
    2. ``session_id`` that already owns a sand-data transcript (Sessions tab)
    3. ``GROKBOT_DEFAULT_AGENT_ID`` on a brand-new Space chat
    4. mint a throwaway seat (never broadcast)
    """
    if session_id and session_id.strip():
        sid = session_id.strip()
        _refuse_broadcast(sid)
        mapped = lookup_seat(sid)
        if mapped:
            _refuse_broadcast(mapped)
            return mapped, False
        if not is_new_session:
            from services.cowork_agent.adapters.grokbot.sessions import owns_session

            if is_valid_sand_agent_id(sid) and owns_session(sid):
                return sid, False
    if is_new_session or not session_id:
        configured = default_agent_id()
        if configured:
            _refuse_broadcast(configured)
            return configured, False
    return None, True


def _entries_from_transcript(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        return payload["entries"]
    return []


def _unwrap_entry(value: Any) -> Any:
    if isinstance(value, dict) and value.get("kind") is None and "entry" in value:
        return value.get("entry")
    return value


def _as_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def last_assistant_text(entries: list[Any]) -> str | None:
    """Last assistant / send-message text from host transcript rows or JSONL."""
    for raw in reversed(entries):
        entry = _unwrap_entry(raw)
        if not isinstance(entry, dict):
            continue
        if entry.get("streaming") is True:
            continue
        kind = entry.get("kind")
        role = entry.get("role")
        if kind == "send-message":
            message = entry.get("message")
            if isinstance(message, dict) and message.get("type") == "text":
                text = _as_text(message.get("content"))
                if text:
                    return text
            continue
        if kind == "message" and role == "assistant":
            text = _as_text(entry.get("content"))
            if text:
                return text
            continue
        if kind is None and role == "assistant":
            text = _as_text(entry.get("content"))
            if text:
                return text
            message = entry.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    texts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    joined = "".join(texts).strip()
                    if joined:
                        return joined
    return None


def _find_agent(agents: list[dict[str, Any]], agent_id: str) -> dict[str, Any] | None:
    for row in agents:
        if row.get("id") == agent_id:
            return row
    return None


def _running_subagent_count(subagents: list[Any]) -> int:
    return sum(
        1
        for row in subagents
        if isinstance(row, dict) and row.get("status") == "running"
    )


async def wait_for_idle(
    gateway: GrokbotGateway,
    agent_id: str,
    *,
    client_nonce: str | None = None,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    interval_s: float = DEFAULT_WAIT_INTERVAL_S,
) -> str:
    """Poll until the seat is idle. Returns ``idle`` / ``awaiting-user`` / ``timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        agents, tasks, subagents = await asyncio.gather(
            gateway.list_agents(),
            gateway.get_async_tasks(agent_id),
            gateway.get_subagents(agent_id),
        )
        acceptance: dict[str, Any] | None = None
        if client_nonce:
            try:
                acceptance = await gateway.prompt_acceptance_status(client_nonce)
            except GrokbotGatewayError:
                acceptance = None

        agent = _find_agent(agents, agent_id)
        if agent is None:
            raise GrokbotGatewayError(
                f"Grok Bot agent {agent_id!r} disappeared from the roster while waiting."
            )

        if (
            isinstance(acceptance, dict)
            and acceptance.get("outcome") == "found"
            and isinstance(acceptance.get("record"), dict)
            and acceptance["record"].get("status") == "rejected"
        ):
            raise GrokbotGatewayError("Grok Bot host rejected the prompt.")

        acceptance_blocking = isinstance(acceptance, dict) and (
            acceptance.get("outcome") == "not-found"
            or (
                acceptance.get("outcome") == "found"
                and isinstance(acceptance.get("record"), dict)
                and acceptance["record"].get("status") == "pending"
            )
        )
        busy = bool(
            agent.get("isRunning")
            or agent.get("isComposingMessage")
            or tasks
            or _running_subagent_count(subagents)
            or acceptance_blocking
        )
        awaiting = agent.get("awaitingUserResponse")
        if not busy and awaiting not in (None, False):
            return "awaiting-user"
        if not busy:
            return "idle"
        remaining = max(0.0, deadline - loop.time())
        if remaining <= 0:
            return "timeout"
        await asyncio.sleep(min(interval_s, remaining))
        if loop.time() >= deadline:
            return "timeout"


async def _read_reply(gateway: GrokbotGateway, agent_id: str) -> str:
    try:
        tail = await gateway.get_agent_transcript_tail(agent_id, limit=50)
        text = last_assistant_text(_entries_from_transcript(tail))
        if text:
            return text
    except GrokbotGatewayError:
        pass
    try:
        full = await gateway.get_agent_transcript(agent_id)
        text = last_assistant_text(_entries_from_transcript(full))
        if text:
            return text
    except GrokbotGatewayError:
        pass
    return ""


async def run_turn(
    question: str,
    session_id: str | None = None,
    *,
    is_new_session: bool = False,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
) -> dict[str, str | None]:
    """Send one Space turn. Returns ``message`` + ``native_session_id``."""
    gateway = GrokbotGateway()
    gateway.require_token()

    agent_id, mint = resolve_target_agent(session_id, is_new_session=is_new_session)
    if mint:
        created = await gateway.create_agent(
            name=f"{CREATE_NAME_PREFIX}{uuid.uuid4().hex[:12]}",
            description="XO Space chat turn",
        )
        agent_id = str(created.get("id") or "")
        if not agent_id:
            raise GrokbotGatewayError("Grok Bot createAgent did not return an agent id.")

    assert agent_id is not None
    remember_seat(session_id, agent_id)
    nonce = str(uuid.uuid4())
    sent = await gateway.send_prompt(prompt=question, agent_id=agent_id, client_nonce=nonce)
    if sent.get("accepted") is not True:
        raise GrokbotGatewayError("Grok Bot host did not accept the prompt.")

    status = await wait_for_idle(
        gateway, agent_id, client_nonce=nonce, timeout_s=timeout_s
    )
    reply = await _read_reply(gateway, agent_id)
    if not reply and status == "timeout":
        raise GrokbotGatewayError(
            f"Grok Bot host did not finish the turn within {int(timeout_s)}s."
        )
    if not reply and status == "awaiting-user":
        reply = "The Grok Bot host is waiting for user input on that seat."
    return {"message": reply, "native_session_id": agent_id}
