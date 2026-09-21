"""Reading a CLI agent's stdout without one huge line killing the turn.

Every streaming adapter drives its agent as a subprocess emitting one JSON
event per line, and asyncio caps a ``StreamReader`` line at 64 KiB by default.
A line past that makes ``readline()`` raise ``ValueError`` — which, iterated
with ``async for``, propagates out of the adapter's generator and ends the SSE
response mid-turn. The subprocess keeps running and finishes on disk, so the
symptom is a chat that freezes and then shows the whole answer at once.

The lines that do it are tool results: one ``npm install``, ``find`` or
``cat`` of a large file is enough. ``LINE_LIMIT`` raises the cap to a size no
realistic tool result reaches, and ``iter_lines`` keeps the turn alive if one
ever does.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

# Per-line cap for an agent's stdout. Generous on purpose: a tool result is
# carried on a single line, and losing it costs the user the rest of the turn.
LINE_LIMIT = 16 * 1024 * 1024


async def iter_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield stdout lines, dropping any that exceed the reader's limit.

    Pass ``limit=LINE_LIMIT`` to ``create_subprocess_exec`` as well — this is
    the backstop for the pathological case, not the primary defence.

    On an over-long line ``readline()`` has already discarded it from the
    buffer, so reading simply continues with the next one: that event is lost
    (the transcript still has it, read from the agent's own log), while every
    later event in the turn still reaches the UI.
    """
    while True:
        try:
            line = await stream.readline()
        except ValueError:
            continue
        if not line:
            return
        yield line
