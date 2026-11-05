"""Run ``git clone --progress`` and say what it is doing, and why it failed.

Three pieces, none of which knows about projects or staging folders
(``services/project_management.py`` owns those):

- :func:`run_git_clone` runs the command through
  :func:`utils.commands.run_watched` (its own process group), reads its
  stderr as it arrives and reports progress through a callback. It stops git
  when nothing has come from it for ``idle_timeout`` seconds (a stalled
  transfer), not after a fixed wall-clock time, so a large repository that
  keeps moving is never cut off. Cancelling the call stops git's whole tree
  (SIGTERM, then SIGKILL) and reaps it before the cancellation travels on.
- :class:`CloneProgress` parses git's progress lines (``Receiving objects:
  45% (450/1000), 1.20 MiB | 2.00 MiB/s``) into a phase, the phase's percent,
  an overall percent that only goes up, and the bytes received.
- :func:`classify_failure` turns a failed run into one of the error codes the
  frontend acts on, with git's last stderr line as ``detail``, scrubbed of
  credentials.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from utils.commands import run_watched

log = logging.getLogger(__name__)

#: Seconds with no output from git before the clone counts as stalled.
CLONE_IDLE_TIMEOUT = 90.0

_LINE_KEEP = 500          # characters kept of one stderr line
_TAIL_LINES = 20          # non-progress stderr lines kept for the error
_BUFFER_MAX = 64 * 1024   # a "line" longer than this is cut, never buffered for ever
_DETAIL_MAX = 300

PHASES = ("counting", "receiving", "resolving", "checkout", "finishing")

# Each phase's share of the overall percent: [start, end). Receiving is most
# of the wall-clock time of a clone, so it gets most of the bar.
_SPAN = {
    "counting": (0, 5),
    "receiving": (5, 80),
    "resolving": (80, 92),
    "checkout": (92, 99),
    "finishing": (99, 99),
}

# The server's three steps before any data moves, as shares of the counting
# phase's own percent, so it climbs once instead of restarting at 0 for each.
_COUNTING_STEPS = {
    "enumerating objects": (0, 20),
    "counting objects": (20, 40),
    "compressing objects": (40, 100),
}

_LABEL_PHASE = {
    "enumerating objects": "counting",
    "counting objects": "counting",
    "compressing objects": "counting",
    "total": "counting",
    "receiving objects": "receiving",
    "resolving deltas": "resolving",
    "checking connectivity": "resolving",
    "updating files": "checkout",
    "checking out files": "checkout",
    "filtering content": "checkout",
}

# "remote: Counting objects:  45% (12/26)", "Receiving objects: 100% (5/5), 1.2 MiB | 3 MiB/s, done."
_PERCENT_RE = re.compile(
    r"^(?:remote:\s*)?(?P<label>[A-Za-z][A-Za-z ]*?):\s+(?P<pct>\d{1,3})%\s*\((?P<done>\d+)/(?P<total>\d+)\)(?P<rest>.*)$"
)
# "remote: Enumerating objects: 1234, done." / "remote: Enumerating objects: 1234"
_COUNT_RE = re.compile(r"^(?:remote:\s*)?(?P<label>[A-Za-z][A-Za-z ]*?):\s+(?P<done>\d+)(?:,\s*done\.?)?\s*$")
_SIZE_RE = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>bytes|KiB|MiB|GiB|TiB)\b")
_UNITS = {"bytes": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def _size_bytes(text: str) -> int | None:
    """The first size in ``text`` (the amount received; the rate comes after the ``|``)."""
    match = _SIZE_RE.search(text.split("|", 1)[0])
    if not match:
        return None
    return int(float(match["n"]) * _UNITS[match["unit"]])


@dataclass
class CloneProgress:
    """Where a clone is. ``percent`` is overall (0-100, never goes down);
    ``phase_percent`` is the current phase's own, or ``None`` before git has
    said."""

    phase: str = "counting"
    phase_percent: int | None = None
    percent: int = 0
    received_bytes: int = 0

    def feed(self, line: str) -> bool:
        """Read one stderr line. ``True`` when it was a progress line."""
        line = line.strip()
        match = _PERCENT_RE.match(line)
        if match:
            phase = _LABEL_PHASE.get(match["label"].strip().lower())
            if phase is None:
                return True  # progress of something we don't map: still progress
            pct = self._step(match["label"], max(0, min(100, int(match["pct"]))))
            size = _size_bytes(match["rest"]) if phase == "receiving" else None
            if size is not None:
                self.received_bytes = max(self.received_bytes, size)
            self._move(phase, pct)
            return True
        match = _COUNT_RE.match(line)
        if match and match["label"].strip().lower() in _LABEL_PHASE:
            phase = _LABEL_PHASE[match["label"].strip().lower()]
            if "done" in line:
                self._move(phase, self._step(match["label"], 100))
            return True
        return False

    @staticmethod
    def _step(label: str, pct: int) -> int:
        """``pct`` of one counting step as the counting phase's percent."""
        step = _COUNTING_STEPS.get(label.strip().lower())
        if step is None:
            return pct
        start, end = step
        return start + ((end - start) * pct) // 100

    def finishing(self) -> None:
        self._move("finishing", None)

    def _move(self, phase: str, pct: int | None) -> None:
        # Never step back: "remote: Total ..." can arrive after receiving starts.
        if PHASES.index(phase) < PHASES.index(self.phase):
            return
        if phase == self.phase and pct is not None and self.phase_percent is not None:
            pct = max(pct, self.phase_percent)
        self.phase, self.phase_percent = phase, pct
        start, end = _SPAN[phase]
        overall = start + ((end - start) * (pct or 0)) // 100
        self.percent = max(self.percent, min(99, overall))

    def as_dict(self) -> dict:
        return {"phase": self.phase, "phase_percent": self.phase_percent,
                "percent": self.percent, "received_bytes": self.received_bytes}


@dataclass
class GitCloneResult:
    returncode: int
    #: The last non-progress stderr lines, oldest first.
    stderr_tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    binary_missing: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.binary_missing


async def run_git_clone(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    on_progress: Callable[[CloneProgress], None] | None = None,
    idle_timeout: float = CLONE_IDLE_TIMEOUT,
) -> GitCloneResult:
    """Run a ``git clone --progress`` argv; never raises except on cancellation.

    The process itself is :func:`utils.commands.run_watched`'s: its own
    group, stopped when silent for ``idle_timeout``, stopped and reaped when
    this call is cancelled.
    """
    progress = CloneProgress()
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    buffer = b""

    def take(raw: bytes) -> None:
        text = raw.decode(errors="replace").strip()
        if not text:
            return
        if not progress.feed(text):
            tail.append(text[:_LINE_KEEP])
        # Every line, progress or not: the caller also learns git is alive.
        if on_progress is not None:
            on_progress(progress)

    def on_stderr(chunk: bytes) -> None:
        nonlocal buffer
        buffer += chunk
        # Progress lines end in \r (redrawn in place), the rest in \n.
        *lines, buffer = re.split(rb"[\r\n]", buffer)
        for raw in lines:
            take(raw)
        if len(buffer) > _BUFFER_MAX:
            take(buffer[:_LINE_KEEP])
            buffer = b""

    result = await run_watched(argv, on_stderr=on_stderr, idle_timeout=idle_timeout, cwd=cwd, env=env,
                               log_label="git clone")
    take(buffer)
    if result.binary_missing:
        return GitCloneResult(returncode=-1, stderr_tail=["git is not installed on this server"], binary_missing=True)
    if result.exception:
        tail.append(f"could not start git: {result.exception}"[:_LINE_KEEP])
    return GitCloneResult(returncode=result.returncode, stderr_tail=list(tail), timed_out=result.timed_out)


# ── Errors ────────────────────────────────────────────────────────────────

#: Code, HTTP status and the message the person sees.
FAILURES: dict[str, tuple[int, str]] = {
    "repo_not_found_or_private": (404, "The repository was not found, or it is private and this Space can't "
                                       "read it. Check the URL, or connect GitHub with an account that has access."),
    "auth_failed": (502, "The Git server refused the credentials this Space sent. Reconnect GitHub and try again."),
    "host_unreachable": (502, "Could not reach the Git server. Check the URL and this machine's network connection."),
    "timed_out": (504, "Cloning stopped making progress, so it was stopped. Try again."),
    "clone_failed": (502, "Could not clone the repository."),
}

_SSH_AUTH_MESSAGE = ("The Git server refused this machine's SSH key. Use the HTTPS URL, or add this "
                     "machine's SSH key to your account.")
_HOST_KEY_MESSAGE = ("This machine doesn't know the Git server's SSH host key yet, so it refused to connect. "
                     "Use the HTTPS URL instead.")

_NOT_FOUND = ("repository not found", "could not read username", "could not read password",
              "does not appear to be a git repository", "returned error: 404")
_REFUSED = ("authentication failed", "invalid username or password", "invalid username or token",
            "returned error: 401", "returned error: 403", "bad credentials")
_SSH_REFUSED = ("permission denied (publickey", "permission denied, please try again")
_UNREACHABLE = ("could not resolve host", "could not resolve hostname", "name or service not known",
                "temporary failure in name resolution", "nodename nor servname", "failed to connect to",
                "connection refused", "network is unreachable", "no route to host", "connection timed out",
                "couldn't connect to server", "connection reset by peer", "could not connect to server")
_REPO_404_RE = re.compile(r"repository '.*' not found")

_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*(?:basic|bearer|token)\s+)\S+")
_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|glpat-[A-Za-z0-9_-]+)\b")
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_REDACTED = "[REDACTED]"


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` without credentials: the given secrets, auth headers, token
    shapes and URL userinfo."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    text = _AUTH_HEADER_RE.sub(rf"\1{_REDACTED}", text)
    text = _TOKEN_RE.sub(_REDACTED, text)
    return _USERINFO_RE.sub(rf"\1{_REDACTED}@", text)


# What git adds after the cause over SSH; never the reason itself.
_BOILERPLATE = ("fatal: could not read from remote repository.", "please make sure you have the correct access rights",
                "and the repository exists.")


def _last_line(lines: list[str]) -> str | None:
    """The line that says why: git's last ``fatal:``/``error:``, else its last."""
    lines = [line for line in lines if line.strip().lower() not in _BOILERPLATE] or lines
    for line in reversed(lines):
        if line.lower().startswith(("fatal:", "error:")):
            return line
    return lines[-1] if lines else None


def classify_failure(result: GitCloneResult, *, credential_sent: bool, ssh: bool) -> tuple[str, str]:
    """``(code, message)`` for a failed run; see :data:`FAILURES`."""
    if result.timed_out:
        return "timed_out", FAILURES["timed_out"][1]
    text = "\n".join(result.stderr_tail).lower()
    if any(s in text for s in _UNREACHABLE):
        return "host_unreachable", FAILURES["host_unreachable"][1]
    if ssh and "host key verification failed" in text:
        return "clone_failed", _HOST_KEY_MESSAGE
    if ssh and any(s in text for s in _SSH_REFUSED):
        return "auth_failed", _SSH_AUTH_MESSAGE
    if any(s in text for s in _REFUSED):
        # A token that was sent and refused is the person's to fix; with no
        # token, the server is saying "you can't see this".
        if credential_sent:
            return "auth_failed", FAILURES["auth_failed"][1]
        return "repo_not_found_or_private", FAILURES["repo_not_found_or_private"][1]
    if any(s in text for s in _NOT_FOUND) or _REPO_404_RE.search(text):
        return "repo_not_found_or_private", FAILURES["repo_not_found_or_private"][1]
    return "clone_failed", FAILURES["clone_failed"][1]


def failure_detail(result: GitCloneResult, *, secrets: Iterable[str] = (),
                   replace: Iterable[tuple[str, str]] = ()) -> str | None:
    """Git's last stderr line, scrubbed and capped; ``replace`` swaps private
    paths for what the person knows them as."""
    line = _last_line(result.stderr_tail)
    if not line:
        return None
    for old, new in replace:
        if old:
            line = line.replace(old, new)
    line = scrub(line, secrets)
    return line if len(line) <= _DETAIL_MAX else line[:_DETAIL_MAX - 1] + "…"
