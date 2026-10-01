"""``BRAIN_MODEL=agent`` / ``agent:<name>``: reason with an agent harness.

``agent`` is the Space's active agent (``AGENT_NAME``); ``agent:<name>`` is
any installed one (a folder under ``config/agents/``), so the brain can
reason with a different harness than the one you chat with. Both go through
the same dispatcher chat uses, with that agent's own login and model; no
harness is named here. The cost of needing no setup: every call is a full
agent turn (for a CLI agent, a process start), and each one is recorded among
that agent's sessions. Fine for the few calls reasoning needs (answers,
designs, naming); for extraction over a large project, connect a direct
model (see ``services/brain/model/__init__.py``).
"""

from __future__ import annotations

from typing import Optional

from services.brain.model import BrainModel, ModelUnavailable


def installed_agents() -> list[str]:
    from services.cowork_agent.registry.agent_registry import all_agents

    return sorted(m.name for m in all_agents())


def resolve_agent(name: Optional[str]) -> str:
    """``name`` when it is an installed agent, the active agent when it is
    empty; an unknown name raises with the ones that exist."""
    from services.cowork_agent.registry.agent_registry import get_active_agent

    if not name:
        return get_active_agent().name
    installed = installed_agents()
    if name not in installed:
        raise ValueError(f"no agent named {name!r}; installed: {', '.join(installed)}")
    return name


async def ask_agent(agent: str, prompt: str, **kwargs) -> str:
    from services.cowork_agent.engine.dispatcher import AgentDispatcher

    result = await AgentDispatcher(agent).ask(prompt, None, **kwargs)
    message = result.get("message") if isinstance(result, dict) else None
    return message if isinstance(message, str) else ""


class Model(BrainModel):
    def __init__(self, agent: Optional[str] = None) -> None:
        self.agent = resolve_agent(agent) if agent else None   # None: whichever agent is active
        self.name = f"agent:{self.agent}" if self.agent else "agent"

    def agent_name(self) -> str:
        return self.agent or resolve_agent(None)

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        question = f"{system.strip()}\n\n{prompt}" if system.strip() else prompt
        agent = self.agent_name()
        try:
            message = await ask_agent(agent, question)
        except Exception as exc:  # noqa: BLE001 - surfaced as "no model" with the cause
            raise ModelUnavailable(f"The agent {agent} could not answer: {type(exc).__name__}: {exc}") from exc
        if not message.strip():
            raise ModelUnavailable(f"The agent {agent} returned no text.")
        return message

    def describe(self) -> dict:
        info = super().describe()
        try:
            info["agent"] = self.agent_name()
        except Exception:  # noqa: BLE001 - the registry is unavailable: say nothing rather than guess
            pass
        return info
