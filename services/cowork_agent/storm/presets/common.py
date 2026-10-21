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
from typing import Any, Literal, Mapping, get_args

from services.cowork_agent.adapters.contract import (
    Effort,
    FeatureList,
    FeatureSupport,
    McpServer,
    ModelInfo,
    PermissionMode,
    SessionSpec,
    SystemPrompt,
)
from services.errors import ServiceError

PresetName = Literal["plain_chat", "workspace_coordinator", "project_coordinator", "thread", "job"]

#: The comms role each preset's session speaks as (ch 12 §3); also the
#: engine's ``sessions.role``.
ROLES: Mapping[str, str] = {
    "plain_chat": "plain", "workspace_coordinator": "workspace-coordinator",
    "project_coordinator": "project-coordinator", "thread": "thread", "job": "thread",
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


#: Every effort a runtime may offer, weakest first (the contract's ``Effort``).
EFFORTS: tuple[Effort, ...] = get_args(Effort)


@dataclass(frozen=True)
class Catalog:
    """An agent's models and the efforts each offers (ch 10 §3.3): what a
    pin, and the router's choice of a new thread's model and effort, are
    checked against. A model that lists no efforts of its own, or no model
    named, offers the efforts of the runtime's feature list."""

    agent: str
    models: tuple[ModelInfo, ...]
    feature_efforts: tuple[Effort, ...]

    @classmethod
    def of(cls, agent: str, models: list[ModelInfo], features: FeatureList) -> "Catalog":
        return cls(agent, tuple(models), offered(features, "effort").values)

    def efforts(self, model: str | None) -> tuple[Effort, ...]:
        found = next((m for m in self.models if m.id == model), None)
        return found.efforts if found is not None and found.efforts else self.feature_efforts

    def all_efforts(self) -> tuple[Effort, ...]:
        """Every effort some model offers (with no models, the feature
        list's), weakest first."""
        every = {e for m in self.models for e in self.efforts(m.id)} if self.models else set(self.feature_efforts)
        return tuple(e for e in EFFORTS if e in every)

    def problem(self, intelligence: Intelligence) -> str | None:
        """``unsupported_model`` or ``unsupported_effort`` when the catalog
        lacks ``intelligence`` (its effort checked with its model), else None."""
        if intelligence.model is not None and intelligence.model not in {m.id for m in self.models}:
            return "unsupported_model"
        if intelligence.effort is not None and intelligence.effort not in self.efforts(intelligence.model):
            return "unsupported_effort"
        return None

    def describe(self) -> str:
        """The catalog in one clause: its models by id (and name), and their
        efforts; a model that offers fewer is named with its own."""
        models = ", ".join(m.id if not m.name or m.name == m.id else f"{m.id} ({m.name})" for m in self.models)
        every = self.all_efforts()
        fewer = [f"{m.id} only {', '.join(self.efforts(m.id)) or 'none'}" for m in self.models
                 if self.efforts(m.id) != every]
        return (f"the models {models or 'none'}, at effort {', '.join(every) or 'none'}"
                + (f" ({'; '.join(fewer)})" if fewer else ""))


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
    """A thread (and a job run) runs in ``auto`` where the runtime's own
    safety check decides it natively; elsewhere in ``accept_edits``, where
    XO's policy answers what the CLI asks (§0.90, superseding ch 10 §5 for
    threads). Read from the feature list, so a runtime that gains a native
    auto switches by itself."""
    return "auto" if offered(features, "permissions.auto").support == "native" else "accept_edits"


def default_effort(features: FeatureList, wanted: Effort) -> Effort | None:
    """The preset's effort when the runtime offers it, else the CLI's own."""
    return wanted if wanted in offered(features, "effort").values else None


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
