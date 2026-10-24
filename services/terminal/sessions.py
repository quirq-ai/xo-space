"""The terminals this server is running: create, attach, rename, close, reap.

``registry`` is the one instance. Everything runs on the event loop, so a
check followed by a change (attach: read the scrollback position and join the
viewers) has no ``await`` between them and nothing can slip in.

Attaching takes a *ticket*: a random string handed out by a guarded POST and
good once, for :data:`config.TICKET_TTL_SECONDS`, for one terminal. The
WebSocket receives it as its first message, never in the URL (servers log
URLs). Only a hash is kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from services import background
from services.errors import ServiceError
from services.periodic import run_forever
from services.terminal import config, environment
from services.terminal.scrollback import Scrollback
from services.timestamps import TS_FORMAT
from utils.commands import spawn_pty

log = logging.getLogger(__name__)

REAPER_NAME = "terminal reaper"
REAPER_INTERVAL_SECONDS = 60.0
MAX_NAME_CHARS = 60
MIN_SIZE, MAX_SIZE = 1, 500


class TerminalError(ServiceError):
    pass


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime(TS_FORMAT)


def clamp_size(value, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, MIN_SIZE), MAX_SIZE)


def clean_name(raw) -> str:
    """A terminal name as shown: printable, single-line, 1–60 characters."""
    if not isinstance(raw, str):
        raise TerminalError("bad_name", "A terminal name must be text.")
    name = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C").strip()
    if not name:
        raise TerminalError("bad_name", "A terminal name cannot be empty.")
    if len(name) > MAX_NAME_CHARS:
        raise TerminalError("bad_name", f"A terminal name is at most {MAX_NAME_CHARS} characters.")
    return name


@dataclass(eq=False)
class Viewer:
    """One attached browser: where it is in the output, and whether it asked to pause."""

    cursor: int = 0
    paused: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class Terminal:
    def __init__(self, terminal_id: str, name: str, project: Optional[str], cwd: Path, *,
                 path: Optional[str] = None, now: Callable[[], float]) -> None:
        self.id = terminal_id
        self.name = name
        self.project = project
        self.path = path or None
        self.cwd = cwd
        self._now = now
        self.created_at = now()
        self.last_attached_at: Optional[float] = None
        self.last_output_at: Optional[float] = None
        self.detached_since: Optional[float] = self.created_at
        self.ended_at: Optional[float] = None
        self.ended_reason: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.scrollback = Scrollback(config.SCROLLBACK_BYTES)
        self.viewers: set[Viewer] = set()
        self.size = (24, 80)
        self.pty = None
        self.closing = False

    # ── wiring from the pty ──────────────────────────────────────────────
    def on_output(self, data: bytes) -> None:
        self.scrollback.append(data)
        self.last_output_at = self._now()
        self._wake_all()

    def on_exit(self, code: int) -> None:
        self.exit_code = code
        self.ended_at = self._now()
        self.ended_reason = self.ended_reason or "exited"
        self._wake_all()

    def _wake_all(self) -> None:
        for viewer in self.viewers:
            viewer.wake.set()

    @property
    def running(self) -> bool:
        return self.exit_code is None

    # ── viewers ──────────────────────────────────────────────────────────
    def attach(self) -> Viewer:
        """Join the viewers. The new viewer starts at the beginning of what is
        kept, so its first reads replay the terminal's recent output."""
        viewer = Viewer(cursor=0)
        viewer.wake.set()
        self.viewers.add(viewer)
        self.last_attached_at = self._now()
        self.detached_since = None
        self._apply_rate()
        return viewer

    def detach(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        if not self.viewers:
            self.detached_since = self._now()
        self._apply_rate()

    def _apply_rate(self) -> None:
        if self.pty is not None:
            self.pty.set_rate(config.ATTACHED_RATE if self.viewers else config.DETACHED_RATE)

    # ── input ────────────────────────────────────────────────────────────
    def write(self, data: bytes) -> bool:
        return self.running and self.pty is not None and self.pty.write(data)

    def resize(self, rows, cols) -> None:
        """The latest viewer's size wins. An unchanged size still repaints, so a
        viewer that just attached sees full-screen programs redrawn."""
        if not self.running or self.pty is None:
            return
        size = (clamp_size(rows, self.size[0]), clamp_size(cols, self.size[1]))
        if size == self.size:
            self.pty.redraw()
        else:
            self.size = size
            self.pty.resize(*size)

    # ── what the API shows ───────────────────────────────────────────────
    def closes_at(self) -> Optional[float]:
        ttl = config.detached_ttl_seconds()
        if not self.running or self.detached_since is None or ttl <= 0:
            return None
        return self.detached_since + ttl

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "scope": "workspace" if self.project is None else "project",
            "project": self.project,
            "path": self.path,
            "cwd": str(self.cwd),
            "status": "running" if self.running else "exited",
            "exit_code": self.exit_code,
            "ended_reason": self.ended_reason,
            "foreground": self.pty.foreground() if self.running and self.pty is not None else None,
            "viewers": len(self.viewers),
            "size": {"rows": self.size[0], "cols": self.size[1]},
            "created_at": _iso(self.created_at),
            "last_attached_at": _iso(self.last_attached_at),
            "last_output_at": _iso(self.last_output_at),
            "detached_since": _iso(self.detached_since) if self.running else None,
            "closes_at": _iso(self.closes_at()),
            "ended_at": _iso(self.ended_at),
        }


class Registry:
    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._terminals: dict[str, Terminal] = {}
        self._tickets: dict[str, tuple[str, float]] = {}
        self._starting = 0
        self._reaper: Optional[asyncio.Task] = None

    # ── queries ──────────────────────────────────────────────────────────
    def get(self, terminal_id: str) -> Terminal:
        terminal = self._terminals.get(terminal_id)
        if terminal is None:
            raise TerminalError("terminal_not_found", "No such terminal. It may have been closed.", status=404)
        return terminal

    def find(self, terminal_id: str) -> Optional[Terminal]:
        return self._terminals.get(terminal_id)

    def list(self, *, project: Optional[str] = None, path: Optional[str] = None,
             workspace: bool = False) -> list[Terminal]:
        items = sorted(self._terminals.values(), key=lambda t: t.created_at)
        if workspace:
            return [t for t in items if t.project is None]
        if project is not None:
            items = [t for t in items if t.project is not None and t.project.lower() == project.lower()]
        if path is not None:
            try:
                wanted = environment.project_path(project, path) if project is not None else None
            except environment.LocationError:
                return []
            items = [t for t in items if t.path == wanted]
        return items

    def counts(self) -> dict:
        running = [t for t in self._terminals.values() if t.running]
        return {"running": len(running), "attached": sum(1 for t in running if t.viewers)}

    # ── create ───────────────────────────────────────────────────────────
    def _default_name(self, project: Optional[str]) -> str:
        base = project or "Workspace"
        taken = {t.name for t in self._terminals.values()}
        n = 1
        while f"{base} {n}" in taken:
            n += 1
        return f"{base} {n}"

    async def create(self, *, project: Optional[str] = None, path: Optional[str] = None,
                     name=None, rows=None, cols=None) -> tuple[Terminal, str]:
        """Start a terminal in the project's folder, or ``path`` inside it (the
        projects root when ``project`` is None), and return it with a ticket to attach."""
        reason = config.unavailable_reason()
        if reason:
            raise TerminalError("terminal_disabled", reason, status=403)
        cwd = environment.start_folder(project, path)
        limit = config.max_sessions()
        if self.counts()["running"] + self._starting >= limit:
            raise TerminalError(
                "too_many_terminals",
                f"{limit} terminals are already running. Close one you no longer need and try again.",
                status=429,
            )
        label = clean_name(name) if name is not None else self._default_name(project)
        terminal_id = secrets.token_urlsafe(12)
        terminal = Terminal(terminal_id, label, project, cwd, path=environment.project_path(project, path), now=self._now)
        terminal.size = (clamp_size(rows, 24), clamp_size(cols, 80))
        self._starting += 1
        try:
            terminal.pty = await spawn_pty(
                config.shell(), cwd=cwd, env=environment.child_env(),
                rows=terminal.size[0], cols=terminal.size[1],
                on_output=terminal.on_output, on_exit=terminal.on_exit, rate=config.DETACHED_RATE,
            )
        except OSError as exc:
            raise TerminalError("terminal_failed", f"Could not start a terminal: {exc.strerror or exc}.", status=500) from exc
        finally:
            self._starting -= 1
        self._terminals[terminal_id] = terminal
        self._ensure_reaper()
        log.info("terminal %s opened (%s)", terminal_id, "workspace" if project is None else f"project {project}")
        return terminal, self.issue_ticket(terminal_id)

    # ── tickets ──────────────────────────────────────────────────────────
    @staticmethod
    def _hash(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8", "replace")).hexdigest()

    def issue_ticket(self, terminal_id: str) -> str:
        self.get(terminal_id)
        now = self._now()
        self._tickets = {h: v for h, v in self._tickets.items() if v[1] > now}
        ticket = secrets.token_urlsafe(32)
        self._tickets[self._hash(ticket)] = (terminal_id, now + config.TICKET_TTL_SECONDS)
        return ticket

    def consume_ticket(self, terminal_id: str, ticket) -> bool:
        """True once for a live, unexpired ticket issued for this terminal."""
        if not isinstance(ticket, str) or not ticket or len(ticket) > 256:
            return False
        entry = self._tickets.pop(self._hash(ticket), None)
        return entry is not None and entry[0] == terminal_id and entry[1] > self._now()

    # ── change ───────────────────────────────────────────────────────────
    def rename(self, terminal_id: str, name) -> Terminal:
        terminal = self.get(terminal_id)
        terminal.name = clean_name(name)
        return terminal

    async def close(self, terminal_id: str, *, reason: str = "closed") -> None:
        """Hang up the terminal (everything started in it) and forget it."""
        terminal = self.get(terminal_id)
        await self._end(terminal, reason)
        self._terminals.pop(terminal_id, None)

    async def _end(self, terminal: Terminal, reason: str) -> None:
        if terminal.running and terminal.pty is not None and not terminal.closing:
            terminal.closing = True
            terminal.ended_reason = reason
            await terminal.pty.terminate()
            log.info("terminal %s %s", terminal.id, reason)

    async def close_all(self) -> None:
        for terminal_id in list(self._terminals):
            await self.close(terminal_id)

    # ── reaper ───────────────────────────────────────────────────────────
    async def reap(self) -> None:
        """Close terminals nobody has watched for the configured time, and
        forget exited ones after :data:`config.EXITED_KEEP_SECONDS`."""
        now = self._now()
        for terminal in list(self._terminals.values()):
            closes_at = terminal.closes_at()
            if closes_at is not None and now >= closes_at:
                await self.close(terminal.id, reason="closed after being left unwatched")
            elif not terminal.running and terminal.ended_at is not None \
                    and now - terminal.ended_at >= config.EXITED_KEEP_SECONDS and not terminal.viewers:
                self._terminals.pop(terminal.id, None)

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and not self._reaper.done():
            return
        self._reaper = asyncio.get_running_loop().create_task(
            run_forever(REAPER_NAME, self.reap, interval_s=lambda: REAPER_INTERVAL_SECONDS,
                        startup_delay_s=REAPER_INTERVAL_SECONDS, logger=log))
        background.register(REAPER_NAME, self._reaper)


registry = Registry()
