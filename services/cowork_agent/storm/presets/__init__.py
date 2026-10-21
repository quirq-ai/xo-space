"""Presets: every storm surface becomes a ``SessionSpec`` (design ch 10).

``build(ctx, features)`` is a pure core function: the caller resolves the
folder, the comms mount and the pins (no I/O here), then hands the result to
``Engine.create_session(spec, agent=…, role=result.role, preset=…,
requires=result.requires)``, which runs the runtime's ``check()`` and refuses
a session whose ``requires`` would rest on the prompt or on nothing (ch 01
§6). No preset names an agent: where roles differ between CLIs, it reads the
runtime's feature list.
"""

from __future__ import annotations

from typing import Callable, Mapping

from services.cowork_agent.adapters.contract import FeatureList
from services.cowork_agent.storm.presets import coordinators, plain_chat, thread
from services.cowork_agent.storm.presets.common import (
    ROLES,
    Catalog,
    Intelligence,
    PresetContext,
    PresetName,
    PresetRefused,
    PresetResult,
    fingerprint,
)

#: The preset table (ch 10 §4).
PRESETS: Mapping[str, Callable[[PresetContext, FeatureList], PresetResult]] = {
    "plain_chat": plain_chat.build,
    "workspace_coordinator": coordinators.build,
    "project_coordinator": coordinators.build,
    "thread": thread.build,
    "job": thread.build,
}


def build(ctx: PresetContext, features: FeatureList) -> PresetResult:
    return PRESETS[ctx.preset](ctx, features)


__all__ = ["PRESETS", "ROLES", "Catalog", "Intelligence", "PresetContext", "PresetName", "PresetRefused",
           "PresetResult", "build", "fingerprint"]
