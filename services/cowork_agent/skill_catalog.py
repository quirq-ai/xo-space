"""
On-demand skill install catalog.

Backs ``GET /api/skills/catalog`` and ``POST /api/skills/install``. The catalog
file (``config/skills/catalog.json``) is the server-side source of truth
mapping a skill name to one or more commands; clients only ever send a
name, and command text is never returned to them (entries may embed tokens or
host paths). Commands run through ``utils.commands`` as argv lists — there is
no shell, so a catalog entry can never chain, pipe or redirect. Distinct from ``skill_installer.py``, which copies repo-bundled
skills at startup.

Catalog entry shape (one of ``command``/``commands`` is required):

    name             required, unique
    description      optional
    command          one command: an argv list (preferred) or a string that
                     is split with POSIX quoting and no shell
    commands         non-empty list of such commands, run sequentially,
                     stopping at the first failure; a string containing a
                     shell operator (&&, |, ;, >, <, `, $() makes the entry
                     invalid
    timeout_seconds  optional, default 300 — applies per command
    cwd              optional working directory for every command
    success_message  optional human-authored line returned as ``summary`` on
                     success (same placeholders as commands are expanded); a
                     default is used when omitted. Failure always returns a
                     generic ``Failed to install <name>.`` line.

A missing or malformed catalog degrades to an empty catalog with a printed
warning; invalid entries are skipped, valid ones kept — matches the non-fatal
bootstrap pattern of ``skill_installer.py``.

``install_startup_skills()`` reuses the same catalog to install an agent's
boot-time skills, declared per agent in
``config/agents/<name>/settings.json`` → ``startup_skills``.
"""

import asyncio
import json
from pathlib import Path

from utils.commands import CommandSpecError, run, split_command

from services.cowork_agent.registry import agent_registry
from services.cowork_agent.registry.settings import load_agent_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = _REPO_ROOT / "config" / "skills" / "catalog.json"

DEFAULT_TIMEOUT_SECONDS = 300
_OUTPUT_CAP = 10_000  # chars kept per stream per step

# One lock per skill name, guarding against concurrent installs of the same
# skill. In-process only — like chat_state.py, this does not coordinate across
# multiple workers.
_locks: dict[str, asyncio.Lock] = {}


class UnknownSkillError(KeyError):
    """Requested skill name is not in the catalog."""


class InstallInProgressError(RuntimeError):
    """An install for this skill name is already running in this process."""


def load_catalog() -> dict[str, dict]:
    """Read and validate the catalog file. Returns ``{name: entry}``."""
    try:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"⚠️ skill catalog unreadable ({CATALOG_PATH}): {exc}")
        return {}

    entries = raw.get("skills") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        print(f"⚠️ skill catalog malformed ({CATALOG_PATH}): expected {{\"skills\": [...]}}")
        return {}

    catalog: dict[str, dict] = {}
    for entry in entries:
        normalized = _normalize(entry)
        if normalized is None:
            label = entry.get("name", "<unnamed>") if isinstance(entry, dict) else "<not an object>"
            print(f"⚠️ skill catalog: skipping invalid entry {label!r} in {CATALOG_PATH}")
            continue
        catalog[normalized["name"]] = normalized
    return catalog


async def install(name: str) -> dict:
    """Run the catalogued commands for ``name`` sequentially, fail-fast.

    Raises ``UnknownSkillError`` for names not in the catalog and
    ``InstallInProgressError`` if this process is already installing ``name``.
    A failing or timed-out command is not an exception — it is reported in the
    returned result (top-level ``ok`` is True only if every step succeeded).
    """
    entry = load_catalog().get(name)
    if entry is None:
        raise UnknownSkillError(name)

    lock = _lock_for(name)
    if lock.locked():
        raise InstallInProgressError(name)

    async with lock:
        steps: list[dict] = []
        ok = True
        for index, argv in enumerate(entry["commands"]):
            try:
                rendered = [_expand_placeholders(token) for token in argv]
            except Exception as exc:
                steps.append(_step_result(index, ok=False, exit_code=None, stdout="",
                                          stderr=f"placeholder expansion failed: {exc}",
                                          duration=0.0, timed_out=False))
                print(f"⚠️ skill install {name!r} step {index + 1}: placeholder expansion failed: {exc}")
                ok = False
                break
            step = await _run_step(index, rendered, entry["timeout_seconds"], entry["cwd"])
            steps.append(step)
            if not step["ok"]:
                detail = step["stderr"].strip() or step["stdout"].strip()
                print(
                    f"⚠️ skill install {name!r} step {index + 1}/{len(entry['commands'])} failed "
                    f"(exit={step['exit_code']}, timed_out={step['timed_out']})"
                    + (f": {detail[-2000:]}" if detail else "")
                )
                ok = False
                break
        steps_total = len(entry["commands"])
        summary = (
            _success_summary(name, entry["success_message"])
            if ok
            else f"Failed to install {name!r}."
        )
        return {
            "name": name,
            "ok": ok,
            "summary": summary,
            "steps": steps,
            "steps_total": steps_total,
            "steps_run": len(steps),
        }


async def install_startup_skills() -> None:
    """Install the active agent's declared boot-time skills.

    The list is declarative and per-agent: ``startup_skills`` in
    ``config/agents/<AGENT_NAME>/settings.json``, holding catalog names (not
    commands — the command text stays in ``config/skills/catalog.json``, the
    single source of truth shared with ``POST /api/skills/install``). An agent
    that declares nothing installs nothing, so core never names a backend.

    Called from ``server.py``'s lifespan as a background task: installs hit the
    network and must not delay serving traffic. Every failure path is non-fatal
    and logged — a skill that won't install must not take the API down. Runs on
    every boot; entries are expected to be idempotent (re-install/upgrade).
    """
    try:
        agent_name = agent_registry.get_active_agent().name
        config = load_agent_config(agent_name)
    except Exception as exc:
        print(f"⚠️ startup skills: could not resolve active agent config (non-fatal): {exc}")
        return

    declared = config.get("startup_skills")
    if declared is None:
        return
    if not isinstance(declared, list):
        print(
            f"⚠️ startup skills: {agent_name}/settings.json 'startup_skills' must be a "
            f"list of catalog names — got {type(declared).__name__}, skipping"
        )
        return

    names = [n.strip() for n in declared if isinstance(n, str) and n.strip()]
    if not names:
        return

    print(f"🔧 Startup skills ({agent_name}): {', '.join(names)}")
    for name in names:
        try:
            result = await install(name)
        except UnknownSkillError:
            print(f"⚠️ startup skill {name!r} is not in {CATALOG_PATH} — skipping")
            continue
        except InstallInProgressError:
            print(f"⚠️ startup skill {name!r} is already installing — skipping")
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"⚠️ startup skill {name!r} failed (non-fatal): {exc}")
            continue

        if result["ok"]:
            print(f"✅ startup skill {name!r} installed")
        else:
            # install() already logged the failing step's exit code and output.
            print(f"⚠️ startup skill {name!r} install failed (non-fatal)")


def _success_summary(name: str, message: str | None) -> str:
    """Success line for the response.

    Returns the catalog's human-authored ``success_message`` (with the same
    ``{skills_dir}``-style placeholders expanded), or a default when the entry
    sets none. Best-effort on expansion: display text must never fail an
    install that already succeeded, so a placeholder error falls back to the
    raw message. Plain messages (no ``{`` ) never touch the registry.
    """
    if not message:
        return f"Installed {name!r}."
    try:
        return _expand_placeholders(message)
    except Exception:
        return message


def _expand_placeholders(command: str) -> str:
    """Substitute known ``{token}`` placeholders from the active agent.

    Supported tokens (resolved from the active agent's manifest, so the
    catalog never names a backend):

        {skills_dir}  — ``<home_dir>/skills`` for the active agent
        {home_dir}    — the active agent's home directory
        {agent_name}  — the active agent's name

    Unknown ``{...}`` sequences are left verbatim so a literal brace in a
    command survives. Tokens are only resolved lazily if the command actually
    contains one, so placeholder-free commands never touch the registry.
    """
    if "{" not in command:
        return command

    agent = agent_registry.get_active_agent()
    tokens = {
        "skills_dir": str(agent.home_dir / "skills"),
        "home_dir": str(agent.home_dir),
        "agent_name": agent.name,
    }

    class _Passthrough(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return command.format_map(_Passthrough(tokens))


def _lock_for(name: str) -> asyncio.Lock:
    return _locks.setdefault(name, asyncio.Lock())


def _to_argv(step) -> list[str] | None:
    """A catalog step is an argv list, or a string split without a shell.
    None means the step is invalid (and so is its entry)."""
    if isinstance(step, list):
        if step and all(isinstance(t, str) and t for t in step):
            return list(step)
        return None
    if isinstance(step, str):
        try:
            return split_command(step)
        except CommandSpecError as exc:
            print(f"⚠️ skill catalog: rejected command {step!r}: {exc}")
            return None
    return None


def _normalize(entry) -> dict | None:
    """Validate one raw catalog entry; None means invalid (skip it)."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    command, commands = entry.get("command"), entry.get("commands")
    if command is not None and commands is None:
        raw_steps = [command]
    elif command is None and isinstance(commands, list) and commands:
        raw_steps = list(commands)
    else:
        return None
    resolved: list[list[str]] = []
    for step in raw_steps:
        argv = _to_argv(step)
        if argv is None:
            return None
        resolved.append(argv)

    timeout = entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return None
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return None
    success_message = entry.get("success_message")
    if success_message is not None and not isinstance(success_message, str):
        return None

    return {
        "name": name.strip(),
        "description": entry.get("description") or "",
        "commands": resolved,
        "timeout_seconds": timeout,
        "cwd": cwd,
        "success_message": success_message,
    }


async def _run_step(index: int, argv: list[str], timeout_seconds: float, cwd: str | None) -> dict:
    """One catalog step through the shared runner: argv, no shell. The runner
    merges stdout and stderr, so the step reports them as one stream."""
    result = await run(argv, cwd=cwd, timeout=timeout_seconds)
    output = result.output[:_OUTPUT_CAP]
    return _step_result(
        index,
        ok=result.ok,
        exit_code=None if result.binary_missing or result.exception else result.returncode,
        stdout=output if result.ok else "",
        stderr="" if result.ok else output,
        duration=result.duration_seconds,
        timed_out=result.timed_out,
    )


def _step_result(index, *, ok, exit_code, stdout, stderr, duration, timed_out) -> dict:
    return {
        "index": index,
        "ok": ok,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
    }
