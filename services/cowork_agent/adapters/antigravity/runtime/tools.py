"""agy's tool calls in the contract's neutral terms (storm ch 04 §8).

agy reports a call twice: on stdout (``step_update`` of type ``tool``, its
parameters trimmed to the path or command) and to XO's pre-tool hook, with
its whole arguments. Both go through ``describe``, so a call has the same
kind, title and target either way. agy 1.4.10 calls its file tools
``write_file``, ``edit_file`` and ``read_file`` while its ``init`` lists
``write_to_file``, ``replace_file_content`` and ``view_file``; both names are
known. A tool this module doesn't know is ``other``, and goes to XO.

It also decides what the runtime answers itself:

* ``own_edit``: a write or edit whose every path is inside the session's
  folder and none protected (XO's and git's state, and every agent's project
  config, ``.agents/**`` included: agy runs a workspace's
  ``.agents/hooks.json`` as code, before XO's own hook);
* ``artifact_write``: a write into agy's own folder for the conversation
  (its plans, task lists, walkthroughs), never into the project;
* ``INTERNAL``: agy's bookkeeping between its own agents (waiting, a sub-agent's
  report, listing them), which acts on nothing outside agy.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from services.cowork_agent.adapters.contract import ToolKind
from services.cowork_agent.registry.agent_registry import all_agents

SUMMARY_MAX = 200
#: The files in a checkout that make agy run code on its own: its workspace
#: hooks, plugins, rules, agents and MCP servers. The integrity check watches
#: them (storm ch 16 §2.1) and no edit of them is accepted unasked.
PROJECT_CONFIG_PATHS = (".agents/**",)
CORE_PROTECTED = (".xo/**", ".git/**")

WRITES = frozenset({"write_file", "write_to_file"})
EDITS = frozenset({"edit_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit"})
READS = frozenset({"read_file", "view_file", "view_file_outline", "view_code_item", "view_content_chunk"})
LISTS = frozenset({"list_dir"})
SEARCHES = frozenset({"grep_search", "find_by_name", "codebase_search"})
COMMANDS = frozenset({"run_command", "send_command_input"})
SUBAGENTS = frozenset({"invoke_subagent", "define_subagent", "browser_subagent"})
QUESTIONS = frozenset({"ask_question"})
#: agy's own permission tools: XO decides each action when it is taken, so
#: these are answered by the runtime ("go ahead and try it").
PERMISSION_ASKS = frozenset({"ask_permission", "ask_custom_permission"})
#: Bookkeeping between agy's own agents: approved by the runtime in every mode.
INTERNAL = frozenset({"wait", "wait_5_seconds", "finish", "send_message", "manage_subagents", "command_status",
                      "list_permissions", "list_resources", "list_plugin_accounts"})

_PATH_KEYS = ("Path", "TargetFile", "AbsolutePath", "File", "FilePath", "path", "file_path", "target_file")
_DIR_KEYS = ("DirectoryPath", "SearchPath", "SearchDirectory", "Directory", "Path", "path")
_QUERY_KEYS = ("Query", "Pattern", "SearchPattern", "query", "pattern")
_CONTENT_KEYS = ("Content", "CodeContent", "Replacement", "ReplacementContent", "ReplacementChunks", "Target",
                 "TargetContent", "Instruction", "Edits", "Script", "content")


@dataclass(frozen=True)
class Call:
    """One tool call in neutral terms."""

    kind: ToolKind
    tool: str                 # agy's own name for it, for display
    title: str
    target: str | None
    summary: str
    paths: tuple[str, ...] = ()
    command: str | None = None
    url: str | None = None
    mcp: tuple[str, str] | None = None
    content_sha256: str | None = None


def one_line(text: str, limit: int = SUMMARY_MAX) -> str:
    first = " ".join(str(text).split("\n", 1)[0].split())
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _first(args: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _name(path: str) -> str:
    return os.path.basename(path.rstrip(os.sep)) or path


def _digest(args: Mapping[str, Any]) -> str:
    body = {k: args[k] for k in _CONTENT_KEYS if k in args}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def absolute(path: str, cwd: str) -> str:
    path = os.path.expanduser(path)
    if path.startswith("file://"):
        path = path[len("file://"):]
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


#: agy's own name for an MCP tool it loaded up front (``_meta`` ``antigravity/alwaysLoad``,
#: ``mcp_relay.always_load``): ``mcp_<server>_<tool>``, called directly, not through call_mcp_tool.
DIRECT_MCP_PREFIX = "mcp_"
#: The argument agy adds to a direct MCP call for its own display; the server never gets it.
TOOL_SUMMARY = "toolSummary"


def direct_mcp(tool: str, servers: Iterable[str]) -> tuple[str, str] | None:
    """(agy's server name, tool) for a direct MCP call to one of ``servers``
    (agy's names). Server and tool names both hold underscores, so only a
    known server's name can split one."""
    if not tool.startswith(DIRECT_MCP_PREFIX):
        return None
    rest = tool[len(DIRECT_MCP_PREFIX):]
    for server in sorted(servers, key=len, reverse=True):
        if rest.startswith(server + "_") and len(rest) > len(server) + 1:
            return server, rest[len(server) + 1:]
    return None


def describe(tool: str, args: Mapping[str, Any] | None, cwd: str, *, exists=os.path.exists,
             servers: Iterable[str] = ()) -> Call:
    """One call of agy's ``tool`` with ``args`` (whole, from the hook, or
    trimmed, from stdout). ``exists`` tells a write that creates a file from
    one that overwrites it; ``servers`` (agy's names for XO's MCP servers)
    tells a direct MCP call."""
    args = args if isinstance(args, Mapping) else {}
    direct = direct_mcp(tool, servers)
    if direct is not None:
        server, name = direct
        return Call("mcp", tool, one_line(f"`{name}` on `{server}`"), f"{server}/{name}",
                    one_line(f"call `{name}` on `{server}`"), mcp=(server, name),
                    content_sha256=hashlib.sha256(json.dumps({k: v for k, v in args.items() if k != TOOL_SUMMARY},
                                                             sort_keys=True, default=str).encode()).hexdigest())
    if tool in WRITES or tool in EDITS:
        path = _first(args, _PATH_KEYS)
        full = absolute(path, cwd) if path else None
        creates = tool in WRITES and not (full and exists(full))
        kind: ToolKind = "create_file" if creates else "edit_file"
        verb = "Write" if tool in WRITES else "Edit"
        title = one_line(f"{verb} `{_name(path)}`") if path else verb + " a file"
        return Call(kind, tool, title, path, one_line(f"{verb.lower()} `{path}`" if path else f"{verb.lower()} a file"),
                    paths=(full,) if full else (), content_sha256=_digest(args))
    if tool in READS:
        path = _first(args, _PATH_KEYS)
        full = absolute(path, cwd) if path else None
        return Call("read", tool, one_line(f"Read `{_name(path)}`") if path else "Read a file", path,
                    one_line(f"read `{path}`" if path else "read a file"), paths=(full,) if full else ())
    if tool in LISTS:
        path = _first(args, _DIR_KEYS) or cwd
        return Call("list", tool, one_line(f"List `{_name(path)}`"), path, one_line(f"list `{path}`"),
                    paths=(absolute(path, cwd),))
    if tool in SEARCHES:
        query = _first(args, _QUERY_KEYS) or ""
        where = _first(args, _DIR_KEYS)
        return Call("search", tool, one_line(f"Search for `{query}`" if query else "Search"), query or where,
                    one_line(f"search for `{query}`" + (f" in `{where}`" if where else "")),
                    paths=(absolute(where, cwd),) if where else ())
    if tool in COMMANDS:
        command = args.get("CommandLine") if isinstance(args.get("CommandLine"), str) else \
            args.get("Input") if isinstance(args.get("Input"), str) else ""
        summary = args.get("toolSummary") if isinstance(args.get("toolSummary"), str) else None
        title = one_line(summary) if summary else one_line(f"Run `{command}`") if command else "Run a command"
        return Call("command", tool, title, command or None, one_line(f"run `{command}`") if command else "run a command",
                    command=command or None)
    if tool == "read_url_content":
        url = _first(args, ("Url", "URL", "url")) or ""
        return Call("web_fetch", tool, one_line(f"Fetch {url}") if url else "Fetch a page", url or None,
                    one_line(f"fetch {url}"), url=url or None)
    if tool == "search_web":
        query = _first(args, ("query", "Query")) or ""
        return Call("web_search", tool, one_line(f"Search the web for {query}"), query or None,
                    one_line(f"search the web for {query}"), url=query or None)
    if tool == "call_mcp_tool":
        server = _first(args, ("ServerName", "server")) or ""
        name = _first(args, ("ToolName", "tool")) or ""
        return Call("mcp", tool, one_line(f"`{name}` on `{server}`"), f"{server}/{name}",
                    one_line(f"call `{name}` on `{server}`"), mcp=(server, name),
                    content_sha256=hashlib.sha256(json.dumps(args.get("Arguments"), sort_keys=True,
                                                             default=str).encode()).hexdigest())
    if tool in SUBAGENTS:
        role = _first(args, ("Role", "TypeName", "Task")) or "a sub-agent"
        return Call("subagent", tool, one_line(f"Start {role}"), role, one_line(f"start sub-agent {role}"),
                    content_sha256=_digest({"Content": args.get("Prompt") or args.get("definition")}))
    if tool in QUESTIONS:
        return Call("ask", tool, "Ask a question", None, "ask the person a question")
    return Call("other", tool, one_line(tool.replace("_", " ").capitalize()), None, one_line(f"use agy's {tool}"),
                content_sha256=_digest({"Content": json.dumps(args, sort_keys=True, default=str)}))


# ── what the runtime answers itself ──────────────────────────────────────


def _real(path: str, cwd: str) -> str:
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def protected_paths() -> tuple[str, ...]:
    """XO's and git's, and every installed agent's project config as its
    manifest declares it (``project_config_paths``), agy's own included."""
    declared = {glob for manifest in all_agents() for glob in manifest.project_config_paths}
    return tuple(sorted({*CORE_PROTECTED, *PROJECT_CONFIG_PATHS, *declared}))


def protected(path: str, cwd: str) -> str | None:
    """The protected pattern ``path`` falls under, inside ``cwd``; None for
    an ordinary file. Matched on the path as given and as it resolves, so a
    link can't hide one. Blocking (resolves links)."""
    root = os.path.realpath(cwd)
    patterns = protected_paths()
    for form in (os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path)), _real(path, cwd)):
        for base in {os.path.normpath(cwd), root}:
            try:
                rel = os.path.relpath(form, base)
            except ValueError:
                continue
            if rel == os.curdir or rel.startswith(os.pardir):
                continue
            if os.path.basename(rel) == "config.worktree":
                return "config.worktree"
            for pattern in patterns:
                folder = pattern[:-3] if pattern.endswith("/**") else pattern
                if rel == folder or rel.startswith(folder + os.sep) or fnmatch.fnmatchcase(rel, pattern):
                    return pattern
    return None


def protected_any(paths: Iterable[str], cwd: str) -> str | None:
    """The first protected pattern among ``paths``, wherever they are: a
    write to another checkout's ``.agents/`` is as protected as one here."""
    for path in paths:
        found = protected(path, cwd)
        if found is None:
            parts = os.path.normpath(_real(path, cwd)).split(os.sep)
            for pattern in (*CORE_PROTECTED, *PROJECT_CONFIG_PATHS):
                if pattern.endswith("/**") and pattern[:-3] in parts:
                    found = pattern
                    break
        if found is not None:
            return found
    return None


def inside(path: str, cwd: str) -> bool:
    """Whether ``path`` resolves into ``cwd`` (links followed). Blocking."""
    root = os.path.realpath(cwd)
    real = _real(path, cwd)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def own_edit(paths: Iterable[str], cwd: str) -> bool:
    """A write or edit the runtime accepts itself in ``accept_edits``: every
    path inside the session's folder, none protected. Blocking."""
    paths = list(paths)
    return bool(paths) and all(inside(p, cwd) and protected(p, cwd) is None for p in paths)


def artifact_write(paths: Iterable[str], artifact_dir: str | None) -> bool:
    """A write into agy's own folder for this conversation (``brain/<id>/``),
    where it keeps plans and task lists. Blocking."""
    paths = list(paths)
    return bool(paths and artifact_dir) and all(inside(p, artifact_dir) for p in paths)
