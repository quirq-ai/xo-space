"""The contract a backend implements to run projects-chat turns: capability ``coordinator``.

The projects chat names no agent. A backend opts in by shipping
``services/cowork_agent/adapters/<name>/coordinator.py``, loaded through
``adapters.loader.try_load_capability("coordinator")``; without it the
projects-chat routes answer 501 for that backend. The module provides:

Required
  ``SUPPORTED_ROLES``  the roles it can run (``workspace-coordinator``,
                       ``project-coordinator``, ``thread``)
  ``async run_turn(spec: TurnSpec) -> dict``
                       run one turn to completion and return
                       ``{ok, exit_code, native_session_id, outcome, result_text,
                       tool_calls, error}``; never raise for an agent failure

Optional
  ``prompt_fingerprint(role) -> str``
                       changes when the role's prompt or settings change; a
                       coordinator started on an older one is replaced
  ``WORKTREES_SUBDIR`` where, inside a project, the backend keeps worktrees
                       (default :data:`DEFAULT_WORKTREES_SUBDIR`)

What the backend must do with a :class:`TurnSpec`: run the role (a
coordinator with comms tools and read-only tools only; a thread with its
normal tools in ``cwd``), give the agent the MCP server at ``comms_url``
under the name ``comms_server``, treat a successful call to one of
``terminal_tools`` (bare tool names) as ending the turn, feed ``prompt`` as
the turn's input, start a new conversation with id ``agent_session_uuid``
or resume it when ``resume`` is true, and write the agent's raw output to
``log_path``. How it names tools, flags and files is its own business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

DEFAULT_WORKTREES_SUBDIR = ".xo-worktrees"


@dataclass
class TurnSpec:
    role: str
    session_id: str
    agent_session_uuid: str
    resume: bool
    cwd: str
    prompt: str
    comms_url: str
    comms_server: str
    terminal_tools: list[str]
    add_dirs: list[str] = field(default_factory=list)
    budget_usd: Optional[float] = None
    log_path: Optional[str] = None


def worktrees_subdir(capability) -> str:
    return getattr(capability, "WORKTREES_SUBDIR", None) or DEFAULT_WORKTREES_SUBDIR


def fingerprint(capability, role: str) -> Optional[str]:
    func = getattr(capability, "prompt_fingerprint", None)
    return func(role) if callable(func) else None
