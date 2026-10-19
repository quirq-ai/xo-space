"""A ``SessionSpec`` as the store keeps it (``sessions.spec``), and back.

The spec is kept so the engine can attach a process again after an idle
release or a restart. It must hold no secret values: a preset never puts one
there (the comms token is minted at attach, chapter 12).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Mapping

from services.cowork_agent.adapters.contract import (
    AgentDef,
    CliLimits,
    HostTool,
    McpServer,
    PermissionPolicy,
    SessionSpec,
    SystemPrompt,
    ToolPolicy,
)


def spec_to_json(spec: SessionSpec) -> str:
    return json.dumps(dataclasses.asdict(spec), sort_keys=True)


def spec_from_json(text: str) -> SessionSpec:
    raw: dict[str, Any] = json.loads(text)
    nested = {
        "system_prompt": lambda v: SystemPrompt(**v) if v is not None else None,
        "tools": lambda v: ToolPolicy(**_tuples(v)),
        "permissions": lambda v: PermissionPolicy(**_tuples(v)),
        "limits": lambda v: CliLimits(**v),
        "mcp_servers": lambda v: tuple(McpServer(**_tuples(s)) for s in v),
        "host_tools": lambda v: tuple(HostTool(**s) for s in v),
        "agents": lambda v: {k: AgentDef(**{**a, "tools": ToolPolicy(**_tuples(a["tools"])) if a.get("tools")
                                             else None}) for k, a in v.items()},
    }
    fields = {f.name for f in dataclasses.fields(SessionSpec)}
    kwargs = {}
    for key, value in raw.items():
        if key not in fields:
            continue                    # a field a later contract dropped
        kwargs[key] = nested[key](value) if key in nested else (tuple(value) if isinstance(value, list) else value)
    return SessionSpec(**kwargs)


def _tuples(obj: Mapping[str, Any]) -> dict[str, Any]:
    """JSON lists back into the tuples the frozen dataclasses hold."""
    return {k: tuple(v) if isinstance(v, list) else v for k, v in obj.items()}
