"""One-shot / send-and-wait loops over the Grok Bot gateway.

The host has no ``waitForCompletion`` API. After ``sendPrompt`` (which only
returns ``{accepted: true}``) we poll roster + tasks + subagents until idle,
then accept only assistant text following this turn's newly recorded prompt.

Space never broadcasts. A durable seat is used when ``session_id`` or
``GROKBOT_DEFAULT_AGENT_ID`` names one; otherwise a session seat is minted
and retained for follow-ups (only standalone one-shot seats are deleted).
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
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

DEFAULT_WAIT_INTERVAL_S = 1.0
MAX_WAIT_INTERVAL_S = 5.0
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


def _current_turn_entries(
    before: list[Any], entries: list[Any], question: str,
) -> list[Any] | None:
    """Fence replies by the pre-send transcript AND the new user prompt.

    Fail closed if the host rewrote/truncated history: array positions are
    only safe while the snapshot remains a prefix. Repeated answer text is
    valid; a repeated old answer entry is not.
    """
    if len(entries) < len(before) or entries[:len(before)] != before:
        raise GrokbotGatewayError(
            "Grok Bot transcript changed while waiting; cannot identify this turn's reply."
        )
    start = None
    for index, raw in enumerate(entries[len(before):], start=len(before)):
        entry = _unwrap_entry(raw)
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else entry.get("content")
        if isinstance(content, list):
            content = "".join(
                str(block.get("text") or "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue  # User-envelope tool results are not a new prompt.
        if start is not None:
            # Another prompt has arrived on this seat; don't attribute its
            # answer to our turn (especially on a shared default seat).
            return entries[start:index]
        if content.strip() == question.strip():
            start = index + 1
    return entries[start:] if start is not None else None


async def _transcript(gateway: GrokbotGateway, agent_id: str) -> list[Any]:
    payload = await gateway.get_agent_transcript(agent_id)
    if not isinstance(payload, list) and not (
        isinstance(payload, dict) and isinstance(payload.get("entries"), list)
    ):
        raise GrokbotGatewayError("Grok Bot host returned an invalid transcript.")
    return _entries_from_transcript(payload)


async def wait_for_idle(
    gateway: GrokbotGateway,
    agent_id: str,
    *,
    before: list[Any],
    question: str,
    client_nonce: str,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    interval_s: float = DEFAULT_WAIT_INTERVAL_S,
) -> tuple[str, str]:
    """Wait for this prompt's reply, not merely an idle roster observation."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        agents, tasks, subagents = await asyncio.gather(
            gateway.list_agents(),
            gateway.get_async_tasks(agent_id),
            gateway.get_subagents(agent_id),
        )
        acceptance: dict[str, Any] | None = None
        try:
            acceptance = await gateway.prompt_acceptance_status(client_nonce)
        except GrokbotGatewayError:
            pass  # The newly appended prompt/reply must still prove freshness.

        agent = _find_agent(agents, agent_id)
        if agent is None:
            raise GrokbotGatewayError(
                f"Grok Bot agent {agent_id!r} disappeared from the roster while waiting."
            )

        record = acceptance.get("record") if isinstance(acceptance, dict) else None
        if isinstance(record, dict) and record.get("status") == "rejected":
            raise GrokbotGatewayError("Grok Bot host rejected the prompt.")
        acceptance_blocking = isinstance(acceptance, dict) and (
            acceptance.get("outcome") == "not-found"
            or (isinstance(record, dict) and record.get("status") == "pending")
        )
        busy = bool(
            agent.get("isRunning")
            or agent.get("isComposingMessage")
            or tasks
            or _running_subagent_count(subagents)
            or acceptance_blocking
        )
        if not busy:
            entries = await _transcript(gateway, agent_id)
            current = _current_turn_entries(before, entries, question)
            reply = last_assistant_text(current or []) or ""
            if loop.time() >= deadline:
                return "timeout", ""
            if reply:
                return "idle", reply
            if current is not None and agent.get("awaitingUserResponse") not in (None, False):
                return "awaiting-user", "The Grok Bot host is waiting for user input on that seat."
        remaining = max(0.0, deadline - loop.time())
        await asyncio.sleep(min(interval_s, remaining))
        interval_s = min(interval_s * 1.5, MAX_WAIT_INTERVAL_S)
    return "timeout", ""


async def run_turn(
    question: str,
    session_id: str | None = None,
    *,
    is_new_session: bool = False,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
) -> dict[str, str | None]:
    """Send one Space turn, retaining its host seat for future follow-ups."""
    async with GrokbotGateway() as gateway:
        gateway.require_token()
        agent_id, mint = resolve_target_agent(session_id, is_new_session=is_new_session)
        if mint:
            created = await gateway.create_agent(
                name=f"{CREATE_NAME_PREFIX}{uuid.uuid4().hex[:12]}",
                description="XO Space chat session",
            )
            agent_id = str(created.get("id") or "")
            if not agent_id:
                raise GrokbotGatewayError("Grok Bot createAgent did not return an agent id.")

        assert agent_id is not None
        retained = False
        prompt_sent = False
        try:
            # Persist before sending: even a disconnect must leave a session
            # that core can own and reopen under the original Space UUID.
            remember_seat(session_id, agent_id)
            retained = bool(session_id)
            async with asyncio.timeout(timeout_s):
                before = await _transcript(gateway, agent_id)
                nonce = str(uuid.uuid4())
                prompt_sent = True
                sent = await gateway.send_prompt(prompt=question, agent_id=agent_id, client_nonce=nonce)
                if sent.get("accepted") is not True:
                    prompt_sent = False
                    raise GrokbotGatewayError("Grok Bot host did not accept the prompt.")
                status, reply = await wait_for_idle(
                    gateway, agent_id, before=before, question=question,
                    client_nonce=nonce, timeout_s=timeout_s,
                )
                if status == "timeout":
                    raise TimeoutError
                return {"message": reply, "native_session_id": agent_id}
        except (asyncio.CancelledError, TimeoutError) as exc:
            if prompt_sent:
                # The host may not support interruption or may be unreachable.
                # Bound cleanup and preserve cancellation/the timeout error.
                with suppress(GrokbotGatewayError, TimeoutError):
                    async with asyncio.timeout(5):
                        await gateway.interrupt_agent(agent_id)
            if isinstance(exc, asyncio.CancelledError):
                raise
            hint = (
                "An interrupt was attempted; check the host if it is still running."
                if prompt_sent else "The prompt was not sent."
            )
            raise GrokbotGatewayError(
                f"Grok Bot host did not finish the turn within {timeout_s:g}s. {hint}"
            ) from exc
        finally:
            if mint and not retained:
                # No Space session can resume a standalone one-shot seat.
                with suppress(GrokbotGatewayError, TimeoutError):
                    async with asyncio.timeout(5):
                        await gateway.delete_agent(agent_id)
