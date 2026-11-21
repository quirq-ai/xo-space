"""Capability ``turn_usage``: the tokens of a turn the engine never saw end,
read back from its raw log (``engine/turns/<turn id>.jsonl``) at boot.

When the server dies mid-turn (out of memory, a kill), recovery marks the
turn interrupted; without this its usage stayed unknown, though every call
it made was spent (the 11-19 audit: 53 such turns, about 79M weighted
tokens). The raw log holds every line claude wrote, so the same wire that
counts a live turn counts it again here: each API call once, root and
sub-agents (``runtime.wire.Ledger``).
"""

from __future__ import annotations

import json
from typing import Iterable

from services.cowork_agent.adapters.claude_code.runtime.wire import Wire
from services.cowork_agent.adapters.contract import TokenUsage, TurnFinished, Usage

#: A line the raw log wrote in its own envelope (a codex diff block), not claude's.
OWN_LINE = '{"xo.raw"'


def usage_of(lines: Iterable[str]) -> TokenUsage | None:
    """The turn's tokens from its raw lines (every turn claude ended in them,
    and what was cut, added up); None when it made no call."""
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
