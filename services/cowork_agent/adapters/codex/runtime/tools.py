"""codex's tool calls in the contract's neutral terms (storm ch 03 §2, §8.1).

codex reports each call as a thread item: a ``commandExecution`` (every
shell command, reads included), a ``fileChange`` (one ``apply_patch``, which
may touch several files), an ``mcpToolCall``, a ``webSearch``, a
``collabAgentToolCall`` (its sub-agent tools), and a few more. Each becomes a
``ToolKind`` with a title, a target and the neutral fields core's policy
reads (``PermissionRequest.paths/command/url/mcp``), so core never reads
codex's own item keys.

It also decides the one thing codex's ``accept_edits`` can't: whether a file
change is a thread's own edit in its own folder (``own_edit``). codex runs
with no sandbox (§0.67), so it asks about every change; the runtime accepts
these itself, as claude's ``acceptEdits`` does, and sends the rest to the
host.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from services.cowork_agent.adapters.contract import ToolKind
from services.cowork_agent.registry.agent_registry import all_agents

SUMMARY_MAX = 200
#: The files in a checkout that make codex run code on its own (its project
#: config: settings, hooks, exec policies); the integrity check watches them
#: (storm ch 16 §2.1) and the runtime never accepts an edit to them itself.
PROJECT_CONFIG_PATHS = (".codex/**",)
#: Paths in a session's folder no edit is accepted for without the host: XO's
#: own state and git's (a hook there runs code); every agent's project config
#: is added from the agent manifests (``protected_paths``), as core's policy
#: does: a codex thread that writes claude's hooks would arm claude.
CORE_PROTECTED = (".xo/**", ".git/**")
#: The shells codex wraps a command in (``/bin/bash -lc '<command>'``).
SHELLS = frozenset({"bash", "sh", "zsh", "dash"})


@dataclass(frozen=True)
class Call:
    """One tool call in neutral terms."""

    kind: ToolKind
    tool: str                 # codex's own name for it, for display
    title: str
    target: str | None
    summary: str
    paths: tuple[str, ...] = ()
    command: str | None = None
    url: str | None = None
    mcp: tuple[str, str] | None = None
    content_sha256: str | None = None


def one_line(text: str, limit: int = SUMMARY_MAX) -> str:
    """One display line: the first line, cut to ``limit`` characters."""
    first = " ".join(text.split("\n", 1)[0].split())
    return first if len(first) <= limit else first[: limit - 1] + "…"


def unwrap(command: object) -> str:
    """The command as the agent wrote it: codex wraps each in a login shell
    (``/bin/bash -lc 'ls -a'``) and reports it as a string, or (in its
    transcript) as an argv list."""
    if isinstance(command, list):
        argv = [str(a) for a in command]
    elif isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError:
            return command
    else:
        return ""
    if len(argv) >= 3 and os.path.basename(argv[0]) in SHELLS and argv[1] in ("-lc", "-c", "-l"):
        return argv[2]
    return " ".join(shlex.quote(a) for a in argv) if isinstance(command, list) else str(command)


def _name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def describe_command(item: dict[str, Any]) -> Call:
    command = unwrap(item.get("command"))
    return Call("command", "shell", one_line(command or "a command"), command or None,
                one_line(f"run `{command}`"), command=command or None)


def change_paths(changes: object) -> tuple[str, ...]:
    out: list[str] = []
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, dict):
            continue
        for key in ("path",):
            if isinstance(change.get(key), str) and change[key]:
                out.append(change[key])
        kind = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        if isinstance(kind.get("move_path"), str) and kind["move_path"]:
            out.append(kind["move_path"])
    return tuple(dict.fromkeys(out))


def _change_types(changes: object) -> set[str]:
    found = set()
    for change in changes if isinstance(changes, list) else []:
        kind = change.get("kind") if isinstance(change, dict) and isinstance(change.get("kind"), dict) else {}
        if isinstance(kind.get("type"), str):
            found.add(kind["type"])
        if kind.get("move_path"):
            found.add("move")
    return found


def describe_change(item: dict[str, Any]) -> Call:
    """One ``apply_patch``: one call, whichever files it touches. Its kind
    is the strongest change in it (a delete, else an edit, else a create)."""
    changes = item.get("changes")
    paths = change_paths(changes)
    types = _change_types(changes)
    kind: ToolKind = "delete_file" if "delete" in types else "edit_file" if types - {"add"} else "create_file"
    verb = {"delete_file": "Delete", "edit_file": "Edit", "create_file": "Create"}[kind]
    if len(paths) == 1:
        title = f"{verb} `{_name(paths[0])}`"
    else:
        title = f"{verb} {len(paths)} files" if paths else f"{verb} files"
    summary = f"{verb.lower()} " + ", ".join(f"`{p}`" for p in paths) if paths else f"{verb.lower()} files"
    clean = [{k: c.get(k) for k in ("path", "kind", "diff")} for c in changes if isinstance(c, dict)] \
        if isinstance(changes, list) else []
    return Call(kind, "apply_patch", one_line(title), paths[0] if paths else None, one_line(summary),
                paths=paths, content_sha256=_digest(clean))


def describe_mcp(item: dict[str, Any]) -> Call:
    server = str(item.get("server") or "")
    tool = str(item.get("tool") or "")
    return Call("mcp", f"{server}/{tool}", one_line(f"`{tool}` on `{server}`"), f"{server}/{tool}",
                one_line(f"call `{tool}` on `{server}`"), mcp=(server, tool),
                content_sha256=_digest(item.get("arguments")))


def describe_web_search(item: dict[str, Any]) -> Call:
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    query = item.get("query") if isinstance(item.get("query"), str) else action.get("query")
    url = action.get("url") if isinstance(action.get("url"), str) else None
    if url and not query:
        return Call("web_fetch", "web_search", one_line(f"Read `{url}`"), url, one_line(f"fetch `{url}`"), url=url)
    query = query if isinstance(query, str) else ""
    return Call("web_search", "web_search", one_line(f"Search the web for {query}" if query else "Search the web"),
                query or None, one_line(f"search the web for `{query}`"), url=query or None)


def describe_collab(item: dict[str, Any]) -> Call:
    tool = str(item.get("tool") or "agent")
    prompt = item.get("prompt") if isinstance(item.get("prompt"), str) else ""
    if tool == "spawnAgent":
        title = one_line(prompt or "Start a sub-agent", 80)
        return Call("subagent", "spawn_agent", title, None, one_line(f"start a sub-agent: {prompt or 'a task'}"))
    titles = {"wait": "Wait for sub-agents", "sendInput": "Message a sub-agent", "sendMessage": "Message a sub-agent",
              "followupTask": "Give a sub-agent more work", "interruptAgent": "Stop a sub-agent",
              "closeAgent": "Close a sub-agent", "resumeAgent": "Resume a sub-agent", "listAgents": "List sub-agents"}
    title = titles.get(tool, f"Sub-agent tool {tool}")
    return Call("subagent", tool, title, None, one_line(title.lower()))


def describe_item(item: dict[str, Any]) -> Call | None:
    """The call a thread item is, or None for an item that isn't one
    (messages, reasoning, plans, sub-agent activity)."""
    kind = item.get("type")
    if kind == "commandExecution":
        return describe_command(item)
    if kind == "fileChange":
        return describe_change(item)
    if kind == "mcpToolCall":
        return describe_mcp(item)
    if kind == "webSearch":
        return describe_web_search(item)
    if kind == "collabAgentToolCall":
        return describe_collab(item)
    if kind == "dynamicToolCall":
        tool = str(item.get("tool") or "a tool")
        return Call("other", tool, one_line(f"Use {tool}"), None, one_line(f"use {tool}"),
                    content_sha256=_digest(item.get("arguments")))
    if kind == "imageView":
        path = item.get("path") if isinstance(item.get("path"), str) else ""
        return Call("read", "view_image", one_line(f"View `{_name(path)}`"), path or None, one_line(f"view `{path}`"),
                    paths=(path,) if path else ())
    if kind == "imageGeneration":
        return Call("other", "image_generation", "Generate an image", None, "generate an image")
    return None


# ── a thread's own edits ─────────────────────────────────────────────────


def _real(path: str, cwd: str) -> str:
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def protected_paths() -> tuple[str, ...]:
    """XO's and git's, and every installed agent's project config as its
    manifest declares it (``project_config_paths``), codex's own included."""
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


def inside(path: str, cwd: str) -> bool:
    """Whether ``path`` resolves into ``cwd`` (links followed). Blocking."""
    root = os.path.realpath(cwd)
    real = _real(path, cwd)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def own_edit(paths: Iterable[str], cwd: str) -> bool:
    """A file change the runtime accepts itself in ``accept_edits``: every
    path in it inside the session's folder, none protected. Blocking."""
    paths = list(paths)
    return bool(paths) and all(inside(p, cwd) and protected(p, cwd) is None for p in paths)


#: codex's own parse of a command's parts that only read (``commandActions``).
READ_ACTIONS = frozenset({"read", "listFiles", "search"})


def skill_roots(codex_home: Path) -> tuple[Path, ...]:
    """Where codex's skills live: its own ``skills/`` and the global skills
    folder its entries may link to."""
    return (codex_home / "skills", Path.home() / ".agents" / "skills")


def skill_read(actions: object, cwd: str, codex_home: Path) -> bool:
    """A command that only reads codex's own skills (``cat …/SKILL.md``):
    codex reads a skill with its shell, where claude has a Skill tool that
    asks nobody. Every part must be a read, listing or search (codex's own
    parse) of a path inside a skill folder, links resolved. Blocking."""
    if not isinstance(actions, list) or not actions:
        return False
    roots = [os.path.realpath(r) for r in skill_roots(codex_home)]
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in READ_ACTIONS:
            return False
        path = action.get("path")
        if not isinstance(path, str) or not path:
            return False
        real = _real(os.path.expanduser(path), cwd)
        if not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
            return False
    return True


def diff_stat(changes: object) -> tuple[int, int] | None:
    """Lines added and removed across a file change's diffs; an added file's
    ``diff`` is its whole content."""
    added = removed = 0
    seen = False
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, dict) or not isinstance(change.get("diff"), str):
            continue
        seen = True
        kind = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        lines = change["diff"].splitlines()
        if kind.get("type") == "add":
            added += len(lines)
            continue
        if kind.get("type") == "delete":
            removed += len(lines)
            continue
        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return (added, removed) if seen else None
