"""Plain chat: one session with its agent, as ``/api/chat/prompt`` starts it
(design ch 10 §4.1; owner decisions ch 10 §13.3–§13.5).

Its history is its CLI's own transcript and it has no reply gate: the
answer is the CLI's own text. Its permission mode is the request's, any mode
the runtime offers, ``auto`` by default; ``bypass`` is never built here (no
preset declares it, ch 01 §3.3). It always mounts Composio as today's chat
does (§0.108): a marker here, its address filled in when the process starts
(``storm/composio.py``). With ``comms`` asked for, it also gets the ``plain``
comms tools (ch 12 §3). No system prompt.
"""

from __future__ import annotations

from services.cowork_agent.adapters.contract import FeatureList, PermissionPolicy, SessionSpec, ToolPolicy
from services.cowork_agent.storm import composio
from services.cowork_agent.storm.presets.common import (
    SHARED,
    PresetContext,
    PresetRefused,
    PresetResult,
    ROLES,
    fingerprint,
    offered,
)

DEFAULT_MODE = "auto"


def build(ctx: PresetContext, features: FeatureList) -> PresetResult:
    request = ctx.request
    mode = request.get("permission_mode") or DEFAULT_MODE
    modes = offered(features, "permissions.mode").values
    if mode == "bypass" or mode not in modes:
        allowed = ", ".join(m for m in modes if m != "bypass")
        raise PresetRefused("unsupported_permission_mode", f"{mode} isn't a mode a plain chat on {ctx.agent} can "
                                                           f"use; choose one of: {allowed}.")
    if request.get("comms") and ctx.comms is None:
        raise PresetRefused("comms_required", "This plain chat asked for comms tools; build it with its comms "
                                              "server.")
    spec = SessionSpec(
        cwd=ctx.cwd,
        model=ctx.intelligence.model,
        effort=ctx.intelligence.effort,
        tools=ToolPolicy(preset="standard"),
        permissions=PermissionPolicy(mode=mode),
        mcp_servers=(composio.marker(), *((ctx.comms,) if request.get("comms") else ())),
        **SHARED,
    )
    return PresetResult(spec=spec, role=ROLES["plain_chat"], requires=(), fingerprint=fingerprint(spec))
