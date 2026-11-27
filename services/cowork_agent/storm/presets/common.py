"""What every preset shares (design ch 10 §2) and the rules that read a
runtime's feature list instead of its name (ch 10 §5; §0.70, §0.90).

Everything here is pure: no I/O, no clock. A preset never compares
``ctx.agent`` to a name; where roles differ between CLIs it reads the
``FeatureList``.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal, Mapping

from services.cowork_agent.adapters.contract import (
    Effort,
    FeatureList,
    FeatureSupport,
    McpServer,
    PermissionMode,
    SessionSpec,
    SystemPrompt,
)
from services.errors import ServiceError

if TYPE_CHECKING:
    from services.cowork_agent.storm.personas import Persona

PresetName = Literal["plain_chat", "workspace_coordinator", "project_coordinator", "thread", "job", "try"]

#: The comms role each preset's session speaks as (ch 12 §3); also the
#: engine's ``sessions.role``.
ROLES: Mapping[str, str] = {
    "plain_chat": "plain", "workspace_coordinator": "workspace-coordinator",
    "project_coordinator": "project-coordinator", "thread": "thread", "job": "thread", "try": "thread",
}

#: Shared defaults (ch 10 §2): the person's own settings and MCP servers load
#: (isolation ``user``), thinking streams as a summary, sub-agents' steps reach
#: the presenter, transcripts stay where each CLI writes them, nothing is
#: limited (§0.17), and a session never forks its parent's context.
SHARED: Mapping[str, Any] = {"isolation": "user", "thinking": "summary", "runs_detail": "steps", "persist": True,
                             "fork": False, "strict": False}


@dataclass(frozen=True)
class Intelligence:
    """The conversation's model and effort (ch 10 §3.3), already checked
    against the runtime's catalog by the caller. None: the preset's default."""

    model: str | None = None
    effort: Effort | None = None
    takes_effort: bool = True       # False: the model has no effort setting, so none is passed (not the default)


@dataclass(frozen=True)
class PresetContext:
    """What a surface hands a preset. The caller resolves everything that
    needs I/O first: the folder, the comms mount (its token is minted at
    attach, never here), the pins."""

    preset: PresetName
    agent: str                              # data passed through; never branched on
    cwd: str
    project: str | None = None              # None: the workspace
    intelligence: Intelligence = Intelligence()
    comms: McpServer | None = None
    request: Mapping[str, Any] = field(default_factory=dict)   # plain chat's /api/chat/prompt settings
    persona: Persona | None = None          # the agent the run works as (``storm.personas``), already resolved


@dataclass(frozen=True)
class PresetResult:
    spec: SessionSpec
    role: str
    requires: tuple[str, ...]               # features that must not rest on the prompt or on nothing (ch 10 §9)
    fingerprint: str                        # moves when the role's prompt or settings move (ch 10 §7.4)


class PresetRefused(ServiceError):
    """A preset can't build a spec from this context on this runtime."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status=400)


def offered(features: FeatureList, key: str) -> FeatureSupport:
    return features.features.get(key, FeatureSupport(support="unsupported"))


def thread_mode(features: FeatureList) -> PermissionMode:
    """A thread (and a job run) runs in ``auto`` where the runtime offers it:
    natively (the CLI's own safety check decides), or emulated (XO's policy
    decides without asking the person, §0.122); elsewhere in
    ``accept_edits``, where XO's policy answers what the CLI asks (§0.90,
    superseding ch 10 §5 for threads). Read from the feature list, so a
    runtime that gains an auto switches by itself."""
    return "auto" if offered(features, "permissions.auto").support in ("native", "emulated") else "accept_edits"


def default_effort(features: FeatureList, wanted: Effort) -> Effort | None:
    """The preset's effort when the runtime offers it, else the CLI's own."""
    return wanted if wanted in offered(features, "effort").values else None


def intended_effort(intelligence: Intelligence, features: FeatureList, wanted: Effort) -> Effort | None:
    """The conversation's effort, else the preset's (``default_effort``);
    none for a model that takes no effort."""
    if not intelligence.takes_effort:
        return None
    return intelligence.effort or default_effort(features, wanted)


@functools.cache
def role_prompt(name: str) -> SystemPrompt:
    """A core-owned role prompt (ch 10 §7.1), appended to the CLI's own.
    Read once per process: the files change only with an upgrade, which
    restarts it, and a coordinator's preset is built on every message."""
    text = resources.files(__package__).joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")
    return SystemPrompt(text=text.strip(), mode="append")


def fingerprint(spec: SessionSpec) -> str:
    """The hash of what defines a role (ch 10 §7.4): its prompt, its tool and
    permission fields, its orchestration and the comms servers' role-bound
    parts. Not the folder, model, effort or a server's address and token, so
    a pin or a moved project never recycles a coordinator. Computed from the
    spec alone, so a stored spec compares with a freshly built one."""
    servers = [{"name": s.name, "transport": s.transport, "auto_approve": s.auto_approve,
                "terminal_tools": list(s.terminal_tools)} for s in spec.mcp_servers]
    defining = {
        "system_prompt": dataclasses.asdict(spec.system_prompt) if spec.system_prompt else None,
        "tools": dataclasses.asdict(spec.tools), "permissions": dataclasses.asdict(spec.permissions),
        "orchestration": spec.orchestration, "mcp_servers": servers,
    }
    return hashlib.sha256(json.dumps(defining, sort_keys=True).encode()).hexdigest()[:16]


#: The tool words a persona's ``tools`` narrows: each one its list leaves out
#: is denied. Reading and comms are never taken away (a run must be heard).
#: ``delete`` isn't one: a runtime with no delete tool deletes only through
#: its shell, so it refuses that deny (D9, 11-27); a file may still name it
#: in ``disallowedTools``.
NARROWED = ("edit", "create", "shell", "web_fetch", "web_search")


def with_persona(spec: SessionSpec, persona: Persona | None) -> SessionSpec:
    """``spec`` working as ``persona`` (plan B3): its instructions follow the
    role's prompt (D6), and its tools only narrow the preset's: what
    ``disallowed_tools`` names, and with a ``tools`` list every word of
    ``NARROWED`` it leaves out, is denied. A runtime that can't enforce a
    deny refuses the session through its check, never runs wider. Its
    runtime, model and effort are the caller's defaults, not set here."""
    if persona is None:
        return spec
    role = spec.system_prompt.text if spec.system_prompt else ""
    section = f"# You work as the agent “{persona.name}”\n\n{persona.body.strip()}" if persona.body.strip() else ""
    prompt = SystemPrompt(text="\n\n".join(p for p in (role, section) if p), mode="append") if section else \
        spec.system_prompt
    listed = tuple(persona.tools or ())
    left_out = tuple(w for w in NARROWED if w not in listed) if listed else ()
    deny = tuple(dict.fromkeys((*spec.tools.deny, *(persona.disallowed_tools or ()), *left_out)))
    return dataclasses.replace(spec, system_prompt=prompt, tools=dataclasses.replace(spec.tools, deny=deny))


def persona_record(persona: Persona) -> dict[str, Any]:
    """What a run's root message keeps of its agent (``extra.persona``)."""
    return {"name": persona.name, "level": persona.level, "sha256": persona.version}
