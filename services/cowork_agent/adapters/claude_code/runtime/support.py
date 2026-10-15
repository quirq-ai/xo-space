"""What the claude runtime supports: ``check(spec)`` (storm ch 01 §6) and the
feature list (ch 01 §4.8, ch 02 §8). Both are pure: they start nothing.

Each spec field gets a verdict and who enforces it. ``refused`` items make
``check`` raise ``SpecRefused``, as do ``emulated``/``ignored`` ones under
``strict``. Core's own safety rule (a limit enforced only by the prompt or by
nothing refuses the session) reads the same report.
"""

from __future__ import annotations

from services.cowork_agent.adapters.claude_code.runtime import command
from services.cowork_agent.adapters.claude_code.runtime.tools import rules_for
from services.cowork_agent.adapters.contract import (
    CONTRACT_VERSION,
    FeatureList,
    FeatureSupport,
    SessionSpec,
    SpecRefused,
    SupportItem,
    SupportReport,
)

AGENT = "claude_code"

#: The files in a checkout that make claude run code on its own; the
#: integrity check watches them (storm ch 16 §2.1).
PROJECT_CONFIG_PATHS = (".claude/**", ".mcp.json")

MODES = ("ask", "accept_edits", "plan", "deny_unasked", "auto", "bypass")


def _item(feature: str, mechanism: str, *, support: str = "native", enforced_by: str = "cli",
          note: str = "") -> SupportItem:
    return SupportItem(feature=feature, support=support, mechanism=mechanism, enforced_by=enforced_by, note=note)


def check(spec: SessionSpec) -> SupportReport:
    items = [
        _item("cwd", "process working folder"),
        _item("permissions.mode", f"--permission-mode {command.permission_mode(spec)}",
              enforced_by="host" if spec.orchestration == "coordinator" else "cli",
              note="a coordinator's asks reach the stdio host (can_use_tool), which answers with core's policy"
              if spec.orchestration == "coordinator" else ""),
        _tools_preset(spec),
    ]
    if spec.add_dirs:
        items.append(_item("add_dirs", "--add-dir"))
    if spec.model:
        items.append(_item("model", "--model"))
    if spec.effort:
        items.append(_item("effort", "--effort"))
    if spec.system_prompt:
        items.append(_item("system_prompt", "--system-prompt-file" if spec.system_prompt.mode == "replace"
                           else "--append-system-prompt-file"))
    if spec.orchestration == "coordinator":
        items.append(_item("orchestration", "CLAUDE_CODE_COORDINATOR_MODE + --allowedTools read set"))
        items.append(_item("system_prompt.addendum", command.COORDINATOR_ADDENDUM_ID, enforced_by="prompt"))
    for name, refs in (("tools.allow", spec.tools.allow), ("tools.ask", spec.tools.ask),
                       ("tools.deny", spec.tools.deny)):
        mechanism = {"tools.allow": "--allowedTools", "tools.ask": "--settings permissions.ask",
                     "tools.deny": "--disallowedTools"}[name]
        missing = [ref for ref in refs if rules_for(ref) is None]
        if missing:
            items.append(_item(name, mechanism, support="refused" if name == "tools.deny" else "ignored",
                               enforced_by="none", note=f"claude has no tool for {', '.join(missing)}"))
        elif refs:
            items.append(_item(name, mechanism))
    sandbox = spec.permissions.sandbox
    if sandbox not in (None, "none", "full_access") or spec.permissions.network is not None \
            or spec.permissions.writable_roots:
        items.append(_item("permissions.sandbox", "none", support="refused", enforced_by="none",
                           note="this runtime doesn't run claude's sandbox"))
    if spec.mcp_servers:
        items.append(_item("mcp_servers", "--mcp-config <run>/mcp.json"))
    if spec.host_tools:
        items.append(_item("host_tools", "none", support="refused", enforced_by="none",
                           note="host tools (an SDK MCP server over mcp_message) are not built yet"))
    if spec.agents:
        items.append(_item("agents", "initialize.agents"))
    items.append(_item("isolation", "--setting-sources '' --strict-mcp-config" if spec.isolation == "isolated"
                       else "claude's default setting sources"))
    if spec.resume:
        items.append(_item("resume", "--resume"))
    if spec.fork:
        items.append(_item("fork", "--fork-session") if spec.resume else
                     _item("fork", "--fork-session", support="refused", enforced_by="none",
                           note="a fork needs a session to resume"))
    if spec.fork_at:
        items.append(_item("fork_at", "--resume-session-at") if spec.resume else
                     _item("fork_at", "--resume-session-at", support="refused", enforced_by="none",
                           note="fork_at needs a session to resume"))
    if not spec.persist:
        items.append(_item("persist", "--no-session-persistence"))
    if spec.output_schema is not None:
        items.append(_item("output_schema", "--json-schema"))
    items.append(_item("runs_detail", "--forward-subagent-text" if spec.runs_detail == "text"
                       else "filtered from the stream"))
    items.append(_item("thinking", "--thinking-display summarized" if spec.thinking == "summary"
                       else "--thinking disabled"))
    limits = spec.limits
    if any(v is not None for v in (limits.max_usd, limits.max_turns, limits.max_subagents, limits.max_depth)):
        items.append(_item("limits", "--max-budget-usd / --max-turns / CLAUDE_CODE_MAX_* variables"))
    if spec.env:
        items.append(_item("env", "added to the env whitelist"))
    if spec.extensions:
        items.append(_item("extensions", "none", support="ignored", enforced_by="none",
                           note=f"no extensions are defined for {AGENT}: {', '.join(sorted(spec.extensions))}"))
    if spec.debug_raw:
        items.append(_item("debug_raw", "Event.raw"))
    report = SupportReport(items=tuple(items))
    refused = [i for i in items if i.support == "refused"]
    weak = [i for i in items if i.support in ("emulated", "ignored")]
    if refused:
        raise SpecRefused("claude_code can't run this spec: " + "; ".join(f"{i.feature} ({i.note})" for i in refused),
                          report)
    if spec.strict and weak:
        raise SpecRefused("strict spec: " + "; ".join(f"{i.feature} is {i.support}" for i in weak), report)
    return report


def _tools_preset(spec: SessionSpec) -> SupportItem:
    if spec.tools.preset == "read_only" and spec.orchestration == "coordinator":
        return _item("tools.preset", "coordinator mode; workers' other tools reach the stdio host",
                     enforced_by="host")
    if spec.tools.preset in ("none", "read_only"):
        return _item("tools.preset", "--tools")
    return _item("tools.preset", "claude's default tool set")


def features(cli_version: str | None) -> FeatureList:
    native, emulated, unsupported = "native", "emulated", "unsupported"

    def f(support: str | None = native, note: str = "", behaviour: str | None = None,
          values: tuple[str, ...] = ()) -> FeatureSupport:
        return FeatureSupport(support=support, note=note, behaviour=behaviour, values=values)

    listed = {
        "cwd": f(note="process working folder"),
        "add_dirs": f(note="--add-dir"),
        "model": f(note="--model; live with set_model"),
        "effort": f(values=("low", "medium", "high", "xhigh", "max")),
        "system_prompt": f(values=("append", "replace")),
        "tools": f(note="--tools, --allowedTools, --disallowedTools, permissions.ask"),
        "permissions": f(note="every mode; no sandbox"),
        "permissions.mode": f(values=MODES, note="auto = claude's own classifier; a coordinator's asks reach "
                                                 "the stdio host"),
        "permissions.sandbox": f(unsupported, "claude's sandbox needs bubblewrap; this runtime doesn't run it"),
        "tools.deny": f(note="--disallowedTools"),
        "orchestration": f(values=("none", "coordinator"), note="CLAUDE_CODE_COORDINATOR_MODE"),
        "mcp_servers": f(note="--mcp-config; HTTP bearer tokens travel in the env"),
        "host_tools": f(unsupported, "an SDK MCP server over mcp_message is not built yet"),
        "agents": f(note="initialize.agents"),
        "isolation": f(values=("user", "isolated")),
        "resume": f(), "fork": f(), "fork_at": f(note="--resume-session-at"),
        "persist": f(note="--no-session-persistence"),
        "output_schema": f(note="--json-schema"),
        "runs_detail": f(values=("lifecycle", "steps", "text")),
        "thinking": f(values=("off", "summary"), note="--thinking-display summarized"),
        "limits": f(note="nothing set by XO"),
        "env": f(note="on top of the env whitelist"),
        "extensions": f(unsupported, "none defined"),
        "strict": f(), "debug_raw": f(),
        "send_while_running": f(behaviour="steered", note="a message sent mid-turn joins the running turn"),
        "interrupt": f(note="control_request interrupt; it also stops every background sub-agent"),
        "update.model": f(note="set_model"),
        "update.effort": f(emulated, "restart + --resume before the next turn"),
        "update.permission_mode": f(note="set_permission_mode"),
        "runs.list": f(note="subagents/agent-<id>.meta.json"),
        "runs.read": f(note="subagents/agent-<id>.jsonl"),
        "runs.stop": f(note="stop_task, confirmed by the run's end event"),
        "runs.message": f(unsupported, "send_task_message is a stub in this CLI"),
        "models": f(note="from initialize"),
        "usage": f(note="the last rate-limit state claude reported"),
        "oneshot": f(note="--print --tools '' --no-session-persistence"),
        "project_config_paths": f(None, values=PROJECT_CONFIG_PATHS),
        "tool_input_delta": f(note="file content bytes while Write/Edit stream"),
        "tool_output_delta": f(unsupported, "claude doesn't stream a command's output"),
        "permission_parking": f(emulated, "deny naming the review item, then grant + wake"),
    }
    return FeatureList(agent=AGENT, cli_version=cli_version, contract_version=CONTRACT_VERSION, features=listed)
