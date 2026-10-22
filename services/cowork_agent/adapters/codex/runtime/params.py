"""The app-server parameters for one session (storm ch 03 §3.2, §4).

Everything per session goes in ``thread/start`` (or ``thread/resume`` /
``thread/fork``) and each ``turn/start``; the process itself takes no
per-session flags. Every value comes from the ``SessionSpec``; nothing is
fixed by a role, except the two the owner fixed for every codex session
(§0.67): no sandbox (``danger-full-access``) and ``untrusted`` approvals, so
every action codex takes reaches this runtime, which answers it the way
claude's modes would (``session.py``).
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
COORDINATOR_ADDENDUM_ID = "codex.coordinator.v1"
COORDINATOR_ADDENDUM = ("You can't change files yourself. Delegate reading to sub-agents with spawn_agent, and "
                        "wait for them with wait_agent; never guess.")
#: The note that stands in for claude's terminal tools: codex has no switch
#: that ends a turn after a tool (reported ``emulated``, enforced by the prompt).
TERMINAL_NOTE = "After a successful call to {tools}, end your turn: say nothing more."
#: Where a session's own edits go through unasked (as claude's acceptEdits),
#: codex is told how to make them: its shell writes always wait for XO's policy.
EDIT_NOTE = ("Change files with apply_patch, not shell redirection, heredocs or sed -i: your own edits in this "
             "folder then go through without asking, while a command that writes files waits for approval.")
#: Where XO's hidden per-turn context goes (``turn/start.additionalContext``).
CONTEXT_KEY = "xo"
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def approval_policy(mode: str) -> str:
    """codex's approval policy for a contract mode: ``never`` only for
    ``bypass``; every other mode asks this runtime about everything."""
    return "never" if mode == "bypass" else "untrusted"


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
        out[server.name] = entry
    return out


def instructions(spec: SessionSpec, mode: str) -> str | None:
    """The role text, plus the coordinator addendum, how to edit files, and
    the terminal-tools note."""
    parts = [spec.system_prompt.text] if spec.system_prompt and spec.system_prompt.text else []
    if spec.orchestration == "coordinator":
        parts.append(COORDINATOR_ADDENDUM)
    elif mode in ("accept_edits", "auto"):
        parts.append(EDIT_NOTE)
    terminal = [f"`{t}` on `{s.name}`" for s in spec.mcp_servers for t in s.terminal_tools]
    if terminal:
        parts.append(TERMINAL_NOTE.format(tools=", ".join(terminal)))
    return "\n\n".join(parts) if parts else None


def web_search(spec: SessionSpec) -> bool:
    """Whether codex may search the web: not for a coordinator, nor when the
    spec denies it (codex runs its web search itself, asking nobody)."""
    if spec.orchestration == "coordinator":
        return False
    return not ({"web_search", "web_fetch"} & set(spec.tools.deny))


def thread_params(spec: SessionSpec, *, mode: str) -> dict[str, Any]:
    """``thread/start``'s parameters (``thread/resume`` and ``thread/fork``
    take the same)."""
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
