"""What the codex runtime supports: ``check(spec)`` (storm ch 01 §6) and the
feature list (ch 01 §4.8, ch 03 §2–§10). Both are pure: they start nothing.

Each spec field gets a verdict and who enforces it. ``refused`` items make
``check`` raise ``SpecRefused``, as do ``emulated``/``ignored`` ones under
``strict``. Core's own safety rule (a limit enforced only by the prompt or by
nothing refuses the session) reads the same report.
"""

from __future__ import annotations

from services.cowork_agent.adapters.codex.runtime import params as p
from services.cowork_agent.adapters.codex.runtime.tools import PROJECT_CONFIG_PATHS
from services.cowork_agent.adapters.contract import (
    CONTRACT_VERSION,
    FeatureList,
    FeatureSupport,
    SessionSpec,
    SpecRefused,
    SupportItem,
    SupportReport,
)

AGENT = "codex"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: What codex reads in the project's main checkout while it runs in one of
#: its worktrees: nothing found (K4: a worktree's own ``.codex/`` loads only
#: once codex trusts it; the main checkout's isn't read for it).
MAIN_CHECKOUT_CONFIG_PATHS: tuple[str, ...] = ()
#: The neutral tool refs this runtime can deny, and how: by declining the
#: request codex sends for it (``host``) or by codex's own config (``cli``).
DENIABLE = {"edit": "host", "create": "host", "delete": "host", "shell": "host", "web_fetch": "cli",
            "web_search": "cli", "worktree": "cli", "schedule": "cli"}
#: Tool refs codex has no tool for: nothing to deny.
ABSENT = frozenset({"worktree", "schedule"})


def _item(feature: str, mechanism: str, *, support: str = "native", enforced_by: str = "cli",
          note: str = "") -> SupportItem:
    return SupportItem(feature=feature, support=support, mechanism=mechanism, enforced_by=enforced_by, note=note)


def check(spec: SessionSpec) -> SupportReport:
    coordinator = spec.orchestration == "coordinator"
    items = [_item("cwd", "thread/start.cwd"), _permission_mode(spec), _tools_preset(spec)]
    if spec.add_dirs:
        items.append(_item("add_dirs", "none", support="ignored", enforced_by="none",
                           note="codex runs with no sandbox (§0.67): every folder is reachable already"))
    if spec.model:
        items.append(_item("model", "thread/start.model, turn/start.model"))
    if spec.effort:
        items.append(_item("effort", "turn/start.effort"))
    if spec.system_prompt:
        items.append(_item("system_prompt", "thread/start.baseInstructions" if spec.system_prompt.mode == "replace"
                           else "thread/start.developerInstructions"))
    if coordinator:
        items.append(_item("orchestration", "file changes and commands to XO's read-only policy; codex's own "
                                            "sub-agents", enforced_by="host"))
        items.append(_item("system_prompt.addendum", p.COORDINATOR_ADDENDUM_ID, enforced_by="prompt"))
    if spec.tools.allow:
        items.append(_item("tools.allow", "XO's policy (the host)", enforced_by="host"))
    if spec.tools.ask:
        items.append(_item("tools.ask", "every action reaches the host already", enforced_by="host"))
    if spec.tools.deny:
        missing = [ref for ref in spec.tools.deny if ref not in DENIABLE]
        if missing:
            items.append(_item("tools.deny", "none", support="refused", enforced_by="none",
                               note=f"codex can't be kept from {', '.join(missing)}"))
        else:
            by = {DENIABLE[ref] for ref in spec.tools.deny}
            items.append(_item("tools.deny", "declined requests + config.web_search",
                               enforced_by="host" if "host" in by else "cli"))
    sandbox = spec.permissions.sandbox
    if sandbox in ("read_only", "workspace_write") or spec.permissions.network is False:
        items.append(_item("permissions.sandbox", "none", support="refused", enforced_by="none",
                           note="codex runs with no sandbox here (§0.67; its sandbox can't start in this container)"))
    elif spec.permissions.network or spec.permissions.writable_roots:
        items.append(_item("permissions.sandbox", "none", support="ignored", enforced_by="none",
                           note="no sandbox runs, so there is nothing to widen"))
    if spec.mcp_servers:
        items.append(_item("mcp_servers", "thread/start.config.mcp_servers"))
        if any(s.terminal_tools for s in spec.mcp_servers):
            items.append(_item("mcp_servers.terminal_tools", "developer instructions", support="emulated",
                               enforced_by="prompt", note="codex has no switch that ends a turn after a tool"))
    if spec.host_tools:
        items.append(_item("host_tools", "none", support="refused", enforced_by="none",
                           note="host tools (codex's dynamic tools) are not built"))
    if spec.agents:
        items.append(_item("agents", "none", support="refused", enforced_by="none",
                           note="codex takes no sub-agent definitions per session"))
    if spec.isolation == "isolated":
        items.append(_item("isolation", "none", support="refused", enforced_by="none",
                           note="codex always loads the person's own config.toml"))
    else:
        items.append(_item("isolation", "codex's own config.toml"))
    if spec.resume:
        items.append(_item("resume", "thread/resume"))
    if spec.fork:
        items.append(_item("fork", "thread/fork") if spec.resume else
                     _item("fork", "thread/fork", support="refused", enforced_by="none",
                           note="a fork needs a thread to fork"))
    if spec.fork_at:
        items.append(_item("fork_at", "none", support="refused", enforced_by="none",
                           note="codex forks a thread whole, never at a turn"))
    if not spec.persist:
        items.append(_item("persist", "thread/start.ephemeral", note="an ephemeral thread can't spawn sub-agents"))
    if spec.output_schema is not None:
        items.append(_item("output_schema", "turn/start.outputSchema"))
    items.append(_item("runs_detail", "filtered from the stream"))
    items.append(_item("thinking", "config.model_reasoning_summary"))
    limits = spec.limits
    if any(v is not None for v in (limits.max_usd, limits.max_turns, limits.max_subagents, limits.max_depth)):
        items.append(_item("limits", "none", support="ignored", enforced_by="none",
                           note="codex takes no per-session limits; XO sets none (§0.17)"))
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
        raise SpecRefused("codex can't run this spec: " + "; ".join(f"{i.feature} ({i.note})" for i in refused),
                          report)
    if spec.strict and weak:
        raise SpecRefused("strict spec: " + "; ".join(f"{i.feature} is {i.support}" for i in weak), report)
    return report


def _permission_mode(spec: SessionSpec) -> SupportItem:
    mode = spec.permissions.mode
    mechanism = f"approvalPolicy {p.approval_policy(mode)} + this runtime's answers"
    if spec.orchestration == "coordinator":
        if mode in p.COORDINATOR_REFUSED:
            return _item("permissions.mode", mechanism, support="refused", enforced_by="none",
                         note=f"in {mode}, a coordinator's workers would act unasked, so it can't be read-only")
        return _item("permissions.mode", mechanism, enforced_by="host",
                     note="every change and command a coordinator asks about reaches the host, which answers "
                          "with core's read-only policy")
    if mode == "auto":
        return _item("permissions.mode", mechanism, support="emulated", enforced_by="host",
                     note="trusted auto uses approvalPolicy never: commands and edits run without host review; "
                          "MCP requests that still reach the host use XO's policy")
    if mode == "bypass":
        return _item("permissions.mode", "approvalPolicy never", note="nothing is checked")
    return _item("permissions.mode", mechanism, enforced_by="host")


def _tools_preset(spec: SessionSpec) -> SupportItem:
    if spec.tools.preset == "read_only" and spec.orchestration == "coordinator":
        return _item("tools.preset", "changes and commands reach the host's read-only policy", enforced_by="host")
    if spec.tools.preset in ("none", "read_only"):
        return _item("tools.preset", "none", support="refused", enforced_by="none",
                     note="codex can't drop its own tools; only a coordinator's are made read-only, by the host")
    return _item("tools.preset", "codex's default tool set")


def features(cli_version: str | None, *, auth_env: tuple[str, ...]) -> FeatureList:
    native, emulated, unsupported = "native", "emulated", "unsupported"

    def f(support: str | None = native, note: str = "", behaviour: str | None = None,
          values: tuple[str, ...] = ()) -> FeatureSupport:
        return FeatureSupport(support=support, note=note, behaviour=behaviour, values=values)

    listed = {
        "cwd": f(note="thread/start.cwd"),
        "add_dirs": f(unsupported, "no sandbox runs (§0.67): every folder is reachable already"),
        "model": f(note="thread/start.model; changes with the next turn/start"),
        "effort": f(values=EFFORTS, note="turn/start.effort; codex's ultra has no contract value"),
        "system_prompt": f(values=("append", "replace"), note="developerInstructions / baseInstructions"),
        "tools": f(note="codex's own tools; explicit review modes send approval requests to XO"),
        "permissions": f(note="no sandbox; auto/bypass use never, explicit review modes use untrusted"),
        "permissions.mode": f(values=p.MODES, note="this runtime answers codex's approval requests the way "
                                                   "claude's modes would; a coordinator's reach the host"),
        "permissions.auto": f(emulated, "trusted auto uses approvalPolicy never for commands and edits; "
                                        "MCP requests that still reach the host use XO's policy"),
        "permissions.sandbox": f(unsupported, "codex's sandbox can't start in this container; no session uses it"),
        "tools.deny": f(note="requests declined by this runtime; web search off in codex's config"),
        "orchestration": f(values=("none", "coordinator"), note="read-only by XO's policy; codex's own sub-agents"),
        "mcp_servers": f(note="thread/start.config.mcp_servers; HTTP bearer tokens travel in the env"),
        "host_tools": f(unsupported, "codex's dynamic tools are not built"),
        "agents": f(unsupported, "codex takes no sub-agent definitions per session"),
        "isolation": f(values=("user",), note="codex always loads the person's config.toml"),
        "resume": f(note="thread/resume"), "fork": f(note="thread/fork"),
        "fork_at": f(unsupported, "codex forks a thread whole"),
        "persist": f(note="thread/start.ephemeral; an ephemeral thread can't spawn sub-agents"),
        "output_schema": f(note="turn/start.outputSchema"),
        "runs_detail": f(values=("lifecycle", "steps", "text")),
        "thinking": f(values=("off", "summary"), note="reasoning summaries (model_reasoning_summary)"),
        "limits": f(unsupported, "codex takes no per-session limits; XO sets none"),
        "env": f(note="on top of the env whitelist"),
        "extensions": f(unsupported, "none defined"),
        "strict": f(), "debug_raw": f(),
        "send_while_running": f(behaviour="steered", note="turn/steer joins the running turn"),
        "interrupt": f(note="turn/interrupt; live sub-agents go on in the background (L10)"),
        "update.model": f(note="the next turn/start"),
        "update.effort": f(note="the next turn/start"),
        "update.permission_mode": f(note="this runtime's answers, at once"),
        "runs.list": f(note="codex's transcript files of the thread's sub-agents"),
        "runs.read": f(note="the sub-agent's transcript file"),
        "runs.stop": f(note="turn/interrupt on the sub-agent, then a system note to the root (§0.69)"),
        "runs.message": f(unsupported, "not offered this release (§0.82)"),
        "models": f(note="model/list"),
        "usage": f(note="account/rateLimits"),
        "oneshot": f(note="codex exec --json, read-only, ephemeral, in an empty folder"),
        "project_config_paths": f(None, values=PROJECT_CONFIG_PATHS),
        "main_checkout_config_paths": f(None, values=MAIN_CHECKOUT_CONFIG_PATHS),
        "auth_env": f(None, values=auth_env),
        "tool_input_delta": f(note="a patch's bytes as codex writes it (item/fileChange/patchUpdated)"),
        "tool_output_delta": f(note="a command's output as it runs"),
        "input.images": f(values=tuple(sorted(p.IMAGE_TYPES)), note="localImage inputs"),
        "permission_parking": f(emulated, "deny naming the review item, then grant + wake"),
    }
    return FeatureList(agent=AGENT, cli_version=cli_version, contract_version=CONTRACT_VERSION, features=listed)
