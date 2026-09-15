"""
Stopping an agent subprocess when its chat turn is cancelled.

A chat turn is cancelled when the browser closes the stream or the user presses
Stop (``POST /api/chat/abort``). The CLI adapters spawn the agent in its own
session (``start_new_session=True``), so the process group holds the CLI and
every tool process it started, and one signal reaches all of them. Without
this, a cancelled turn kept editing files and spending tokens after the person
had stopped it.

Agent-agnostic: shared by every CLI adapter, names no backend.
"""
from __future__ import annotations

import asyncio
import os
import signal

# How long a SIGTERM'd agent gets to exit before it is SIGKILL'd.
TERM_GRACE_SECONDS = 3.0


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the process group led by ``proc`` (the process itself off POSIX)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass  # already gone: the outcome we wanted


async def terminate_process_tree(proc: asyncio.subprocess.Process | None) -> None:
    """Stop ``proc`` and its process group if it is still running.

    SIGTERM first so the agent can flush its transcript, SIGKILL after
    :data:`TERM_GRACE_SECONDS`. A no-op for a process that already exited, so
    it is safe to call from every ``finally``.
    """
    if proc is None or proc.returncode is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERM_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _signal_group(proc, signal.SIGKILL)
    except BaseException:
        # Cancelled again while waiting: do not leave the process running.
        _signal_group(proc, signal.SIGKILL)
        raise
