"""
Subprocess command-runner utility.

A small wrapper over `asyncio.create_subprocess_exec` (and a sync sibling
over `subprocess.run`) so new code that needs to shell out — to the agent
CLI or anything else — has a single, consistent entry point with:

* timeout enforcement (kills the process instead of hanging forever),
* captured stdout+stderr (merged, so call sites can log one stream),
* optional append-to-log-file for background provisioning flows,
* structured `CommandResult` return type (no bare ints floating around).

Designed to pair with `services.cowork_agent.registry.agent_registry.AgentManifest.command`,
which renders templated argvs from the manifest — those argvs go directly
into `run` / `run_sync` here.

Examples
--------
    # Async (inside a route handler or background task):
    from utils.commands import run
    from services.cowork_agent.registry.agent_registry import get_active_agent

    agent = get_active_agent()
    argv = agent.command("models_set", model="anthropic/claude-opus-4.6")
    result = await run(argv, cwd=agent.cwd, timeout=agent.cli_timeout_seconds)
    if not result.ok:
        log.warning("cli failed: %s", result.output)

    # Sync (scripts, startup checks):
    from utils.commands import run_sync
    result = run_sync(["git", "rev-parse", "HEAD"])
    print(result.output.strip())

    # With a log file (background provisioning style):
    await run(argv, cwd=agent.cwd, log_path=agent.provisioning_log,
              log_label=f"provisioning: {provider_id}")

Security
--------
These helpers ONLY use `create_subprocess_exec` / `subprocess.run` with a
list argv — never `shell=True`. Do not add a `shell=True` path; callers
should pre-render argvs (e.g. via manifest command templates) so user
input never reaches a shell interpreter.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from utils.runtime_env import inbox_activity_dir, logs_dir, quirq_state_dir

log = logging.getLogger(__name__)

_COMMAND_LOG_MAX_BYTES = 5 * 1024 * 1024
_COMMAND_LOG_ENTRY_CAP_CHARS = 4096
#: Full generations of the shared log are kept here, one file per rotation,
#: and nothing deletes them: disk use grows with how much the server runs.
COMMAND_LOG_ARCHIVE_DIR = "archive"
_REDACTED = "[REDACTED]"
_SENSITIVE_FLAGS = frozenset({
    "--access-token",
    "--api-key",
    "--auth-token",
    "--code",
    "--password",
    "--secret",
    "--token",
})
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*(?:basic|bearer|token)\s+)(\S+)")
_INLINE_SECRET_RE = re.compile(
    r"(?i)(--(?:access-token|api-key|auth-token|code|password|secret|token)\b\s*[=:]\s*)(\S+)"
)
_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:ghp_[A-Za-z0-9_]+|gho_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+|ak_[A-Za-z0-9_-]+)\b"
)
# One lock for everything the log writer touches: the size check, the
# rotation rename and the append happen as one step, so two threads finishing
# commands together (scheduler jobs, a request shelling out) cannot both
# rotate the same file. It is held for one small append, never while a
# process runs. The warned set makes "unwritable log" a one-line warning per
# destination; writes are always retried, so a transient failure costs one
# entry rather than the rest of the record.
_COMMAND_LOG_LOCK = threading.Lock()
_WARNED_COMMAND_LOG_PATHS: set[str] = set()


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess run.

    `returncode` is -1 when no process ran or its code is unknown (binary not
    found, a local exception). On a timeout it is what `subprocess` reports
    for the kill, e.g. -9, with `timed_out` set. Check `ok` rather than
    testing for 0 directly when you want "finished cleanly".
    """

    argv: list[str]
    returncode: int
    output: str  # stdout (+ stderr merged unless separate_stderr was requested)
    duration_seconds: float
    timed_out: bool = False
    binary_missing: bool = False
    exception: str | None = None
    stderr: str = ""  # populated only when separate_stderr=True

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.binary_missing

    @property
    def stdout(self) -> str:
        """Alias for `output`, so call sites ported from `subprocess.run`
        keep reading `.stdout` / `.stderr` / `.returncode`."""
        return self.output


def _redact_text(text: str) -> str:
    redacted = _AUTH_HEADER_RE.sub(rf"\1{_REDACTED}", text)
    redacted = _INLINE_SECRET_RE.sub(rf"\1{_REDACTED}", redacted)
    return _TOKEN_PREFIX_RE.sub(_REDACTED, redacted)


def _is_sensitive_flag(token: str) -> bool:
    name = token.split("=", 1)[0].lower()
    return name in _SENSITIVE_FLAGS


def _redact_argv(argv: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for token in argv:
        token = str(token)
        if redact_next:
            redacted.append(_REDACTED)
            redact_next = False
            continue
        if _is_sensitive_flag(token):
            if "=" in token:
                redacted.append(_redact_text(token))
            else:
                redacted.append(token)
                redact_next = True
            continue
        redacted.append(_redact_text(token))
    return redacted


def _cap_log_output(text: str) -> str:
    if len(text) <= _COMMAND_LOG_ENTRY_CAP_CHARS:
        return text
    marker = f"\n...[truncated {len(text) - _COMMAND_LOG_ENTRY_CAP_CHARS} chars]...\n"
    if len(marker) >= _COMMAND_LOG_ENTRY_CAP_CHARS:
        return marker[:_COMMAND_LOG_ENTRY_CAP_CHARS]
    keep = _COMMAND_LOG_ENTRY_CAP_CHARS - len(marker)
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + text[-tail:]


def _render_output(result: CommandResult, *, cap: bool = True) -> str:
    if result.stderr:
        combined = f"{result.output}[stderr]\n{result.stderr}" if result.output else f"[stderr]\n{result.stderr}"
    else:
        combined = result.output
    redacted = _redact_text(combined)
    return _cap_log_output(redacted) if cap else redacted


def _command_log_status(result: CommandResult) -> str:
    return (
        "timeout" if result.timed_out
        else "missing-binary" if result.binary_missing
        else "exception" if result.exception is not None
        else str(result.returncode)
    )


def _default_command_logging_enabled() -> bool:
    return ((os.getenv("QUIRQ_COMMAND_LOG", "") or "").strip().lower() != "off")


def _default_command_log_path() -> Path | None:
    if not _default_command_logging_enabled():
        return None
    override = (os.getenv("QUIRQ_COMMAND_LOG_PATH", "") or "").strip()
    if override:
        return Path(override).expanduser()
    # Earlier releases kept commands.log at the top of the state root, then in
    # logs/. Keep appending to an old copy until the server's boot migration
    # moves it, so a command run before that, or by an older server sharing
    # the root, never splits the log in two.
    new = inbox_activity_dir() / "commands.log"
    if not new.exists():
        for old in _legacy_command_log_paths():
            if old.is_file():
                return old
    return new


def _legacy_command_log_paths() -> tuple[Path, ...]:
    """Where earlier releases kept the shared log, newest layout first."""
    return logs_dir() / "commands.log", quirq_state_dir() / "commands.log"


def _iter_log_paths(log_path: str | Path | None) -> list[tuple[Path, bool]]:
    """Destinations for one entry as (path, is_shared). The shared file is the
    runner's own commands.log: capped per entry and rotated into `archive/`,
    because every command in the process feeds it. A caller's explicit
    `log_path` is that caller's complete record (a scheduler job, a
    provisioning run): redacted, but never capped or rotated — retention there
    is the caller's business."""
    paths: list[tuple[Path, bool]] = []
    default_path = _default_command_log_path()
    if default_path is not None:
        paths.append((default_path, True))
    if log_path is not None:
        explicit_path = Path(log_path).expanduser()
        if default_path is None or os.path.abspath(str(explicit_path)) != os.path.abspath(str(default_path)):
            paths.append((explicit_path, False))
    return paths


def _warn_logging_failed(path: Path, exc: Exception) -> None:
    key = os.path.abspath(str(path))
    with _COMMAND_LOG_LOCK:
        if key in _WARNED_COMMAND_LOG_PATHS:
            return
        _WARNED_COMMAND_LOG_PATHS.add(key)
    log.warning("command log entry dropped, cannot write %s: %s", path, exc)


def _emit_logs(
    *,
    ts: str,
    label: str,
    argv: Sequence[str],
    cwd: str | Path | None,
    result: CommandResult,
    log_path: str | Path | None,
) -> None:
    for path, shared in _iter_log_paths(log_path):
        try:
            entry = _render_log_entry(ts, label, argv, result, cwd=cwd, cap=shared)
            _write_log(path, entry, rotate=shared)
        except Exception as exc:  # noqa: BLE001 - logging must never affect command execution
            _warn_logging_failed(path, exc)


def _render_log_entry(
    ts: str,
    label: str,
    argv: Sequence[str],
    result: CommandResult,
    *,
    cwd: str | Path | None = None,
    cap: bool = True,
) -> str:
    header = f"\n=== {ts} {label} ===\n" if label else f"\n=== {ts} ===\n"
    cmdline = " ".join(repr(a) if " " in a else a for a in _redact_argv(argv))
    output = _render_output(result, cap=cap)
    if output and not output.endswith("\n"):
        output += "\n"
    where = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
    return f"{header}$ {cmdline}\ncwd: {where}\n[{_command_log_status(result)}; {result.duration_seconds:.3f}s]\n{output}"


def _text(value) -> str:
    """bytes or str or None -> str (subprocess hands back bytes when we do
    not ask for text mode, which we cannot when passing binary stdin)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Start a process and do not wait for it: its own session, stdio to
    /dev/null. For restart/update scripts that outlive this server. The
    result only says whether the spawn itself worked."""
    if not argv:
        raise ValueError("argv must be non-empty")
    argv_list = [str(a) for a in argv]
    try:
        subprocess.Popen(
            argv_list,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        result = CommandResult(argv=argv_list, returncode=-1, output=f"{argv_list[0]} not found in PATH",
                               duration_seconds=0.0, binary_missing=True)
        _emit_logs(ts=datetime.now(timezone.utc).isoformat(), label="", argv=argv_list, cwd=cwd, result=result, log_path=None)
        return result
    except Exception as e:  # noqa: BLE001
        result = CommandResult(argv=argv_list, returncode=-1, output=f"[exception] {e}",
                               duration_seconds=0.0, exception=str(e))
        _emit_logs(ts=datetime.now(timezone.utc).isoformat(), label="", argv=argv_list, cwd=cwd, result=result, log_path=None)
        return result
    result = CommandResult(argv=argv_list, returncode=0, output="", duration_seconds=0.0)
    _emit_logs(ts=datetime.now(timezone.utc).isoformat(), label="detached", argv=argv_list, cwd=cwd, result=result, log_path=None)
    return result


def _archive_dir_for(log_path: Path) -> Path:
    """``<log folder>/archive/``, except for a log still at an old path: its
    archive goes to ``inbox/activity/archive/`` with the current log's, rather
    than starting a second archive in ``logs/`` or loose in the state root."""
    if log_path in _legacy_command_log_paths():
        return inbox_activity_dir() / COMMAND_LOG_ARCHIVE_DIR
    return log_path.parent / COMMAND_LOG_ARCHIVE_DIR


def archive_path_for(log_path: Path, at: datetime) -> Path:
    """``archive/commands.<stamp>.log``: the first free name at or after ``at``.

    The stamp is UTC to the second, like the visualizer's rotated timeline
    segments, so plain name order is chronological order. A stamp already taken
    steps forward a second instead of gaining a suffix, which would sort before
    the name it was meant to follow.
    """
    directory = _archive_dir_for(log_path)
    when = at.astimezone(timezone.utc).replace(microsecond=0)
    while True:
        candidate = directory / f"{log_path.stem}.{when.strftime('%Y%m%dT%H%M%SZ')}{log_path.suffix}"
        if not candidate.exists():
            return candidate
        when += timedelta(seconds=1)


def _write_log(log_path: Path, entry: str, *, rotate: bool = False) -> None:
    entry_bytes = entry.encode("utf-8")
    with _COMMAND_LOG_LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if rotate and log_path.exists() and log_path.stat().st_size + len(entry_bytes) > _COMMAND_LOG_MAX_BYTES:
            archived = archive_path_for(log_path, datetime.now(timezone.utc))
            archived.parent.mkdir(parents=True, exist_ok=True)
            log_path.replace(archived)
        with log_path.open("ab") as f:
            f.write(entry_bytes)


def _kill_tree(proc) -> None:
    """Kill the child and, on POSIX, everything it spawned.

    Both runners start each child in its own session, so its pid is also its process
    group id and one signal reaches the grandchildren too. Without that, a
    killed `npx` or `git` can leave a helper process holding the stdout pipe,
    and `communicate()` then waits for that helper instead of honouring the
    timeout (measured: 7.5 s of a 0.5 s timeout). ProcessLookupError means the
    tree is already gone, which is the outcome we wanted.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        pid = getattr(proc, "pid", None)
        if os.name == "posix" and pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            proc.kill()


async def run(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
    input: bytes | None = None,
    separate_stderr: bool = False,
    inherit_output: bool = False,
) -> CommandResult:
    """Run a command asynchronously and return a `CommandResult`.

    Parameters
    ----------
    argv:       command + args as a list — never a string (no shell).
    cwd:        working directory for the child process.
    timeout:    seconds before the runner kills the process. `None` = no timeout.
    env:        environment overrides; unset → inherit parent.
    log_path:   if provided, append a formatted log entry after the run.
    log_label:  prefix for the log entry (e.g. "provisioning: anthropic").
    input:      bytes written to the child's stdin (a passphrase, a payload);
                without it stdin is /dev/null so nothing can hang on a prompt.
    separate_stderr:  keep stderr apart (`result.stderr`) instead of merging it
                into `output`. For callers that parse stdout.
    inherit_output:   do not capture at all — the child writes straight to
                this process's stdout/stderr (long setup scripts whose progress
                must be visible live). `output` is then empty.
    """
    if not argv:
        raise ValueError("argv must be non-empty")

    argv_list = [str(a) for a in argv]
    ts = datetime.now(timezone.utc).isoformat()
    started = asyncio.get_event_loop().time()

    result: CommandResult
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv_list,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=None if inherit_output else asyncio.subprocess.PIPE,
            stderr=None if inherit_output
                   else (asyncio.subprocess.PIPE if separate_stderr else asyncio.subprocess.STDOUT),
            # own session => own process group, so a timeout can kill the
            # whole tree (see _kill_tree). Trade-off: a child no longer dies
            # with the server on Ctrl+C, so long-running calls pass a timeout.
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"{argv_list[0]} not found in PATH",
            duration_seconds=0.0,
            binary_missing=True,
        )
        _emit_logs(ts=ts, label=log_label, argv=argv_list, cwd=cwd, result=result, log_path=log_path)
        return result
    except Exception as e:  # noqa: BLE001 — surface as CommandResult, never raise
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[exception] {e}",
            duration_seconds=0.0,
            exception=str(e),
        )
        _emit_logs(ts=ts, label=log_label, argv=argv_list, cwd=cwd, result=result, log_path=log_path)
        return result

    try:
        if timeout is not None:
            stdout, stderr = await asyncio.wait_for(proc.communicate(input=input), timeout=timeout)
        else:
            stdout, stderr = await proc.communicate(input=input)
    except asyncio.TimeoutError:
        # Kill the whole tree; _kill_tree swallows the ProcessLookupError that
        # asyncio raises when the child exited in the instant between the
        # timeout firing and the kill (the runner promises never to raise).
        _kill_tree(proc)
        await proc.communicate()
        result = CommandResult(
            argv=argv_list,
            returncode=proc.returncode if proc.returncode is not None else -1,
            output=f"[timed out after {timeout}s]",
            duration_seconds=asyncio.get_event_loop().time() - started,
            timed_out=True,
        )
        _emit_logs(ts=ts, label=log_label, argv=argv_list, cwd=cwd, result=result, log_path=log_path)
        return result

    duration = asyncio.get_event_loop().time() - started
    result = CommandResult(
        argv=argv_list,
        returncode=proc.returncode if proc.returncode is not None else -1,
        output=(stdout or b"").decode(errors="replace"),
        duration_seconds=duration,
        stderr=(stderr or b"").decode(errors="replace") if separate_stderr else "",
    )
    _emit_logs(ts=ts, label=log_label, argv=argv_list, cwd=cwd, result=result, log_path=log_path)
    return result


def run_sync(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
    input: bytes | None = None,
    separate_stderr: bool = False,
    inherit_output: bool = False,
) -> CommandResult:
    """Synchronous sibling of `run` — for scripts, startup probes, or tests.
    Same options as `run`.

    Do NOT call this from inside an async handler — it will block the
    event loop. Use `run` there.
    """
    if not argv:
        raise ValueError("argv must be non-empty")

    argv_list = [str(a) for a in argv]
    ts = datetime.now(timezone.utc).isoformat()
    import time
    started = time.monotonic()

    timed_out = False
    try:
        with subprocess.Popen(
            argv_list,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=None if inherit_output else subprocess.PIPE,
            stderr=None if inherit_output else subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        ) as proc:
            try:
                stdout, stderr = proc.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                # Match the async runner: killing just the direct child can
                # leave a scheduled command's helpers running and holding pipes.
                _kill_tree(proc)
                stdout, stderr = proc.communicate()
                timed_out = True
            except BaseException:
                _kill_tree(proc)
                proc.wait()
                raise
            completed = subprocess.CompletedProcess(argv_list, proc.returncode, stdout, stderr)
    except FileNotFoundError:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"{argv_list[0]} not found in PATH",
            duration_seconds=0.0,
            binary_missing=True,
        )
        _emit_logs(ts=ts, label=log_label, argv=argv_list, cwd=cwd, result=result, log_path=log_path)
        return result
    except Exception as e:  # noqa: BLE001
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[exception] {e}",
            duration_seconds=time.monotonic() - started,
            exception=str(e),
        )
        _emit_logs(ts=ts, label=log_label, argv=argv_list, cwd=cwd, result=result, log_path=log_path)
        return result

    out = _text(completed.stdout)
    err = _text(completed.stderr)
    result = CommandResult(
        argv=argv_list,
        returncode=completed.returncode,
        output=(out if separate_stderr else out + err) + (f"[timed out after {timeout}s]" if timed_out else ""),
        duration_seconds=time.monotonic() - started,
        timed_out=timed_out,
        stderr=err if separate_stderr else "",
    )
    _emit_logs(ts=ts, label=log_label, argv=argv_list, cwd=cwd, result=result, log_path=log_path)
    return result


async def run_chain(
    argvs: Sequence[Sequence[str]],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
    abort_on_failure: bool = True,
) -> list[CommandResult]:
    """Run a sequence of commands, optionally aborting on the first failure.

    Matches the provider/channel provisioning pattern — batch first, then
    post-commands — so those call sites can migrate to this helper later
    without reshaping their control flow.
    """
    results: list[CommandResult] = []
    for argv in argvs:
        result = await run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            log_path=log_path,
            log_label=log_label,
        )
        results.append(result)
        if not result.ok and abort_on_failure:
            if log_path is not None:
                _write_log(
                    Path(log_path),
                    f"[chain aborted{' for ' + log_label if log_label else ''} at: {' '.join(argv)}]\n",
                )
            break
    return results


# =============================================================================
# The one door: a JSON-shaped command spec
# =============================================================================
#
# Everything above takes a Python list. Config files (the skill catalog, agent
# manifests, future automation) describe commands as data, so this is the
# shape they use and the validation they all get:
#
#     {"argv": ["npm", "install", "-g", "@okxweb3/a2a-node"],
#      "cwd": "/home/coder", "env": {"CI": "1"}, "timeout": 300}
#
# `argv` is the preferred form. A `command` string is accepted only as a
# convenience for hand-written config (the skill catalog): `split_command`
# turns it into an argv with POSIX quoting and refuses `&&`, `|`, `;`,
# redirections and `$()`, so it never reaches a shell — which is where command
# injection (CWE-78) happens. A value that must come from user input goes into
# ONE argv slot via `safe_arg`, which refuses anything that a program would
# read as an option (argument injection).

SHELL_OPERATORS = ("&&", "||", "|", ";", ">", "<", "`", "$(", "\n")


class CommandSpecError(ValueError):
    """A command spec that must not run: malformed, or trying to reach a shell."""


def safe_arg(value: Any, *, allow_option: bool = False) -> str:
    """Return `value` as one argv element, or raise CommandSpecError.

    Refuses empty strings, NUL bytes, and (unless `allow_option`) anything
    starting with '-' so an attacker-controlled repo name, branch, or path can
    never become `--upload-pack=...` or `-c core.sshCommand=...`. Callers that
    legitimately pass a flag pass it as a literal in their own argv, not
    through this function.
    """
    if not isinstance(value, str) or not value:
        raise CommandSpecError("argument must be a non-empty string")
    if "\x00" in value:
        raise CommandSpecError("argument contains a NUL byte")
    if not allow_option and value.startswith("-"):
        raise CommandSpecError(f"argument {value!r} looks like an option; refuse it or pass it after '--'")
    return value


def split_command(template: str) -> list[str]:
    """Turn a human-written command line into argv WITHOUT a shell.

    POSIX quoting rules (shlex), so `--dir "{skills_dir}"` stays one token.
    Refuses anything a shell would interpret as more than one command or as a
    redirection: those need a real shell, and this codebase does not run one.
    """
    if not isinstance(template, str) or not template.strip():
        raise CommandSpecError("command must be a non-empty string")
    for op in SHELL_OPERATORS:
        if op in template:
            raise CommandSpecError(
                f"command contains shell operator {op!r}; write it as separate steps or an argv list")
    try:
        argv = shlex.split(template, posix=True)
    except ValueError as exc:
        raise CommandSpecError(f"unbalanced quoting in command: {exc}") from exc
    if not argv:
        raise CommandSpecError("command is empty after parsing")
    return argv


@dataclass(frozen=True)
class CommandSpec:
    """One command as data. Build it with `from_json` so every field is
    validated once, in one place, before anything runs."""

    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout: float | None = None
    log_path: str | None = None
    log_label: str = ""

    ALLOWED_KEYS = frozenset({"argv", "command", "cwd", "env", "timeout", "log_path", "log_label"})

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> "CommandSpec":
        if not isinstance(obj, Mapping):
            raise CommandSpecError("command spec must be an object")
        unknown = set(obj) - cls.ALLOWED_KEYS
        if unknown:
            raise CommandSpecError(f"unknown command spec keys: {sorted(unknown)}")
        if "argv" in obj and "command" in obj:
            raise CommandSpecError("give argv or command, not both")
        if "argv" in obj:
            argv = obj["argv"]
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise CommandSpecError("argv must be a non-empty list of non-empty strings")
            if any("\x00" in a for a in argv):
                raise CommandSpecError("argv contains a NUL byte")
            argv = list(argv)
        elif "command" in obj:
            argv = split_command(obj["command"])
        else:
            raise CommandSpecError("command spec needs argv (preferred) or command")
        # Both input forms share the same argument checks. Flags are valid
        # arguments, but the executable itself cannot be an option.
        argv = [safe_arg(arg, allow_option=index > 0) for index, arg in enumerate(argv)]
        cwd = obj.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd):
            raise CommandSpecError("cwd must be a non-empty string")
        env = obj.get("env")
        if env is not None and (not isinstance(env, Mapping)
                                or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
            raise CommandSpecError("env must map strings to strings")
        timeout = obj.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            raise CommandSpecError("timeout must be a positive number of seconds")
        log_path = obj.get("log_path")
        if log_path is not None and not isinstance(log_path, str):
            raise CommandSpecError("log_path must be a string")
        log_label = obj.get("log_label", "")
        if not isinstance(log_label, str):
            raise CommandSpecError("log_label must be a string")
        return cls(argv=argv, cwd=cwd, env=dict(env) if env is not None else None,
                   timeout=float(timeout) if timeout is not None else None,
                   log_path=log_path, log_label=log_label)

    def with_argv(self, argv: Sequence[str]) -> "CommandSpec":
        """Same spec, different argv (used after placeholder expansion)."""
        return CommandSpec(argv=[str(a) for a in argv], cwd=self.cwd, env=self.env,
                           timeout=self.timeout, log_path=self.log_path, log_label=self.log_label)


async def run_spec(spec: CommandSpec, **options: Any) -> CommandResult:
    """Run a validated spec. Config-driven callers (catalog, manifests) come
    through here; Python callers with a literal argv may call `run` directly.
    `options` are the runner's capture switches (`separate_stderr`, `input`,
    `inherit_output`): how to capture, never what to run."""
    return await run(spec.argv, cwd=spec.cwd, timeout=spec.timeout, env=spec.env,
                     log_path=spec.log_path, log_label=spec.log_label, **options)


def run_spec_sync(spec: CommandSpec, **options: Any) -> CommandResult:
    return run_sync(spec.argv, cwd=spec.cwd, timeout=spec.timeout, env=spec.env,
                    log_path=spec.log_path, log_label=spec.log_label, **options)
