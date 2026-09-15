"""Read-only Space session telemetry for the Claude Code runtime.

Argus owns ingestion and pricing. This capability only resolves its database
location and adapts the existing builder to the generic multi-provider
telemetry contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from services.cowork_agent.visualizer.argus_index import build_argus_stats


SOURCE_ID = "claude_code"
SOURCE_LABEL = "Claude Code"
META_PRIORITY = 100  # keep the legacy Argus meta fields at the top level
COST_STATUS = "estimated"
# How Space's Configure page presents and edits this source. Read generically
# by services/telemetry_sources.py; the env key is the one collect() honors.
SOURCE_CONFIG = {
    "vendor": "anthropic",
    "path_env": "ARGUS_DB",
    "path_default": "~/.argus/argus.db",
    "path_kind": "file",
    "path_label": "Argus database",
    "collects": ["Sessions and turns", "Token usage", "Tool calls"],
    "never": "No prompt text stored",
}


def collect_session_telemetry() -> dict:
    db_path = Path(os.getenv("ARGUS_DB", "~/.argus/argus.db")).expanduser()
    # Argus can contain rows from several runtimes. This capability represents
    # only its owning runtime so another provider cannot duplicate those rows.
    data = build_argus_stats(db_path, agent=SOURCE_ID)
    return {
        "source": {
            "id": SOURCE_ID,
            "label": SOURCE_LABEL,
            "cost_status": COST_STATUS,
        },
        "meta_priority": META_PRIORITY,
        "meta": data["meta"],
        "totals": data["totals"],
        "project_keys": data["project_keys"],
        "sessions": data["sessions"],
        "daily_models": data["daily_models"],
        "daily_sessions": data["daily_sessions"],
        "daily_tools": data["daily_tools"],
    }


# ---------------------------------------------------------------------------
# Ingestion daemon lifecycle.
#
# Argus writes argus.db; without it running, collect_session_telemetry above
# reads a stale or missing file and the Sessions tab shows its error card.
# The server calls these through the capability loader, so core never has to
# know that this runtime's telemetry happens to be Argus.
#
# Starting a daemon is not installing software: argus-code is pinned in
# requirements.txt and already sits in the environment. That is why these are
# not gated by QUIRQ_SKIP_BOOT_INSTALL.
# ---------------------------------------------------------------------------

_DAEMON_TIMEOUT = 30


def _argus_binary() -> str | None:
    """Locate the argus console script.

    pip installs console scripts beside the interpreter that owns them. The
    server is normally launched as venv/bin/python without activating the
    venv, so venv/bin is not on PATH — look there first, then fall back.
    """

    for name in ("argus", "argus.exe"):
        candidate = Path(sys.executable).with_name(name)
        if candidate.exists():
            return str(candidate)
    return shutil.which("argus")


def _run_daemon_command(action: str) -> None:
    """Run `argus daemon <action>`, reporting rather than raising.

    Idempotent in both directions: argus answers "already running" / "not
    running" instead of failing, and both are treated as success. Telemetry is
    never worth failing a boot or blocking a shutdown over.
    """

    binary = _argus_binary()
    if not binary:
        print("⚠️ argus binary not found — session telemetry unavailable")
        return

    try:
        result = subprocess.run(
            [binary, "daemon", action],
            check=False,
            capture_output=True,
            text=True,
            timeout=_DAEMON_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"⚠️ argus daemon {action} timed out after {_DAEMON_TIMEOUT}s (non-fatal)")
        return
    except Exception as exc:
        print(f"⚠️ argus daemon {action} failed (non-fatal): {exc}")
        return

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        print(f"   argus daemon {action}: {output or 'ok'}")
        return
    if "already running" in output.lower() or "not running" in output.lower():
        print(f"   argus daemon {action}: {output}")
        return
    print(f"⚠️ argus daemon {action} failed (non-fatal): {output}")


def start_daemon() -> None:
    """Start Argus ingestion. Called at server startup."""
    _run_daemon_command("start")


def stop_daemon() -> None:
    """Stop Argus ingestion. Called at server shutdown."""
    _run_daemon_command("stop")
