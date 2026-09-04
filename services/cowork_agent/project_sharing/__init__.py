"""Cross-workspace commit relay client (pull-based, workspace-anchored).

Core code: names no agent. Talks to the swarm broker for membership and the
commit ledger, and to GitHub via git for objects. Machine-local state lives
under ~/.quirq/project_sharing/ (see state.py). Entry point: poller.run_relay_poller().
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
