"""One-shot / send-and-wait loops over the Grok Bot gateway.

The host has no ``waitForCompletion`` API. After ``sendPrompt`` (which only
returns ``{accepted: true}``) we poll roster + tasks + subagents until idle,
then accept only replies linked to this turn by clientNonce and requestId.

Space never broadcasts. A durable seat is used when ``session_id`` or
``GROKBOT_DEFAULT_AGENT_ID`` names one; otherwise a session seat is minted
and retained for follow-ups (only standalone one-shot seats are deleted).
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

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
from services.cowork_agent.adapters.grokbot.transcript import current_turn_reply, transcript_entries

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
    2. a host seat already recorded in the Space session index
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


async def wait_for_idle(
    gateway: GrokbotGateway,
    agent_id: str,
    *,
    client_nonce: str,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    interval_s: float = DEFAULT_WAIT_INTERVAL_S,
) -> tuple[str, str]:
    """Wait for this prompt's reply, not merely an idle roster observation."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    request_id = None
    while loop.time() < deadline:
        agents, tasks, subagents = await asyncio.gather(
            gateway.list_agents(),
            gateway.get_async_tasks(agent_id),
            gateway.get_subagents(agent_id),
        )
        acceptance = None
        try:
            acceptance = await gateway.prompt_acceptance_status(client_nonce)
        except GrokbotGatewayError:
            pass  # Transcript correlation must still prove which turn replied.

        agent = next((row for row in agents if row.get("id") == agent_id), None)
        if agent is None:
            raise GrokbotGatewayError(
                f"Grok Bot agent {agent_id!r} disappeared from the roster while waiting."
            )

        record = acceptance.get("record") if isinstance(acceptance, dict) else None
        if isinstance(record, dict) and record.get("status") == "rejected":
            raise GrokbotGatewayError("Grok Bot host rejected the prompt.")
        busy = bool(
            agent.get("isRunning")
            or agent.get("isComposingMessage")
            or tasks
            or any(isinstance(row, dict) and row.get("status") == "running" for row in subagents)
        )
        # Read while busy too: the prompt may leave the host's in-memory
        # window before the run finishes, so retain its request ID.
        entries = transcript_entries(await gateway.get_agent_transcript(agent_id))
        request_id, reply = current_turn_reply(entries, client_nonce, request_id)
        if not busy and reply:
            return "idle", reply  # Include a reply found on the deadline tick.
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

        if not agent_id:
            raise GrokbotGatewayError("Grok Bot could not resolve a target agent id.")
        retained = False
        prompt_sent = False
        try:
            # Persist before sending: even a disconnect must leave a session
            # that core can own and reopen under the original Space UUID.
            remember_seat(session_id, agent_id, question=question)
            retained = bool(session_id)
            async with asyncio.timeout(timeout_s):
                nonce = str(uuid.uuid4())
                prompt_sent = True
                sent = await gateway.send_prompt(prompt=question, agent_id=agent_id, client_nonce=nonce)
                if sent.get("accepted") is not True:
                    prompt_sent = False
                    raise GrokbotGatewayError("Grok Bot host did not accept the prompt.")
                status, reply = await wait_for_idle(
                    gateway, agent_id,
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
                f"Grok Bot host did not finish the turn within {timeout_s:g}s with an idle seat "
                f"and a reply matching clientNonce/requestId. {hint}"
            ) from exc
        finally:
            if mint and not retained:
                # No Space session can resume a standalone one-shot seat.
                with suppress(GrokbotGatewayError, TimeoutError):
                    async with asyncio.timeout(5):
                        await gateway.delete_agent(agent_id)
