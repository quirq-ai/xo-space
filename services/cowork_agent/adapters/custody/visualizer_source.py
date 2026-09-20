"""Custody visualizer source — tails hash-chained audit ledgers.

Custody records every action of an audited remediation run as one JSONL
entry in ``<repo>/.custody/ledger.jsonl``, each entry sealed against the one
before it. This source tails those ledgers and emits normalised activity
events, which gives the Space a property no other runtime's feed has: the
session store it reads is tamper-evident. Editing, deleting or truncating
the record breaks the chain, and ``custody verify`` says so offline.

Loaded by ``services.cowork_agent.visualizer.source_loader.load_source_module``
when ``AGENT_NAME=custody``. The class name ``Source`` is the loader
contract; ``name`` must equal the adapter directory name.

Ledger entry shape (all keys always present):
``{seq, ts, actor, action, target, detail, files_touched, tokens_in,
tokens_out, cost_usd, verdict, prev_hash, entry_hash}``. Mapping (PII
boundary enforced — names, paths and counts only; ``target`` and ``detail``
carry free text and are never emitted):

    run.started                  → SessionFirstSeen(cwd=<repo>) + MessageObserved(role="user")
    every entry                  → ToolUseObserved(tool=<action>)
    case.ruled                   → ToolUseObserved(tool="verdict." + <verdict, lowercased enum>)
    files_touched[]              → FileTouched(relative_path=..., created=False)
    tokens_in/tokens_out > 0     → UsageObserved(input_tokens=..., output_tokens=...)

Sessions are runs: an entry belongs to the most recent ``run.started``,
whose ``entry_hash`` prefix is the session id — content-derived, so the same
ledger yields the same session ids on every machine. Cost lives in the
``session_telemetry`` capability (``session_telemetry.py``), not here: the
watcher's stats sink models tokens only.

Presence: custody runs are batch processes with no per-pid presence file,
so ``poll_presence`` returns [] — a valid "no live sessions" answer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
)
from services.cowork_agent.visualizer.workspace_index import list_project_ids
from services.cowork_agent.project_layout import xo_projects_root

logger = logging.getLogger(__name__)

_BACKEND = "custody"
_LEDGER_RELATIVE = Path(".custody") / "ledger.jsonl"
_SESSION_ID_CHARS = 12


def iter_ledgers() -> Iterator[tuple[str, Path]]:
    """Yield ``(project_id, ledger_path)`` for every project ledger.

    A project may be the audited repository itself, or contain audited
    repositories one level down (a workspace of checkouts), so both depths
    are searched. Module-level so tests can patch the roots it derives from.
    """
    root = xo_projects_root()
    for project_id in list_project_ids():
        project_root = (root / project_id).resolve()
        candidates = [project_root / _LEDGER_RELATIVE]
        try:
            candidates.extend(sorted(project_root.glob("*/" + _LEDGER_RELATIVE.as_posix())))
        except OSError:  # pragma: no cover - unreadable project root
            continue
        for ledger in candidates:
            if ledger.is_file():
                yield project_id, ledger


class Source:
    """Visualizer source for the custody backend.

    The class name ``Source`` is the loader contract — see
    ``services/cowork_agent/visualizer/source_loader.py``.
    """

    name = _BACKEND

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        self.offsets = offsets or jsonl_tail.OffsetStore()
        # Ledger path -> current run's session id. Rebuilt lazily: entries
        # read after a restart, before a new run.started, key onto their
        # prev_hash so they stay grouped and stable rather than dropped.
        self._session_by_path: dict[str, str] = {}

    # -- events ---------------------------------------------------------

    def poll_events(self) -> Iterator[Event]:
        for project_id, ledger in iter_ledgers():
            try:
                yield from self._tail_one(project_id, ledger)
            except OSError as exc:  # pragma: no cover - transient FS races
                logger.debug("custody source: %s unreadable (%s)", ledger, exc)
        self.offsets.flush()

    def _tail_one(self, project_id: str, ledger: Path) -> Iterator[Event]:
        for raw in jsonl_tail.read_new_lines(ledger, self.offsets):
            try:
                entry = json.loads(raw)
            except ValueError:
                # A malformed line is tampering or corruption; the viewer
                # stays read-only and lets `custody verify` make the charge.
                logger.debug("custody source: malformed ledger line in %s", ledger)
                continue
            if not isinstance(entry, dict):
                continue
            yield from self._events_for(project_id, ledger, entry)

    def _events_for(
        self, project_id: str, ledger: Path, entry: dict
    ) -> Iterator[Event]:
        ts = str(entry.get("ts", ""))
        action = str(entry.get("action", ""))
        session = self._session_for(ledger, entry, action)
        base = {
            "ts": ts,
            "native_session_id": session,
            "runtime": self.name,
            "project_id": project_id,
        }

        if action == "run.started":
            yield SessionFirstSeen(cwd=str(ledger.parent.parent), **base)
            yield MessageObserved(role="user", **base)

        if action:
            yield ToolUseObserved(tool=action, **base)

        verdict = str(entry.get("verdict", ""))
        if verdict:
            yield ToolUseObserved(tool="verdict." + verdict.lower(), **base)

        for path in entry.get("files_touched") or []:
            relative = str(path).replace("\\", "/")
            if relative.startswith("/") or ".." in relative.split("/"):
                continue
            yield FileTouched(relative_path=relative, created=False, **base)

        tokens_in = int(entry.get("tokens_in") or 0)
        tokens_out = int(entry.get("tokens_out") or 0)
        if tokens_in or tokens_out:
            yield UsageObserved(
                input_tokens=tokens_in, output_tokens=tokens_out, **base
            )

    def _session_for(self, ledger: Path, entry: dict, action: str) -> str:
        """Return the run-scoped session id this entry belongs to."""
        key = str(ledger)
        if action == "run.started":
            seal = str(entry.get("entry_hash", "")) or str(entry.get("prev_hash", ""))
            self._session_by_path[key] = seal[:_SESSION_ID_CHARS] or "run"
        current = self._session_by_path.get(key)
        if current is None:
            # Restart mid-ledger: anchor on the chain itself so grouping
            # stays deterministic without inventing a run boundary.
            current = "resume-" + str(entry.get("prev_hash", ""))[:8]
            self._session_by_path[key] = current
        return current

    # -- presence -------------------------------------------------------

    def poll_presence(self) -> list[dict]:
        return []
