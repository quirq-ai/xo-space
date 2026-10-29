"""What the agy runtime supports: ``check(spec)`` (storm ch 01 §6) and the
feature list (ch 01 §4.8, ch 04 §5). Both are pure: they start nothing.

Each spec field gets a verdict and who enforces it. Most of agy's are
enforced by XO's hook (``enforced_by="hook"``): agy runs it before every
tool call of the session and of its sub-agents, and the runtime answers it.
``refused`` items make ``check`` raise ``SpecRefused``, as do
``emulated``/``ignored`` ones under ``strict``.
"""

from __future__ import annotations

from services.cowork_agent.adapters.antigravity.runtime import argv as a
from services.cowork_agent.adapters.antigravity.runtime.tools import PROJECT_CONFIG_PATHS
from services.cowork_agent.adapters.contract import (
    CONTRACT_VERSION,
    FeatureList,
    FeatureSupport,
    SessionSpec,
    SpecRefused,
    SupportItem,
    SupportReport,
)

AGENT = "antigravity"
EFFORTS = a.EFFORTS
#: What agy reads in the project's main checkout while it runs in one of its
#: worktrees: nothing (a worktree is its own project root for ``.agents/``).
MAIN_CHECKOUT_CONFIG_PATHS: tuple[str, ...] = ()
#: The neutral tool refs this runtime can deny (its hook refuses the calls).
DENIABLE = frozenset({"edit", "create", "delete", "shell", "web_fetch", "web_search", "worktree", "schedule",
                      "subagents", "ask_user"})
#: The coordinator addendum (storm ch 10 §7.2), reported as ``system_prompt.addendum``.
COORDINATOR_ADDENDUM_ID = "agy.coordinator.v1"


def _item(feature: str, mechanism: str, *, support: str = "native", enforced_by: str = "hook",
          note: str = "") -> SupportItem:
    return SupportItem(feature=feature, support=support, mechanism=mechanism, enforced_by=enforced_by, note=note)


def check(spec: SessionSpec) -> SupportReport:
    coordinator = spec.orchestration == "coordinator"
    items = [_item("cwd", "the process's folder", enforced_by="cli"), _permission_mode(spec), _tools_preset(spec)]
    if spec.add_dirs:
        items.append(_item("add_dirs", "--add-dir", enforced_by="cli"))
    if spec.model:
        items.append(_item("model", "--model", enforced_by="cli"))
    if spec.effort:
        items.append(_item("effort", "--effort", enforced_by="cli"))
    if spec.system_prompt:
        if spec.system_prompt.mode == "replace":
            items.append(_item("system_prompt", "none", support="refused", enforced_by="none",
                               note="agy keeps its own system prompt; XO's role text can only be added"))
        else:
            items.append(_item("system_prompt", "XO's SessionStart hook: a system message at the conversation's "
                                                "start", support="emulated", enforced_by="prompt"))
    if coordinator:
        items.append(_item("orchestration", "every tool call but comms to the host's read-only policy, through "
                                            "XO's hook", enforced_by="host"))
        items.append(_item("system_prompt.addendum", COORDINATOR_ADDENDUM_ID, enforced_by="prompt"))
    if spec.tools.allow:
        items.append(_item("tools.allow", "XO's policy (the host), through XO's hook", enforced_by="host"))
    if spec.tools.ask:
        items.append(_item("tools.ask", "every action reaches the host through XO's hook", enforced_by="host"))
    if spec.tools.deny:
        missing = [ref for ref in spec.tools.deny if ref not in DENIABLE and not ref.startswith(("shell:", "mcp:"))]
        if missing:
            items.append(_item("tools.deny", "none", support="refused", enforced_by="none",
                               note=f"agy can't be kept from {', '.join(missing)}"))
        else:
            items.append(_item("tools.deny", "refused by XO's hook"))
    sandbox = spec.permissions.sandbox
    if sandbox in ("read_only", "workspace_write") or spec.permissions.network is False:
        items.append(_item("permissions.sandbox", "none", support="refused", enforced_by="none",
                           note="agy's terminal sandbox limits only its shell, and XO doesn't use it"))
    elif spec.permissions.network or spec.permissions.writable_roots:
        items.append(_item("permissions.sandbox", "none", support="ignored", enforced_by="none",
                           note="no sandbox runs, so there is nothing to widen"))
    stdio = [s.name for s in spec.mcp_servers if s.transport != "http"]
    if stdio:
        items.append(_item("mcp_servers", "none", support="refused", enforced_by="none",
                           note=f"only HTTP servers are relayed to agy: {', '.join(stdio)}"))
    elif spec.mcp_servers:
        items.append(_item("mcp_servers", "XO's agy plugin relays each to its HTTP server", enforced_by="cli"))
        if any(s.terminal_tools for s in spec.mcp_servers):
            items.append(_item("mcp_servers.terminal_tools", "the role prompt's note", support="emulated",
                               enforced_by="prompt", note="ending the turn after one would end a thread's turn at "
                                                          "its opening checklist"))
    if spec.host_tools:
        items.append(_item("host_tools", "none", support="refused", enforced_by="none",
                           note="host tools are not built for agy"))
    if spec.agents:
        items.append(_item("agents", "none", support="refused", enforced_by="none",
                           note="agy takes no sub-agent definitions per session"))
    if spec.isolation == "isolated":
        items.append(_item("isolation", "none", support="refused", enforced_by="none",
                           note="agy always loads the person's own settings and plugins"))
    else:
        items.append(_item("isolation", "the person's own agy setup", enforced_by="cli"))
    if spec.resume:
        items.append(_item("resume", "--conversation", enforced_by="cli"))
    if spec.fork or spec.fork_at:
        items.append(_item("fork", "none", support="refused", enforced_by="none",
                           note="agy can't fork a conversation from its command line"))
    if not spec.persist:
        items.append(_item("persist", "none", support="ignored", enforced_by="none",
                           note="agy keeps every conversation"))
    if spec.output_schema is not None:
        items.append(_item("output_schema", "--json-schema", enforced_by="cli"))
    items.append(_item("runs_detail", "the sub-agent's own transcript", support="emulated", enforced_by="cli"))
    items.append(_item("thinking", "the conversation's transcript, per step", support="emulated", enforced_by="cli"))
    limits = spec.limits
    if any(v is not None for v in (limits.max_usd, limits.max_turns, limits.max_subagents, limits.max_depth)):
        items.append(_item("limits", "none", support="ignored", enforced_by="none",
                           note="agy takes no per-session limits; XO sets none (§0.17)"))
    if spec.env:
        items.append(_item("env", "added to the env whitelist", enforced_by="cli"))
    if spec.extensions:
        items.append(_item("extensions", "none", support="ignored", enforced_by="none",
                           note=f"no extensions are defined for {AGENT}: {', '.join(sorted(spec.extensions))}"))
    if spec.debug_raw:
        items.append(_item("debug_raw", "Event.raw", enforced_by="cli"))
    report = SupportReport(items=tuple(items))
    refused = [i for i in items if i.support == "refused"]
    weak = [i for i in items if i.support in ("emulated", "ignored")]
    if refused:
        raise SpecRefused("agy can't run this spec: " + "; ".join(f"{i.feature} ({i.note})" for i in refused),
                          report)
    if spec.strict and weak:
        raise SpecRefused("strict spec: " + "; ".join(f"{i.feature} is {i.support}" for i in weak), report)
    return report


def _permission_mode(spec: SessionSpec) -> SupportItem:
    mode = spec.permissions.mode
    mechanism = "XO's hook answers each tool call"
    if spec.orchestration == "coordinator":
        if mode in a.COORDINATOR_REFUSED:
            return _item("permissions.mode", mechanism, support="refused", enforced_by="none",
                         note=f"in {mode}, a coordinator's workers would act unasked, so it can't be read-only")
        return _item("permissions.mode", mechanism, enforced_by="host",
                     note="every call but comms reaches the host, which answers with core's read-only policy")
    if mode == "auto":
        return _item("permissions.mode", mechanism, support="emulated",
                     note="trusted auto, as on codex: commands and edits run without host review; agy's own "
                          "auto needs its sandbox and isn't offered to this account. XO's hook still refuses "
                          "file-tool writes to protected paths")
    if mode == "bypass":
        return _item("permissions.mode", mechanism, note="nothing is checked but protected paths")
    if mode == "plan":
        return _item("permissions.mode", "--mode plan + XO's hook", note="no changes until the plan is approved")
    return _item("permissions.mode", mechanism, enforced_by="host")


def _tools_preset(spec: SessionSpec) -> SupportItem:
    if spec.tools.preset == "read_only" and spec.orchestration == "coordinator":
        return _item("tools.preset", "every call reaches the host's read-only policy", enforced_by="host")
    if spec.tools.preset in ("none", "read_only"):
        return _item("tools.preset", "none", support="refused", enforced_by="none",
                     note="agy can't drop its own tools; only a coordinator's are made read-only, by the host")
    return _item("tools.preset", "agy's default tool set", enforced_by="cli")


def features(cli_version: str | None, *, auth_env: tuple[str, ...]) -> FeatureList:
    native, emulated, unsupported = "native", "emulated", "unsupported"

    def f(support: str | None = native, note: str = "", behaviour: str | None = None,
          values: tuple[str, ...] = ()) -> FeatureSupport:
        return FeatureSupport(support=support, note=note, behaviour=behaviour, values=values)

    listed = {
        "cwd": f(note="the process's folder"),
        "add_dirs": f(note="--add-dir"),
        "model": f(note="--model; a change restarts agy on the same conversation"),
        "effort": f(values=EFFORTS, note="--effort"),
        "system_prompt": f(emulated, "a system message XO's hook adds at the conversation's start", values=("append",)),
        "tools": f(note="agy's own tools; XO's hook answers every call"),
        "permissions": f(note="every mode, answered by XO's hook; no sandbox"),
        "permissions.mode": f(values=a.MODES, note="XO's hook answers each call the way claude's modes would; a "
                                                   "coordinator's reach the host"),
        "permissions.auto": f(emulated, "trusted auto, as on codex: commands and edits run without host "
                                        "review, file-tool writes to protected paths are refused; agy's own auto "
                                        "needs its terminal sandbox and isn't offered to this account"),
        "permissions.sandbox": f(unsupported, "agy's sandbox limits only its shell; no session uses it"),
        "tools.deny": f(note="calls refused by XO's hook"),
        "orchestration": f(values=("none", "coordinator"), note="read-only by XO's policy; agy's own sub-agents"),
        "mcp_servers": f(note="HTTP servers, relayed by XO's agy plugin; their tokens stay in XO"),
        "host_tools": f(unsupported, "not built for agy"),
        "agents": f(unsupported, "agy takes no sub-agent definitions per session"),
        "isolation": f(values=("user",), note="agy always loads the person's own setup"),
        "resume": f(note="--conversation; a conversation agy doesn't have is reported, never replaced silently"),
        "fork": f(unsupported, "agy can't fork from its command line"),
        "fork_at": f(unsupported, "agy can't fork from its command line"),
        "persist": f(unsupported, "agy keeps every conversation"),
        "output_schema": f(note="--json-schema"),
        "runs_detail": f(emulated, "a sub-agent's steps come from its own transcript", values=("lifecycle", "steps")),
        "thinking": f(emulated, "per step, from the conversation's transcript", values=("off", "summary")),
        "limits": f(unsupported, "agy takes no per-session limits; XO sets none"),
        "env": f(note="on top of the env whitelist"),
        "extensions": f(unsupported, "none defined"),
        "strict": f(), "debug_raw": f(),
        "send_while_running": f(behaviour="queued", note="agy runs a message sent mid-turn as the next turn"),
        "interrupt": f(emulated, "SIGINT ends the turn and agy's process; the next send resumes the "
                                 "conversation", behaviour="stops_runs"),
        "update.model": f(emulated, "restarts agy on the same conversation"),
        "update.effort": f(emulated, "restarts agy on the same conversation"),
        "update.permission_mode": f(note="XO's hook answers by the new mode at once; plan restarts agy"),
        "runs.list": f(note="agy's own record of the conversation's sub-agents"),
        "runs.read": f(note="the sub-agent's transcript"),
        "runs.stop": f(unsupported, "agy has no way in to stop one sub-agent"),
        "runs.message": f(unsupported, "agy has no way in to message a sub-agent"),
        "models": f(note="agy models"),
        "usage": f(note="agy -p /usage, model-free"),
        "oneshot": f(emulated, "agy -p in an empty folder, every tool refused"),
        "project_config_paths": f(None, values=PROJECT_CONFIG_PATHS),
        "main_checkout_config_paths": f(None, values=MAIN_CHECKOUT_CONFIG_PATHS),
        "auth_env": f(None, values=auth_env),
        "tool_input_delta": f(unsupported, "agy reports a write once it is done"),
        "tool_output_delta": f(unsupported, "agy reports a command's output once it is done"),
        "input.images": f(emulated, "agy takes text only: an image's path goes in the message for it to read"),
        "permission_parking": f(native, "XO's hook waits as long as the person needs (held 11 minutes live)"),
    }
    return FeatureList(agent=AGENT, cli_version=cli_version, contract_version=CONTRACT_VERSION, features=listed)
