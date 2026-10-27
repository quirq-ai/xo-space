"""The workspace and project coordinators (design ch 10 §4.2, §4.3).

A coordinator works in the real folder (§0.26): the projects root, or its
project's folder. It is read-only on every runtime: orchestration
``coordinator``, mode ``deny_unasked``, the read tools plus comms. Each
runtime delivers that its own way (ch 10 §4.3's table); this module only
states the intent.

``shell:git status`` (ch 10 §4.3) is not in a project coordinator's allow
list: claude's allow list for coordinators is exactly ch 02 §3.4's read set
(§0.92), and on every runtime a coordinator's other read-only commands reach
XO's read-only check, which allows ``git status`` by its default rules.
"""

from __future__ import annotations

from services.cowork_agent.adapters.contract import FeatureList, PermissionPolicy, SessionSpec, ToolPolicy
from services.cowork_agent.storm.presets.common import (
    SHARED,
    PresetContext,
    PresetRefused,
    PresetResult,
    ROLES,
    intended_effort,
    fingerprint,
    role_prompt,
)

#: What a coordinator runs unasked: reading, and talking through comms.
READ_SET = ("read", "search", "list", "shell:git log", "shell:git show", "shell:ls")
#: What a coordinator never does, whatever a runtime's tool set holds.
DENY = ("edit", "create", "delete", "web_fetch", "web_search", "worktree", "schedule")
#: Each runtime must enforce these by its CLI or by XO, never by the prompt (ch 10 §9).
REQUIRES = ("orchestration", "tools.deny", "permissions.mode")
#: A coordinator thinks little and delegates (PoC coordinator.json, D49).
EFFORT = "low"


def build(ctx: PresetContext, features: FeatureList) -> PresetResult:
    if ctx.comms is None:
        raise PresetRefused("comms_required", "A coordinator speaks only through xo_comms; build it with its comms "
                                              "server.")
    spec = SessionSpec(
        cwd=ctx.cwd,
        model=ctx.intelligence.model,
        effort=intended_effort(ctx.intelligence, features, EFFORT),
        system_prompt=role_prompt(ctx.preset),
        tools=ToolPolicy(preset="read_only", allow=(*READ_SET, f"mcp:{ctx.comms.name}/*"), deny=DENY),
        permissions=PermissionPolicy(mode="deny_unasked"),
        orchestration="coordinator",
        mcp_servers=(ctx.comms,),
        **SHARED,
    )
    return PresetResult(spec=spec, role=ROLES[ctx.preset], requires=REQUIRES, fingerprint=fingerprint(spec))
