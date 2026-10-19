"""A thread, and a job run, which is a thread (design ch 10 §4.4, §4.5).

A thread works in its own worktree (or, in a project that isn't a git
repository, in the project folder: the caller decides ``cwd``, ch 13 §2).
It keeps every tool its CLI offers (ch 10 §6): no deny set. Its mode is
``auto`` where the runtime's own safety check decides natively, else
``accept_edits`` with XO's policy answering (§0.90). It must have its comms
server: a thread is heard only through it.

A job run's spec is a thread's; the job's prompt is its first input, which
the caller delivers.
"""

from __future__ import annotations

from services.cowork_agent.adapters.contract import FeatureList, PermissionPolicy, SessionSpec, ToolPolicy
from services.cowork_agent.storm.presets.common import (
    SHARED,
    PresetContext,
    PresetRefused,
    PresetResult,
    ROLES,
    default_effort,
    fingerprint,
    role_prompt,
    thread_mode,
)

REQUIRES = ("mcp_servers",)
#: A thread does the work (PoC, D49).
EFFORT = "high"


def build(ctx: PresetContext, features: FeatureList) -> PresetResult:
    if ctx.comms is None:
        raise PresetRefused("comms_required", "A thread speaks only through xo_comms; build it with its comms "
                                              "server.")
    spec = SessionSpec(
        cwd=ctx.cwd,
        model=ctx.intelligence.model,
        effort=ctx.intelligence.effort or default_effort(features, EFFORT),
        system_prompt=role_prompt("thread"),
        tools=ToolPolicy(preset="standard"),
        permissions=PermissionPolicy(mode=thread_mode(features)),
        mcp_servers=(ctx.comms,),
        **SHARED,
    )
    return PresetResult(spec=spec, role=ROLES[ctx.preset], requires=REQUIRES, fingerprint=fingerprint(spec))
