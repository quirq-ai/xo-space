"""The ``claude`` command line for one session, and the files it reads
(storm ch 02 §2.1, §3).

Every value comes from the ``SessionSpec``; nothing is fixed by a role. The
prompt never goes in argv: every turn is a user message on stdin. The files
claude reads (the system prompt, the MCP servers, the settings with the ask
rules) are written into the session's own folder under the state root
(``<state>/run/<session>/``, 0700, files 0600), never ``/tmp``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.claude_code.runtime.tools import REF_TOOLS, rules_for
from services.cowork_agent.adapters.contract import AgentDef, McpServer, PermissionMode, SessionSpec, ToolPolicy

#: The contract's modes as claude names them (ch 02 §3.3). A coordinator's
#: ``deny_unasked`` is not ``dontAsk``: its asks reach the stdio host, which
#: answers with core's read-only check (§0.75; see ``permission_mode``).
MODES: dict[str, str] = {
    "ask": "default",
    "accept_edits": "acceptEdits",
    "plan": "plan",
    "deny_unasked": "dontAsk",
    "auto": "auto",
    "bypass": "bypassPermissions",
}

#: What a coordinator's in-process workers may run unasked: claude's own allow
#: list, exactly as ch 02 §3.4 gives it (owner, §0.92). Everything else reaches
#: the stdio host; the credential ask rules still cover the read tools.
COORDINATOR_READ_SET = ("Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)")
#: The spec's shell allows the read set already holds (``shell:<prefix>``).
COORDINATOR_SHELL_PREFIXES = ("git log", "git show", "ls")
#: Modes in which claude would let a coordinator's workers act without
#: asking: a coordinator can't be read-only there, so they are refused.
COORDINATOR_REFUSED = frozenset({"accept_edits", "auto", "bypass"})

#: claude's coordinator addendum (storm ch 10 §7.2), reported as
#: ``system_prompt.addendum``.
COORDINATOR_ADDENDUM_ID = "claude.coordinator.v1"
COORDINATOR_ADDENDUM = ("You can't read files yourself. Read code through read-only workers (the Agent tool); "
                        "never guess.")

#: The read-only tool preset.
READ_ONLY_TOOLS = (*REF_TOOLS["read"], *REF_TOOLS["search"])


def claude_mode(mode: PermissionMode, *, coordinator: bool) -> str:
    """claude's name for a contract mode, at launch and for
    ``set_permission_mode``."""
    if coordinator and mode == "deny_unasked":
        return MODES["ask"]
    return MODES[mode]


def permission_mode(spec: SessionSpec) -> str:
    return claude_mode(spec.permissions.mode, coordinator=spec.orchestration == "coordinator")


def credential_ask_rules(home: Path, state_root: Path, agy_token: Path) -> tuple[str, ...]:
    """The ``permissions.ask`` rules every session gets (ch 16 §11.5): reads
    and edits of credential files go to the host even under ``acceptEdits``
    and ``auto``. ``~/`` rules are home-relative; ``//`` marks an absolute path."""
    def rule_path(path: Path) -> str:
        try:
            return "~/" + path.relative_to(home).as_posix()
        except ValueError:
            return "//" + path.as_posix().lstrip("/")

    targets = ("~/.ssh/**", "~/.aws/**", "~/.config/gh/**", "~/.claude/.credentials.json", "~/.codex/auth.json",
               rule_path(agy_token), rule_path(state_root) + "/**")
    return tuple(f"{tool}({target})" for target in targets for tool in ("Read", "Edit"))


def tool_rules(refs: tuple[str, ...]) -> list[str]:
    """claude rules for neutral refs; refs claude can't express are left
    out here and reported by ``check()``."""
    out: list[str] = []
    for ref in refs:
        out.extend(rules_for(ref) or ())
    return out


def is_comms(server: McpServer) -> bool:
    """XO's comms server, by the spec's own markers on every role: its tools
    need no permission, or a successful call ends the turn. claude loads it
    before the first turn, and coordinator mode keeps it among the
    coordinator's tools; any other server (Composio, the person's) is not."""
    return server.auto_approve or bool(server.terminal_tools)


def mcp_config(spec: SessionSpec) -> dict[str, object]:
    """``<run>/mcp.json``. An HTTP server's bearer token stays in the process
    env and is named here as ``${VAR}``, never written (C2: claude expands it)."""
    servers: dict[str, object] = {}
    for server in spec.mcp_servers:
        if server.transport == "stdio":
            entry: dict[str, object] = {"type": "stdio", "command": server.command[0] if server.command else "",
                                        "args": list(server.command[1:]), "env": dict(server.env)}
        else:
            entry = {"type": "http", "url": server.url or ""}
            if server.bearer_env:
                entry["headers"] = {"Authorization": f"Bearer ${{{server.bearer_env}}}"}
        if is_comms(server):
            entry["alwaysLoad"] = True
            if spec.orchestration == "coordinator":
                entry["role"] = "comms"
        servers[server.name] = entry
    return {"mcpServers": servers}


def settings(spec: SessionSpec, credential_rules: tuple[str, ...]) -> dict[str, object]:
    """``<run>/settings.json``: permissions only, never hooks."""
    return {"permissions": {"ask": [*credential_rules, *tool_rules(spec.tools.ask)]}}


def system_text(spec: SessionSpec) -> str | None:
    """What goes in ``<run>/system.txt``: the spec's role text, plus the
    coordinator addendum."""
    parts = [spec.system_prompt.text] if spec.system_prompt and spec.system_prompt.text else []
    if spec.orchestration == "coordinator":
        parts.append(COORDINATOR_ADDENDUM)
    return "\n\n".join(parts) if parts else None


def agent_definitions(agents: Mapping[str, AgentDef]) -> dict[str, dict[str, object]]:
    """``initialize``'s sub-agent definitions."""
    out: dict[str, dict[str, object]] = {}
    for name, agent in agents.items():
        entry: dict[str, object] = {"description": agent.description, "prompt": agent.prompt}
        if agent.model:
            entry["model"] = agent.model
        if agent.tools is not None:
            entry["tools"] = _preset_tools(agent.tools) + tool_rules(agent.tools.allow)
            if agent.tools.deny:
                entry["disallowedTools"] = tool_rules(agent.tools.deny)
        out[name] = entry
    return out


def _preset_tools(tools: ToolPolicy) -> list[str]:
    return list(READ_ONLY_TOOLS) if tools.preset == "read_only" else []


def session_switches(spec: SessionSpec) -> dict[str, str]:
    """The ``CLAUDE_CODE_*`` variables this spec needs (ch 02 §2.2), plus the
    HTTP servers' tokens, which travel in the env, never in a file."""
    env: dict[str, str] = {}
    if spec.orchestration == "coordinator":
        env["CLAUDE_CODE_COORDINATOR_MODE"] = "1"
    prewait = [s.name for s in spec.mcp_servers if is_comms(s)]
    if prewait:
        env["CLAUDE_CODE_MCP_PREWAIT_SERVERS"] = ",".join(prewait)
    terminal = [f"mcp__{s.name}__{t}" for s in spec.mcp_servers for t in s.terminal_tools]
    if terminal:
        env["CLAUDE_CODE_TERMINAL_MCP_TOOLS"] = ",".join(terminal)
    if spec.limits.max_subagents is not None:
        env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] = str(spec.limits.max_subagents)
    if spec.limits.max_depth is not None:
        env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] = str(spec.limits.max_depth)
    for server in spec.mcp_servers:
        if server.transport == "http":
            env.update(server.env)
    return env


@dataclass(frozen=True)
class RunFiles:
    """The files of one session's folder that claude is pointed at."""

    folder: Path
    system: Path | None
    mcp: Path | None
    settings: Path


def write_run_files(folder: Path, spec: SessionSpec, credential_rules: tuple[str, ...]) -> RunFiles:
    """Create the session's folder (0700) and its files (0600). Blocking:
    run it in a thread."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    text = system_text(spec)
    system = _write(folder / "system.txt", text) if text is not None else None
    mcp = _write(folder / "mcp.json", json.dumps(mcp_config(spec))) if spec.mcp_servers else None
    settings_file = _write(folder / "settings.json", json.dumps(settings(spec, credential_rules)))
    return RunFiles(folder=folder, system=system, mcp=mcp, settings=settings_file)


def _write(path: Path, text: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)
    return path


def build_argv(binary: str, spec: SessionSpec, files: RunFiles, *, native_id: str,
               resume: str | None) -> list[str]:
    """The whole command line. ``resume`` is the native id to resume (the
    spec's, or this session's own after a restart); a fork keeps the
    resumed history under ``native_id``."""
    argv = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--permission-prompt-tool", "stdio"]
    if resume:
        argv += ["--resume", resume]
        if spec.fork:
            argv += ["--fork-session", "--session-id", native_id]
        if spec.fork_at:
            argv += ["--resume-session-at", spec.fork_at]
    else:
        argv += ["--session-id", native_id]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    argv += ["--permission-mode", permission_mode(spec)]
    argv += _tool_flags(spec)
    if files.system is not None:
        flag = "--system-prompt-file" if spec.system_prompt and spec.system_prompt.mode == "replace" \
            else "--append-system-prompt-file"
        argv += [flag, str(files.system)]
    if files.mcp is not None:
        argv += ["--mcp-config", str(files.mcp)]
    if spec.isolation == "isolated":
        argv += ["--strict-mcp-config", "--setting-sources", ""]
    argv += ["--settings", str(files.settings)]
    for folder in spec.add_dirs:
        argv += ["--add-dir", folder]
    argv += ["--thinking-display", "summarized"] if spec.thinking == "summary" else ["--thinking", "disabled"]
    if spec.output_schema is not None:
        argv += ["--json-schema", json.dumps(spec.output_schema)]
    if not spec.persist:
        argv += ["--no-session-persistence"]
    if spec.limits.max_usd is not None:
        argv += ["--max-budget-usd", f"{spec.limits.max_usd:g}"]
    if spec.limits.max_turns is not None:
        argv += ["--max-turns", str(spec.limits.max_turns)]
    if spec.runs_detail == "text":
        argv += ["--forward-subagent-text"]
    return argv


def _tool_flags(spec: SessionSpec) -> list[str]:
    flags: list[str] = []
    if spec.tools.preset == "none":
        flags += ["--tools", ""]
    elif spec.tools.preset == "read_only" and spec.orchestration != "coordinator":
        # A coordinator's tools are coordinator mode's; its workers' writes
        # reach the stdio host, which keeps them read-only (§0.75).
        flags += ["--tools", *READ_ONLY_TOOLS]
    allowed = tool_rules(spec.tools.allow)
    allowed += [f"mcp__{s.name}__*" for s in spec.mcp_servers if s.auto_approve]
    if spec.orchestration == "coordinator":
        allowed += COORDINATOR_READ_SET
    allowed = list(dict.fromkeys(allowed))      # a rule the spec and a server both give is listed once
    # One argument per rule: a rule may hold a space (``Bash(git log:*)``).
    if allowed:
        flags += ["--allowedTools", *allowed]
    denied = tool_rules(spec.tools.deny)
    if denied:
        flags += ["--disallowedTools", *denied]
    return flags
