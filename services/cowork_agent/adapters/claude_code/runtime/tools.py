"""claude's tools in the contract's neutral terms (storm ch 02 §3.2, §5.2).

Two directions: a neutral ``ToolRef`` becomes claude permission rules for the
command line, and each tool call claude makes becomes a ``ToolKind`` with a
title, a target and the neutral fields core's policy reads
(``PermissionRequest.paths/command/url/mcp``), so core never reads claude's
own input keys.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from services.cowork_agent.adapters.contract import ToolKind

#: Neutral tool names and the claude tools they stand for.
REF_TOOLS: dict[str, tuple[str, ...]] = {
    "read": ("Read",),
    "search": ("Grep", "Glob"),
    "list": ("LS",),
    "edit": ("Edit", "MultiEdit", "NotebookEdit"),
    "create": ("Write",),
    "shell": ("Bash",),
    "web_fetch": ("WebFetch",),
    "web_search": ("WebSearch",),
    "subagents": ("Agent", "Task"),
    "ask_user": ("AskUserQuestion",),
    "plan": ("TodoWrite", "ExitPlanMode"),
    "worktree": ("EnterWorktree", "ExitWorktree"),
    "schedule": ("CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "RemoteTrigger"),
}

#: The tools whose input carries a file's new content, streamed as it is
#: written (``ToolInputDelta``), and the input key that holds it.
CONTENT_KEYS: dict[str, str] = {"Write": "content", "Edit": "new_string", "NotebookEdit": "new_source"}

SUMMARY_MAX = 200
#: Input fields that only describe a call for a person, never change what it
#: does: claude rewrites a command's ``description`` on every retry.
DISPLAY_ONLY: dict[str, frozenset[str]] = {"Bash": frozenset({"description"}),
                                          "Agent": frozenset({"description"}),
                                          "Task": frozenset({"description"})}
#: Input fields the neutral request carries itself, where the policy hashes
#: them in canonical form (a command without the whitespace the shell
#: ignores); kept out of ``content_sha256`` so they are hashed once, that way.
NEUTRAL_FIELDS: dict[str, frozenset[str]] = {"Bash": frozenset({"command"})}


def rules_for(ref: str) -> tuple[str, ...] | None:
    """claude permission rules for one neutral ``ToolRef``; None when claude
    has no such tool (``delete``: claude deletes through its shell)."""
    if ref in REF_TOOLS:
        return REF_TOOLS[ref]
    head, sep, rest = ref.partition(":")
    if not sep or not rest:
        return None
    if head == "shell":
        return (f"Bash({rest}:*)",)
    if head == "mcp":
        server, _, tool = rest.partition("/")
        return (f"mcp__{server}__{tool or '*'}",) if server else None
    if head == "native":
        return (rest,)
    return None


def mcp_parts(tool: str) -> tuple[str, str] | None:
    """``mcp__<server>__<tool>`` as (server, tool)."""
    if not tool.startswith("mcp__"):
        return None
    server, sep, name = tool[len("mcp__"):].partition("__")
    return (server, name) if server and sep and name else None


@dataclass(frozen=True)
class Call:
    """One tool call in neutral terms."""

    kind: ToolKind
    title: str
    target: str | None
    summary: str
    paths: tuple[str, ...] = ()
    command: str | None = None
    url: str | None = None
    mcp: tuple[str, str] | None = None
    plan: str | None = None


def content_sha256(tool: str, args: object) -> str:
    """``PermissionRequest.content_sha256``: a digest of everything in the
    call's input that defines what it does (a write's content, an edit's
    change, an MCP call's or a sub-agent's arguments), without the
    display-only fields and the fields the neutral request carries."""
    inp = args if isinstance(args, dict) else {}
    left_out = DISPLAY_ONLY.get(tool, frozenset()) | NEUTRAL_FIELDS.get(tool, frozenset())
    kept = {k: v for k, v in inp.items() if k not in left_out}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def describe_call(tool: str, args: object, *, cwd: str | None, live: bool = True) -> Call:
    """Classify one call. Unknown tools are ``other``, never an error.
    ``live``: the call is being made now, so a ``Write`` whose file exists
    edits it. Read back from a transcript the file exists either way, so a
    ``Write`` there is ``create_file``: what it does is write the whole file."""
    inp = args if isinstance(args, dict) else {}

    def text(key: str) -> str:
        value = inp.get(key)
        return value if isinstance(value, str) else ""

    def path(key: str) -> str:
        raw = text(key)
        return os.path.normpath(os.path.join(cwd, raw)) if raw and cwd and not os.path.isabs(raw) else raw

    if tool == "Write":
        target = path("file_path")
        existed = live and bool(target) and os.path.exists(target)
        kind: ToolKind = "edit_file" if existed else "create_file"
        verb = "edit" if existed else "create"
        return Call(kind, one_line(f"Write `{_name(target)}`"), target or None, one_line(f"{verb} `{target}`"),
                    paths=_some(target))
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        target = path("notebook_path" if tool == "NotebookEdit" else "file_path")
        return Call("edit_file", one_line(f"Edit `{_name(target)}`"), target or None, one_line(f"edit `{target}`"),
                    paths=_some(target))
    if tool == "Read":
        target = path("file_path")
        return Call("read", one_line(f"Read `{_name(target)}`"), target or None, one_line(f"read `{target}`"),
                    paths=_some(target))
    if tool in ("Grep", "Glob"):
        pattern = text("pattern")
        where = path("path")
        return Call("search", one_line(f"Search for `{pattern}`"), pattern or None,
                    one_line(f"search for `{pattern}`" + (f" in `{where}`" if where else "")), paths=_some(where))
    if tool == "LS":
        target = path("path")
        return Call("list", one_line(f"List `{_name(target)}`"), target or None, one_line(f"list `{target}`"),
                    paths=_some(target))
    if tool == "Bash":
        command = text("command")
        title = text("description") or command
        return Call("command", one_line(title), command or None, one_line(f"run `{command}`"), command=command or None)
    if tool == "WebFetch":
        url = text("url")
        host = urlsplit(url).hostname or url
        return Call("web_fetch", one_line(f"Read `{host}`"), url or None, one_line(f"fetch `{url}`"), url=url or None)
    if tool == "WebSearch":
        query = text("query")
        return Call("web_search", one_line(f"Search the web for {query}"), query or None,
                    one_line(f"search the web for `{query}`"), url=query or None)
    if tool in ("Agent", "Task"):
        description = text("description") or "a sub-agent"
        kind_name = text("subagent_type") or None
        return Call("subagent", one_line(description), kind_name, one_line(f"start a sub-agent: {description}"))
    if tool == "ExitPlanMode":       # plan mode's end: the plan the person approves
        return Call("plan", "Update the plan", None, "update the plan", plan=text("plan") or None)
    if tool == "TodoWrite":
        return Call("plan", "Update the plan", None, "update the plan")
    if tool == "AskUserQuestion":
        questions = inp.get("questions")
        first = questions[0].get("question") if isinstance(questions, list) and questions \
            and isinstance(questions[0], dict) else None
        title = one_line(first if isinstance(first, str) and first else "A question")
        return Call("ask", title, None, one_line(f"ask: {title}"))
    parts = mcp_parts(tool)
    if parts:
        server, name = parts
        return Call("mcp", one_line(f"`{name}` on `{server}`"), f"{server}/{name}",
                    one_line(f"call `{name}` on `{server}`"), mcp=parts)
    return Call("other", one_line(tool or "a tool"), None, one_line(f"use {tool or 'a tool'}"))


def _name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def _some(path: str) -> tuple[str, ...]:
    return (path,) if path else ()


def one_line(text: str, limit: int = SUMMARY_MAX) -> str:
    """One display line: the first line, cut to ``limit`` characters."""
    first = " ".join(text.split("\n", 1)[0].split())
    return first if len(first) <= limit else first[: limit - 1] + "…"
