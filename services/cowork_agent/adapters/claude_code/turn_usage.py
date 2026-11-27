"""Capability ``turn_usage``: at boot, recount a turn's tokens from its raw log when the server died mid-turn."""

from __future__ import annotations

import json
from typing import Iterable

from services.cowork_agent.adapters.claude_code.runtime.wire import Wire
from services.cowork_agent.adapters.contract import TokenUsage, TurnFinished, Usage

#: A line the raw log wrote in its own envelope (``storm.engine.raw_turns``), not claude's.
OWN_LINE = '{"xo.raw"'


def usage_of(lines: Iterable[str]) -> TokenUsage | None:
    """The tokens summed over every turn in the raw lines, a cut one included; None when no call was made."""
    wire = Wire(cwd="", runs_detail="steps", coordinator=False)
    wire.start_turn("system")
    found: list[TokenUsage] = []
    for line in lines:
        if not line.strip() or line.startswith(OWN_LINE):
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue                      # a line cut by the crash
        if not isinstance(msg, dict):
            continue
        for out in wire.line(msg):
            if out.cls is Usage and out.fields.get("usage") is not None:
                found.append(out.fields["usage"])
    if wire.current_turn is not None:
        for out in wire.close_turn(status="interrupted", error=None):
            if out.cls is TurnFinished and out.fields.get("usage") is not None:
                found.append(out.fields["usage"])
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return TokenUsage(input=sum(u.input for u in found), output=sum(u.output for u in found),
                      cache_read=sum(u.cache_read for u in found), cache_write=sum(u.cache_write for u in found),
                      context_tokens=found[-1].context_tokens, context_window=found[-1].context_window)
