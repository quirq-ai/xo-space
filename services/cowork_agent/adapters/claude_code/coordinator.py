"""Capability ``coordinator``: run one projects-chat turn with the claude CLI.

The projects chat (``services/projects_chat/``) names no agent; it hands this
module a :class:`TurnSpec` (role, session, cwd, the envelopes for the turn,
the comms URL) and gets back what happened. Everything Claude-specific is
here and in ``config/agents/claude_code/coordinator.json``:

- coordinators run in the CLI's coordinator mode (``CLAUDE_CODE_COORDINATOR_MODE``)
  with the comms server marked ``"role": "comms"``: their tools are
  orchestration and comms only, plus read-only readers (``--allowedTools``);
  there is no ``--dangerously-skip-permissions`` for them;
- threads run like today's chat, in their worktree, with the comms server and
  the role's ``--disallowedTools``;
- the comms tools a successful call to which ends a turn are declared with
  ``CLAUDE_CODE_TERMINAL_MCP_TOOLS``, and the comms server is waited for
  before the first prompt (``CLAUDE_CODE_MCP_PREWAIT_SERVERS``).

Per turn, a spawn folder (0700) under ``XO_MCP_TMP_ROOT`` holds the role
prompt and ``mcp.json``, the shape Claude Code's own ``bridge-spawn/`` has;
it is removed when the turn ends. The envelopes go in on stdin, so their size
is never limited by the argument list. One process per turn, resumed by the
session's own UUID, through the one command executor
(``utils.commands.run``); a turn that outlives ``XO_TURN_TIMEOUT_S`` is
killed with its process tree.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from services.cowork_agent.adapters.claude_code.adapter import Adapter
from services.cowork_agent.adapters.claude_code.streaming import turn_outcome
from services.cowork_agent.registry.settings import load_agent_config
from utils.commands import run

log = logging.getLogger(__name__)

SUPPORTED_ROLES = ("workspace-coordinator", "project-coordinator", "thread")
_CONFIG = Path(__file__).resolve().parents[4] / "config" / "agents" / "claude_code" / "coordinator.json"
COMMS_SERVER = "xo_comms"
_STDERR_TAIL = 2000


@dataclass
class TurnSpec:
    role: str
    session_id: str
    agent_session_uuid: str
    resume: bool
    cwd: str
    prompt: str
    comms_url: str
    terminal_tools: list[str]
    add_dirs: list[str] = field(default_factory=list)
    budget_usd: Optional[float] = None
    log_path: Optional[str] = None


def role_config(role: str) -> dict:
    doc = json.loads(_CONFIG.read_text(encoding="utf-8"))
    roles = doc.get("roles") or {}
    if role not in roles:
        raise ValueError(f"coordinator.json has no role {role!r}")
    return roles[role]


def prompt_fingerprint(role: str) -> str:
    """Changes when the role's prompt or settings change; a coordinator started
    on an older one is replaced (a running session keeps its first prompt)."""
    import hashlib

    return hashlib.sha256(json.dumps(role_config(role), sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _tmp_root() -> Path:
    return Path(os.environ.get("XO_MCP_TMP_ROOT") or "/tmp/xo-cowork")


def _spawn_dir(spec: TurnSpec, cfg: dict) -> Path:
    folder = _tmp_root() / f"{spec.session_id}-{secrets.token_hex(4)}"
    folder.mkdir(parents=True, mode=0o700)
    prompt = "\n".join(cfg.get("prompt") or [])
    (folder / "append-system-prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    server = {"type": "http", "url": spec.comms_url, "alwaysLoad": True}
    if cfg.get("coordinator_mode"):
        server["role"] = "comms"
    (folder / "mcp.json").write_text(json.dumps({"mcpServers": {COMMS_SERVER: server}}), encoding="utf-8")
    for name in ("append-system-prompt.txt", "mcp.json"):
        os.chmod(folder / name, 0o600)
    return folder


def build_argv(spec: TurnSpec, cfg: dict, spawn: Path, cli: str) -> list[str]:
    argv = [cli, "--print", "--output-format", "stream-json", "--verbose",
            "--append-system-prompt-file", str(spawn / "append-system-prompt.txt"),
            "--mcp-config", str(spawn / "mcp.json"), "--strict-mcp-config"]
    if cfg.get("effort"):
        argv += ["--effort", str(cfg["effort"])]
    if cfg.get("model"):
        argv += ["--model", str(cfg["model"])]
    if spec.budget_usd:
        argv += ["--max-budget-usd", f"{spec.budget_usd:g}"]
    for folder in [spec.cwd, *spec.add_dirs]:
        argv += ["--add-dir", folder]
    if cfg.get("coordinator_mode"):
        argv += ["--allowedTools", f"mcp__{COMMS_SERVER}__*", *cfg.get("read_tools", [])]
    else:
        argv += ["--dangerously-skip-permissions", "--allowedTools", f"mcp__{COMMS_SERVER}__*"]
        if cfg.get("disallowed_tools"):
            argv += ["--disallowedTools", *cfg["disallowed_tools"]]
    argv += ["--resume", spec.agent_session_uuid] if spec.resume else ["--session-id", spec.agent_session_uuid]
    return argv


def build_env(spec: TurnSpec, cfg: dict, adapter: Adapter) -> dict[str, str]:
    env = adapter.cli_env()
    env["CLAUDE_CODE_MCP_PREWAIT_SERVERS"] = COMMS_SERVER
    env["CLAUDE_CODE_TERMINAL_MCP_TOOLS"] = ",".join(spec.terminal_tools)
    if cfg.get("coordinator_mode"):
        env["CLAUDE_CODE_COORDINATOR_MODE"] = "1"
    else:
        env.pop("CLAUDE_CODE_COORDINATOR_MODE", None)
    return env


def turn_timeout_s() -> float:
    """The longest one turn may run before it is killed (``XO_TURN_TIMEOUT_S``, default 1 h)."""
    try:
        return max(60.0, float(os.environ.get("XO_TURN_TIMEOUT_S") or 3600))
    except ValueError:
        return 3600.0


async def run_turn(spec: TurnSpec) -> dict:
    """Run one turn to completion through the one command executor. Never
    raises for a CLI failure; returns ``{ok, exit_code, native_session_id,
    outcome, result_text, tool_calls, error}``.

    The agent's output stays out of the shared command log (it holds the
    whole turn); it is written to ``spec.log_path`` instead.
    """
    if spec.role not in SUPPORTED_ROLES:
        return {"ok": False, "error": f"role {spec.role!r} is not supported", "tool_calls": []}
    cfg = role_config(spec.role)
    adapter = Adapter(load_agent_config("claude_code"))
    cli = adapter.config.get("cli_path") or "claude"
    spawn = _spawn_dir(spec, cfg)
    try:
        res = await run(build_argv(spec, cfg, spawn, cli), cwd=spec.cwd, env=build_env(spec, cfg, adapter),
                        input=spec.prompt.encode("utf-8"), separate_stderr=True, timeout=turn_timeout_s(),
                        log_label=f"projects chat: {spec.role} turn", log_output=False)
    finally:
        shutil.rmtree(spawn, ignore_errors=True)
    if res.binary_missing:
        return {"ok": False, "exit_code": None, "error": f"the claude CLI was not found ({cli})", "tool_calls": []}
    if spec.log_path:
        try:
            Path(spec.log_path).write_text(res.stdout, encoding="utf-8")
        except OSError:
            log.warning("projects chat: could not write the turn log %s", spec.log_path)
    tool_calls: list[str] = []
    native_id = outcome = result_text = None
    for raw in res.stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "system" and event.get("subtype") == "init":
            native_id = event.get("session_id") or native_id
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append(block.get("name") or "")
        elif kind == "result":
            outcome = turn_outcome(event)
            result_text = event.get("result")
            native_id = event.get("session_id") or native_id
    ok = res.returncode == 0 and not res.timed_out and not (outcome or {}).get("is_error")
    error = None
    if not ok:
        error = ("timed out" if res.timed_out else None) or (res.stderr or "")[-_STDERR_TAIL:] \
            or result_text or f"exit {res.returncode}"
    return {"ok": ok, "exit_code": res.returncode, "native_session_id": native_id, "outcome": outcome,
            "result_text": result_text, "tool_calls": tool_calls, "error": error}
