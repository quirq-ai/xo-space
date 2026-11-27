"""A thread, and a job run, which is a thread (design ch 10 §4.4, §4.5).

A thread works in its own worktree (or, in a project that isn't a git
repository, in the project folder: the caller decides ``cwd``, ch 13 §2).
It keeps every tool its CLI offers (ch 10 §6): no deny set. Its mode is
``auto`` where the runtime's own safety check decides natively, else
``accept_edits`` with XO's policy answering (§0.90). It must have its comms
server: a thread is heard only through it.

A job run's spec is a thread's; the job's prompt is its first input, which
the caller delivers.

A try is a thread that only reads: the coordinators' read-only
tools, mode and orchestration, the one shape every runtime proved it
enforces, with its comms server. A runtime that can't enforce
it refuses it through its check (``TRY_REQUIRES``), never runs it wider.
A run with an agent (``ctx.persona``) works as it (``with_persona``).
"""

from __future__ import annotations

from services.cowork_agent.adapters.contract import (
    FeatureList,
    PermissionMode,
    PermissionPolicy,
    SessionSpec,
    ToolPolicy,
)
from services.cowork_agent.storm.presets.common import (
    SHARED,
    PresetContext,
    PresetRefused,
    PresetResult,
    ROLES,
    intended_effort,
    fingerprint,
    role_prompt,
    thread_mode,
    with_persona,
)
from services.cowork_agent.storm.presets import coordinators

REQUIRES = ("mcp_servers",)
TRY_REQUIRES = (*REQUIRES, *coordinators.REQUIRES)
#: A thread does the work (PoC, D49).
EFFORT = "high"


def build(ctx: PresetContext, features: FeatureList) -> PresetResult:
    if ctx.comms is None:
        raise PresetRefused("comms_required", "A thread speaks only through xo_comms; build it with its comms "
                                              "server.")
    trying = ctx.preset == "try"
    tools = ToolPolicy(preset="read_only", allow=(*coordinators.READ_SET, f"mcp:{ctx.comms.name}/*"),
                       deny=coordinators.DENY) if trying else ToolPolicy(preset="standard")
    mode: PermissionMode = "deny_unasked" if trying else thread_mode(features)
    spec = with_persona(SessionSpec(
        cwd=ctx.cwd,
        model=ctx.intelligence.model,
        effort=intended_effort(ctx.intelligence, features, EFFORT),
        system_prompt=role_prompt("thread"),
        tools=tools,
        permissions=PermissionPolicy(mode=mode),
        orchestration="coordinator" if trying else "none",
        mcp_servers=(ctx.comms,),
        **SHARED,
    ), ctx.persona)
    return PresetResult(spec=spec, role=ROLES[ctx.preset], requires=TRY_REQUIRES if trying else REQUIRES,
                        fingerprint=fingerprint(spec))
