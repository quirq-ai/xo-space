"""Sharing: the cross-workspace commit relay (pull-based, workspace-anchored).

Projects shared with you and by you, kept in step. Core code: names no
agent. Talks to the swarm broker for membership and the commit ledger, and
to GitHub via git for objects. Machine-local state lives under
``~/.quirq/sharing/`` (``store.py`` lists the files). Entry point:
``poller.run_relay_poller``, started by the supervisor as the module's
``relay`` task. ``services.cowork_agent.project_sharing`` is the old import
path and resolves to these same modules.
"""
from datetime import datetime


def log_line(msg: str) -> None:
    """Timestamped print(flush=True). Relay activity must be visible in the
    service log; module-level logging is invisible under the default config.
    A console that cannot encode a glyph gets a lossy line, never an exception:
    logging must not be able to break the loop."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
