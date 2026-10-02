"""Custody adapter: drive the custody CLI from the Space.

Custody (https://github.com/vilka-9999/custody) is an auditing agent: a
remediator proposes bounded fixes for one finding at a time under a scope
contract, and an adversarial auditor rules on every attempt, recording the
whole proceeding in an append-only, hash-chained ledger. The ledger doubles
as the session store this Space reads (see ``visualizer_source.py``), which
makes custody's activity feed tamper-evident rather than best-effort.

The adapter is a thin, allowlisted bridge to ``python -m custody``. The
"question" is a custody command line (``survey .``, ``harden ../repo
--limit 5``); anything outside the CLI's own subcommands is refused rather
than passed to a shell — no shell is involved at any point.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from services.cowork_agent.adapters.base import BaseAgentAdapter

ALLOWED_COMMANDS = frozenset({"survey", "harden", "verify", "trial", "eval", "console"})
"""Custody subcommands the Space may invoke. Nothing else reaches a process."""

_STREAM_CHUNK_CHARS = 512


def _python(config: dict[str, Any]) -> str:
    """Return the interpreter that has custody importable."""
    configured = config.get("python")
    return str(configured) if configured else sys.executable


def _session_id_for(repo: Path) -> Optional[str]:
    """Derive the run's session id from the ledger head marker.

    Custody seals every entry against the previous one and records the tail
    in ``.custody/ledger.jsonl.head``; the recorded seal prefix is a stable,
    content-derived identifier for "where this run left the record".
    """
    head = repo / ".custody" / "ledger.jsonl.head"
    try:
        marker = json.loads(head.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    seal = marker.get("entry_hash") if isinstance(marker, dict) else None
    return str(seal)[:12] if isinstance(seal, str) and seal else None


class CustodyAdapter(BaseAgentAdapter):
    """Runs custody commands and reports the ledger position they reached."""

    @property
    def adapter_name(self) -> str:
        return "custody"

    def _parse(self, question: str) -> list[str]:
        """Turn the question into a validated argv, or raise ``ValueError``."""
        tokens = shlex.split(question or "")
        if not tokens:
            raise ValueError(
                "empty command; expected a custody subcommand such as "
                "'survey .' or 'harden ../repo --limit 5'"
            )
        if tokens[0] not in ALLOWED_COMMANDS:
            raise ValueError(
                "unsupported subcommand %r; custody accepts: %s"
                % (tokens[0], ", ".join(sorted(ALLOWED_COMMANDS)))
            )
        return tokens

    def _repo_argument(self, tokens: list[str]) -> Path:
        """Return the repository path a command targets (default: cwd)."""
        positional = [t for t in tokens[1:] if not t.startswith("-")]
        return Path(positional[0]).expanduser() if positional else Path(".")

    async def _execute(self, tokens: list[str]) -> tuple[str, Optional[str]]:
        """Run one custody command and return (transcript, session id)."""
        process = await asyncio.create_subprocess_exec(
            _python(self.config), "-m", "custody", *tokens,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        timeout = float(self.config.get("timeout", 900))
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return (
                "custody %s timed out after %ss; the run was killed and any "
                "partial attempt is recorded in the ledger" % (tokens[0], timeout),
                None,
            )
        transcript = raw.decode("utf-8", errors="replace")
        if process.returncode not in (0, None):
            transcript += "\n(exit code %d)" % process.returncode
        return transcript, _session_id_for(self._repo_argument(tokens))

    async def run(
        self, question: str, session_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            tokens = self._parse(question)
        except ValueError as exc:
            return {"message": str(exc), "native_session_id": session_id}
        transcript, native = await self._execute(tokens)
        return {"message": transcript, "native_session_id": native or session_id}

    async def stream(
        self, question: str, session_id: str | None = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        result = await self.run(question, session_id=session_id, **kwargs)
        message = str(result.get("message", ""))
        for start in range(0, len(message), _STREAM_CHUNK_CHARS):
            yield {"type": "token", "token": message[start:start + _STREAM_CHUNK_CHARS]}
        yield {"done": True, "native_session_id": result.get("native_session_id")}

    async def health(self) -> dict[str, Any]:
        """Report whether the custody package is importable and versioned."""
        try:
            process = await asyncio.create_subprocess_exec(
                _python(self.config), "-m", "custody", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except (OSError, asyncio.TimeoutError) as exc:
            return {"ok": False, "reason": "custody CLI unavailable: %s" % exc}
        if process.returncode != 0:
            return {"ok": False, "reason": raw.decode("utf-8", errors="replace").strip()}
        return {"ok": True, "version": raw.decode("utf-8", errors="replace").strip()}


Adapter = CustodyAdapter
