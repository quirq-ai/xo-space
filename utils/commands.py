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
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess run.

    `returncode` is -1 when the process was killed by the runner (timeout,
    binary-not-found, or another local exception) — check `ok` rather
    than testing for 0 directly when you want "finished cleanly".
    """

    argv: list[str]
    returncode: int
    output: str  # stdout + stderr, merged
    duration_seconds: float
    timed_out: bool = False
    binary_missing: bool = False
    exception: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.binary_missing


def _render_log_entry(ts: str, label: str, argv: Sequence[str], result: CommandResult) -> str:
    header = f"\n=== {ts} {label} ===\n" if label else f"\n=== {ts} ===\n"
    cmdline = " ".join(repr(a) if " " in a else a for a in argv)
    rc = (
        "timeout" if result.timed_out
        else "missing-binary" if result.binary_missing
        else "exception" if result.exception is not None
        else str(result.returncode)
    )
    tail = result.output
    if tail and not tail.endswith("\n"):
        tail += "\n"
    return f"{header}$ {cmdline}\n{tail}[exit {rc}]\n"


def _write_log(log_path: Path, entry: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(entry)


async def run(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"{argv_list[0]} not found in PATH",
            duration_seconds=0.0,
            binary_missing=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result
    except Exception as e:  # noqa: BLE001 — surface as CommandResult, never raise
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[exception] {e}",
            duration_seconds=0.0,
            exception=str(e),
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result

    try:
        if timeout is not None:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        else:
            stdout, _ = await proc.communicate()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[timed out after {timeout}s]",
            duration_seconds=asyncio.get_event_loop().time() - started,
            timed_out=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result

    duration = asyncio.get_event_loop().time() - started
    result = CommandResult(
        argv=argv_list,
        returncode=proc.returncode if proc.returncode is not None else -1,
        output=stdout.decode(errors="replace"),
        duration_seconds=duration,
    )
    if log_path is not None:
        _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
    return result


def run_sync(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
) -> CommandResult:
    """Synchronous sibling of `run` — for scripts, startup probes, or tests.

    Do NOT call this from inside an async handler — it will block the
    event loop. Use `run` there.
    """
    if not argv:
        raise ValueError("argv must be non-empty")

    argv_list = [str(a) for a in argv]
    ts = datetime.now(timezone.utc).isoformat()
    import time
    started = time.monotonic()

    try:
        completed = subprocess.run(
            argv_list,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"{argv_list[0]} not found in PATH",
            duration_seconds=0.0,
            binary_missing=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result
    except subprocess.TimeoutExpired as e:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=(e.stdout or "") + (e.stderr or "") + f"\n[timed out after {timeout}s]",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result
    except Exception as e:  # noqa: BLE001
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[exception] {e}",
            duration_seconds=time.monotonic() - started,
            exception=str(e),
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result

    merged = (completed.stdout or "") + (completed.stderr or "")
    result = CommandResult(
        argv=argv_list,
        returncode=completed.returncode,
        output=merged,
        duration_seconds=time.monotonic() - started,
    )
    if log_path is not None:
        _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
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
# `argv` is the only way to say what runs. There is no "command string" key
# and never will be: a string is what a shell parses, and a shell is where
# command injection (CWE-78) happens. A value that must come from user input
# goes into ONE argv slot via `safe_arg`, which refuses anything that a
# program would read as an option (argument injection).

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


async def run_spec(spec: CommandSpec) -> CommandResult:
    """Run a validated spec. Config-driven callers (catalog, manifests) come
    through here; Python callers with a literal argv may call `run` directly."""
    return await run(spec.argv, cwd=spec.cwd, timeout=spec.timeout, env=spec.env,
                     log_path=spec.log_path, log_label=spec.log_label)


def run_spec_sync(spec: CommandSpec) -> CommandResult:
    return run_sync(spec.argv, cwd=spec.cwd, timeout=spec.timeout, env=spec.env,
                    log_path=spec.log_path, log_label=spec.log_label)
