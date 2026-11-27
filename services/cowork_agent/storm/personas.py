"""Agents a person defines (the UI says "Agents"; the code says persona).

An agent is a file in Claude Code's agent shape, at one of two levels:

* ``project``: ``<project>/.xo/agents/<name>.md``, for that project;
* ``space``: ``~/.quirq/agents/<name>.md``, for every project.

A project's agent replaces the Space's agent with the same name.

The file is YAML front matter and a body, the instructions. The keys are
Claude Code's (``name``, ``description``, ``tools``, ``disallowedTools``,
``model``, ``effort``, ``mcpServers``) plus XO's ``runtime``. Tool names are
XO's neutral words (``adapters.contract.ToolRef``). Any other key is reported
with its line, so a typo never silently changes a run.

Both levels are read, saved and versioned by the project-files code: a
project's agents at ``.xo/agents/<name>.md`` under the project's id, the
Space's at ``<name>.md`` under ``SPACE_ID`` (``rel_path`` says which). This
module only reads them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import Any, get_args

import yaml

from services.cowork_agent.adapters.contract import Effort
from services.cowork_agent.registry.agent_registry import all_agents
from services.cowork_agent.storm.presets.coordinators import READ_SET
from services.project_files.address import (
    AGENTS_REL,
    EDIT_MAX_BYTES,
    SPACE_ID,
    FileRefused,
    locate,
    open_file,
    parent_dir,
    project_root,
    read_fd,
    version_of,
)
from services.storage.layout import agents_dir

__all__ = ["SPACE_ID", "PROJECT", "SPACE", "MAY", "Problem", "Persona", "parse", "listing", "resolve", "rel_path"]

PROJECT, SPACE = "project", "space"
#: A name is also its file's name: lowercase letters, digits and hyphens.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SUFFIX = ".md"

#: The form's three "May" choices, each the neutral tools it allows. Reading
#: is the coordinators' read set, so a read-only agent reads as they do.
MAY: dict[str, dict[str, Any]] = {
    "read": {"label": "Read the project", "tools": list(READ_SET)},
    "edit": {"label": "Change files", "tools": ["edit", "create", "delete"]},
    "run": {"label": "Run commands", "tools": ["shell"]},
}

#: The neutral tool words (``adapters.contract.ToolRef``); ``shell:``,
#: ``mcp:`` and ``native:`` take what follows them.
_TOOLS = frozenset({"read", "search", "list", "edit", "create", "delete", "shell", "web_fetch", "web_search",
                    "subagents", "ask_user", "plan", "worktree", "schedule"})
_TOOL_PREFIXES = ("shell:", "mcp:", "native:")
#: Claude Code keys XO does not carry, and why.
_NOT_CARRIED = {
    "permissionMode": "XO sets how each run asks for permission; remove permissionMode.",
    "hooks": "XO's own guard runs the hooks; remove hooks.",
}
_KEYS = ("name", "description", "tools", "disallowedTools", "model", "effort", "mcpServers", "runtime")


@dataclass(frozen=True)
class Problem:
    """A mistake in an agent file: its line (1-based, None for the whole file), its key, and what to fix."""

    line: int | None
    key: str | None
    message: str


@dataclass(frozen=True)
class Persona:
    """One agent file. ``level``, ``path`` (its address under the project's
    id or ``SPACE_ID``), ``shadows`` and ``shadowed`` (``{name, level}``) are
    set when it is read from a folder; ``version`` is the file's
    project-files version. ``problems`` empty means it can run."""

    name: str
    description: str = ""
    level: str = ""
    path: str = ""
    version: str = ""
    runtime: str | None = None
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] | None = None
    body: str = ""
    problems: tuple[Problem, ...] = ()
    shadows: dict[str, str] | None = None
    shadowed: dict[str, str] | None = None


# ── reading one file ──────────────────────────────────────────────────────────


def parse(text: str) -> tuple[Persona | None, list[Problem]]:
    """An agent from a file's text, and its mistakes. None when the text has
    no readable front matter; otherwise the agent, with what is wrong in it."""
    text = text.removeprefix("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, [Problem(1, None, "An agent file starts with front matter: a line '---', its settings, "
                                       "then another '---'.")]
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, [Problem(1, None, "The front matter has no closing '---' line.")]
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return None, [Problem(mark.line + 2 if mark else None, None, f"The front matter isn't valid YAML: "
                                                                      f"{getattr(exc, 'problem', None) or exc}.")]
    if not isinstance(meta, dict):
        return None, [Problem(2, None, "The front matter must be settings, one 'key: value' per line.")]

    at = _key_lines(lines[1:end])
    problems: list[Problem] = []

    def bad(key: str, message: str) -> None:
        problems.append(Problem(at.get(key), key, message))

    for key in meta:
        if key in _NOT_CARRIED:
            bad(key, _NOT_CARRIED[key])
        elif key not in _KEYS:
            bad(str(key), f"'{key}' isn't an agent setting. The settings are: {', '.join(_KEYS)}.")

    name = _text(meta, "name", bad, required=True)
    if name and not NAME_RE.fullmatch(name):
        bad("name", "A name is lowercase letters, digits and hyphens (at most 64), starting with a letter or "
                    "digit.")
    description = _text(meta, "description", bad, required=True)
    effort = _text(meta, "effort", bad)
    if effort and effort not in get_args(Effort):
        bad("effort", f"effort is one of {', '.join(get_args(Effort))}.")
    runtime = _text(meta, "runtime", bad)
    if runtime and runtime not in {m.name for m in all_agents()}:
        bad("runtime", f"No runtime called '{runtime}' is installed.")
    mcp = _names(meta, "mcpServers", bad, check=None)
    persona = Persona(
        name=name or "",
        description=description or "",
        version=version_of(text.encode("utf-8")),
        runtime=runtime,
        model=_text(meta, "model", bad),
        effort=effort,
        tools=_names(meta, "tools", bad, check=_is_tool),
        disallowed_tools=_names(meta, "disallowedTools", bad, check=_is_tool) or (),
        mcp_servers=mcp,
        body="\n".join(lines[end + 1:]).strip("\n"),
        problems=tuple(problems),
    )
    return persona, problems


def _key_lines(front: list[str]) -> dict[str, int]:
    """Each top-level key's line in the file (the front matter starts on line 2)."""
    found: dict[str, int] = {}
    for i, line in enumerate(front):
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if match:
            found.setdefault(match.group(1), i + 2)
    return found


def _text(meta: dict, key: str, bad, *, required: bool = False) -> str | None:
    value = meta.get(key)
    if value is None or value == "":
        if required:
            bad(key, f"'{key}' is required.")
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        bad(key, f"'{key}' is one line of text.")
        return None
    return str(value).strip()


def _names(meta: dict, key: str, bad, *, check) -> tuple[str, ...] | None:
    """A list, written as YAML or as one comma-separated line; None when absent."""
    value = meta.get(key)
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
        bad(key, f"'{key}' is a list of names, or one line of names separated by commas.")
        return None
    names = tuple(v.strip() for v in items if v.strip())
    unknown = [n for n in names if check is not None and not check(n)]
    if unknown:
        bad(key, f"Not XO tool names: {', '.join(unknown)}. Tools are {', '.join(sorted(_TOOLS))}, "
                 "or shell:<command>, mcp:<server>/<tool>.")
    return names


def _is_tool(name: str) -> bool:
    return name in _TOOLS or any(name.startswith(p) and len(name) > len(p) for p in _TOOL_PREFIXES)


# ── the two folders ───────────────────────────────────────────────────────────


def rel_path(level: str, name: str) -> str:
    """An agent's path for the project-files code: under the project's id for
    ``project``, under ``SPACE_ID`` for ``space``."""
    if level not in (PROJECT, SPACE):
        raise ValueError(f"unknown agent level {level!r}")
    return f"{AGENTS_REL}/{name}{_SUFFIX}" if level == PROJECT else f"{name}{_SUFFIX}"


def _folder_id(project_id: str | None, level: str) -> str | None:
    return SPACE_ID if level == SPACE else project_id


def _names_in(project_id: str | None, level: str) -> list[str]:
    """The agent names in one level's folder (``<name>.md`` files), sorted."""
    if level == SPACE:
        folder = agents_dir()
    else:
        if project_id is None:
            return []
        _, root = project_root(project_id)
        folder = root / AGENTS_REL
    try:
        entries = os.listdir(folder)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(e[:-len(_SUFFIX)] for e in entries if e.endswith(_SUFFIX) and not e.startswith("."))


def _read(project_id: str | None, level: str, name: str) -> Persona | None:
    """One level's agent file, never through a link nor out of its folder;
    None when there is no such file."""
    fid = _folder_id(project_id, level)
    if fid is None or not NAME_RE.fullmatch(name):
        return None
    rel = rel_path(level, name)
    try:
        target = locate(fid, rel)
        with parent_dir(target) as folder:
            fd = open_file(folder, target.name)
            if fd is None:
                return None
            try:
                data = read_fd(fd, EDIT_MAX_BYTES + 1)
            finally:
                os.close(fd)
    except FileRefused as exc:
        if exc.code in ("folder_not_found", "project_not_found"):
            return None
        return Persona(name=name, level=level, path=rel, problems=(Problem(None, None, exc.message),))
    version = version_of(data)
    if len(data) > EDIT_MAX_BYTES:
        found, problems = None, [Problem(None, None, "The file is too big for an agent (over 1 MB).")]
    else:
        try:
            found, problems = parse(data.decode("utf-8"))
        except UnicodeDecodeError:
            found, problems = None, [Problem(None, None, "The file isn't UTF-8 text.")]
    if found is not None and found.name and found.name != name:
        problems = [*problems, Problem(2, "name", f"The name '{found.name}' differs from the file's name "
                                                  f"'{name}{_SUFFIX}'; make them the same.")]
    base = found or Persona(name=name)
    return replace(base, name=name, level=level, path=rel, version=version, problems=tuple(problems))


def listing(project_id: str | None) -> list[Persona]:
    """Every agent a project sees, its own first, then the Space's, each
    sorted by name; a project agent ``shadows`` the Space agent it replaces,
    which is ``shadowed``. Without a project, the Space's agents.
    ``FileRefused`` (404) for a missing project."""
    own = [p for n in _names_in(project_id, PROJECT) if (p := _read(project_id, PROJECT, n))]
    space = [p for n in _names_in(None, SPACE) if (p := _read(None, SPACE, n))]
    mine = {p.name for p in own}
    theirs = {p.name for p in space}
    return ([replace(p, shadows={"name": p.name, "level": SPACE}) if p.name in theirs else p for p in own]
            + [replace(p, shadowed={"name": p.name, "level": PROJECT}) if p.name in mine else p for p in space])


def resolve(project_id: str | None, name: str, level: str | None = None) -> Persona | None:
    """The agent a run uses: at ``level``, or with None the one that wins
    (the project's over the Space's). None when there is no such file, or
    the project is gone. A found agent may have ``problems``; the caller
    refuses to run it then."""
    try:
        for each in ((level,) if level else (PROJECT, SPACE)):
            found = _read(project_id, each, name)
            if found is not None:
                return found
    except FileRefused:
        return None
    return None
