"""The app-server parameters for one session (storm ch 03 §3.2, §4).

Everything per session goes in ``thread/start`` (or ``thread/resume`` /
``thread/fork``) and each ``turn/start``; the process itself takes no
per-session flags. Every value comes from the ``SessionSpec``; nothing is
fixed by a role. Sessions use no sandbox (``danger-full-access``).
Trusted ``auto`` and ``bypass`` sessions use ``never`` approvals; explicit
review modes use ``untrusted`` so their actions reach this runtime.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any

from services.cowork_agent.adapters.contract import McpServer, RuntimeFailure, SessionSpec, UserInput

SANDBOX = "danger-full-access"
#: The modes this runtime offers (contract ``PermissionMode``).
MODES = ("ask", "accept_edits", "plan", "deny_unasked", "auto", "bypass")
#: Modes in which a coordinator's workers would act unasked: refused for one.
COORDINATOR_REFUSED = frozenset({"accept_edits", "auto", "bypass"})
#: codex's coordinator addendum (storm ch 10 §7.2), reported as ``system_prompt.addendum``.
#: A coordinator's read-only shell commands pass XO's read-only check
#: (``presets/coordinators.py``), so it reads code itself.
COORDINATOR_ADDENDUM_ID = "codex.coordinator.v2"
COORDINATOR_ADDENDUM = ("You can't change files: XO denies edits and any command that writes. Read code yourself "
                        "with read-only shell commands (rg, sed -n, git log, git show); for wide reading you may use "
                        "sub-agents. Never guess.")
#: The note that stands in for claude's terminal tools: codex has no switch
#: that ends a turn after a tool (reported ``emulated``, enforced by the prompt).
#: It defers to the role prompt's own list of endings (a hand-off such as
#: ``message_coordinator`` ends a turn too), naming the comms server's
#: terminal tools only as examples, so the two can't drift apart. An early
#: status update must not read as the turn's end (a thread once stopped
#: after its opening checklist and did nothing).
TERMINAL_NOTE = ("End every turn with one of the calls your instructions above name for ending a turn (such as "
                 "{tools}, or a call that hands the work on) as your last call, and say nothing after it. One "
                 "such call is the whole ending: after a hand-off, don't add a chat message too. Calling one "
                 "doesn't end the turn by itself: a status or checklist update before or during the work is not "
                 "the end, so carry on with the work in the same turn. Only once the task is done, or there is "
                 "nothing to do, make that last call and stop.")
#: codex gives a sub-agent its parent's instructions and MCP servers (checked
#: live on codex 0.164.0), where claude keeps comms to the root (``role:
#: comms``). Only the prompt can keep a sub-agent off the comms tools.
SUBAGENT_NOTE = ("Sub-agents you start get these same instructions and tools, but only you talk through {servers}. "
                 "Tell each sub-agent in its task that it is a sub-agent: it never calls a {servers} tool, and "
                 "reports back to you in its final message. If your task says you are a sub-agent, that is you: "
                 "ignore the turn endings above, never call a {servers} tool, and end with your report.")
#: Where a session's own edits may go through unasked (as claude's
#: acceptEdits), codex is told to make them with apply_patch. Set at thread
#: start while the mode can change later, so it promises no mode's outcome.
EDIT_NOTE = ("Change files with apply_patch, not shell redirection, heredocs or sed -i: a patch is checked as an "
             "edit of the files it names, while a command that writes files is checked as a command and may "
             "need approval.")
#: Where XO's hidden per-turn context goes (``turn/start.additionalContext``).
CONTEXT_KEY = "xo"
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
COMMS_START_TIMEOUT_S = 10.0


def approval_policy(mode: str) -> str:
    """Trusted auto sessions run without approval round trips.

    Explicit review modes retain host checks; coordinators cannot use auto.
    """
    return "never" if mode in ("auto", "bypass") else "untrusted"


def is_comms(server: McpServer) -> bool:
    """XO's comms server, by the spec's own markers: its tools need no
    permission, or a successful call ends the turn."""
    return server.auto_approve or bool(server.terminal_tools)


def mcp_servers(spec: SessionSpec) -> dict[str, dict[str, Any]]:
    """``config.mcp_servers``: an HTTP server's bearer token stays in the
    process env, named by ``bearer_token_env_var`` and never written (X2)."""
    out: dict[str, dict[str, Any]] = {}
    for server in spec.mcp_servers:
        if server.transport == "stdio":
            entry: dict[str, Any] = {"command": server.command[0] if server.command else "",
                                     "args": list(server.command[1:])}
            if server.env:
                entry["env"] = dict(server.env)
        else:
            entry = {"url": server.url or ""}
            if server.bearer_env:
                entry["bearer_token_env_var"] = server.bearer_env
        if server.auto_approve:
            entry["default_tools_approval_mode"] = "approve"
        if server.terminal_tools:
            # A conversation cannot deliver its result without these tools.
            # Auto-approval alone does not make an optional integration required.
            entry.update(required=True, enabled=True, startup_timeout_sec=COMMS_START_TIMEOUT_S)
        out[server.name] = entry
    return out


def instructions(spec: SessionSpec, mode: str) -> str | None:
    """The role text, plus the coordinator addendum, how to edit files, the
    terminal-tools note, and what a sub-agent may do with comms."""
    parts = [spec.system_prompt.text] if spec.system_prompt and spec.system_prompt.text else []
    if spec.orchestration == "coordinator":
        parts.append(COORDINATOR_ADDENDUM)
    elif mode in ("accept_edits", "auto"):
        parts.append(EDIT_NOTE)
    terminal = [f"`{t}` on `{s.name}`" for s in spec.mcp_servers for t in s.terminal_tools]
    if terminal:
        parts.append(TERMINAL_NOTE.format(tools=", ".join(terminal)))
    comms = [f"`{s.name}`" for s in spec.mcp_servers if is_comms(s)]
    if comms:
        parts.append(SUBAGENT_NOTE.format(servers=" or ".join(comms)))
    return "\n\n".join(parts) if parts else None


def web_search(spec: SessionSpec) -> bool:
    """Whether codex may search the web: not for a coordinator, nor when the
    spec denies it (codex runs its web search itself, asking nobody)."""
    if spec.orchestration == "coordinator":
        return False
    return not ({"web_search", "web_fetch"} & set(spec.tools.deny))


def thread_params(spec: SessionSpec, *, mode: str) -> dict[str, Any]:
    """``thread/start``'s parameters."""
    params: dict[str, Any] = {"cwd": spec.cwd, "approvalPolicy": approval_policy(mode), "sandbox": SANDBOX,
                              "approvalsReviewer": "user", "ephemeral": not spec.persist}
    if spec.model:
        params["model"] = spec.model
    text = instructions(spec, mode)
    if text:
        params["baseInstructions" if spec.system_prompt and spec.system_prompt.mode == "replace"
               else "developerInstructions"] = text
    config: dict[str, Any] = {"model_reasoning_summary": "detailed" if spec.thinking == "summary" else "none"}
    servers = mcp_servers(spec)
    if servers:
        config["mcp_servers"] = servers
    if not web_search(spec):
        config["web_search"] = "disabled"
    params["config"] = config
    return params


def resume_params(spec: SessionSpec, *, mode: str) -> dict[str, Any]:
    """Restore native context without returning the unused transcript to XO."""
    params = thread_params(spec, mode=mode)
    params.pop("ephemeral")      # thread/resume has no such field
    return {"threadId": spec.resume, **params, "excludeTurns": True}


def turn_params(thread_id: str, msg: UserInput, *, spec: SessionSpec, mode: str, model: str | None,
                collaboration: str | None = None) -> dict[str, Any]:
    """``turn/start``'s parameters. ``collaboration``: codex's collaboration
    mode to switch to (``plan`` for the plan mode, ``default`` to leave it),
    None to keep the thread's; ``model``: the thread's model, which a
    collaboration mode must name."""
    params: dict[str, Any] = {"threadId": thread_id, "input": user_input(msg),
                              "approvalPolicy": approval_policy(mode)}
    if spec.effort:
        params["effort"] = spec.effort
    if spec.model:
        params["model"] = spec.model
    if spec.output_schema is not None:
        params["outputSchema"] = dict(spec.output_schema)
    if msg.context:
        params["additionalContext"] = context(msg.context)
    if collaboration and (spec.model or model):
        params["collaborationMode"] = {"mode": collaboration, "settings": {"model": spec.model or model,
                                                                            "reasoning_effort": spec.effort}}
    return params


def context(text: str) -> dict[str, Any]:
    return {CONTEXT_KEY: {"kind": "application", "value": text}}


def user_input(msg: UserInput) -> list[dict[str, Any]]:
    """The turn's input: the person's text, then images by path (codex reads
    them itself). Blocking (checks the images): run it in a thread."""
    items: list[dict[str, Any]] = [{"type": "text", "text": msg.text}]
    for path in msg.images:
        if mimetypes.guess_type(path)[0] not in IMAGE_TYPES:
            raise RuntimeFailure("unsupported", f"codex takes PNG, JPEG, GIF or WebP images, not {path}")
        if not os.path.isfile(path):
            raise RuntimeFailure("spec_refused", f"the image {path} can't be read")
        items.append({"type": "localImage", "path": path})
    return items


def note_input(text: str) -> list[dict[str, Any]]:
    """A system note steered into a running turn: marked as XO's, never the
    person's words (§0.69)."""
    return [{"type": "text", "text": f"[XO system note, not the person's words] {text}"}]
