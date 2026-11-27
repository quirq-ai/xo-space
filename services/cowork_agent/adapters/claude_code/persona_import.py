"""claude_code's ``persona_import`` capability (plan B7, D10): the agents
the person already wrote for Claude Code, offered for a one-time copy into
XO's agent files.

Claude Code keeps them as Markdown with front matter, in ``agents/`` under
its config folder (``~/.claude``, or ``CLAUDE_CONFIG_DIR``) for every
project, and in ``<project>/.claude/agents/`` for one. XO's agent file is
the same shape (``name``, ``description``, ``tools``, ``disallowedTools``,
``model``, ``effort``, ``mcpServers``, the body as the instructions), with
one key of its own, ``runtime``, which an import sets to this runtime: the
file was written for it, and its model names are this runtime's.

Each file is mapped on its own. Tool names become XO's neutral words
(``runtime/tools.py``'s table, read backwards). What XO can't carry is
listed in ``dropped``, never silently lost: a key XO has no place for
(``permissionMode``, ``hooks``, ``skills``, ``color``…), a tool with no
neutral word, an MCP server given inline rather than by name. The person
then imports the file without them, or skips it.

The contract core reads (``routers/cowork_agent/storm/personas.py``):
``DISPLAY_NAME`` and ``candidates(project_folder) -> list[dict]``, each
``{id, name, level, source, text, dropped}``; ``level`` is ``space`` for the
config folder's files and ``project`` for the project's own.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from services.cowork_agent.adapters.claude_code.adapter import config_dir
from services.cowork_agent.adapters.claude_code.runtime.tools import REF_TOOLS

log = logging.getLogger(__name__)

DISPLAY_NAME = "Claude Code"
#: This runtime, as XO's ``runtime`` key names it.
RUNTIME = "claude_code"
#: The keys XO's file carries over as they are.
KEPT = ("name", "description", "model", "effort")
#: A file larger than this isn't an agent definition; it is skipped.
MAX_BYTES = 256 * 1024

_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)
_SHELL = re.compile(r"\ABash\((.+?)(?::\*)?\)\Z")
#: Each Claude tool's neutral word.
_NEUTRAL = {tool: word for word, tools in REF_TOOLS.items() for tool in tools}


def _home_agents() -> Path:
    return (config_dir() or Path.home() / ".claude") / "agents"


def candidates(project_folder: Path | None) -> list[dict[str, Any]]:
    """Every agent file in the config folder and, given a project, its own; one unreadable is skipped (logged)."""
    found = []
    places = [("space", _home_agents())]
    if project_folder is not None:
        places.append(("project", project_folder / ".claude" / "agents"))
    for level, folder in places:
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else ():
            try:
                if not path.is_file() or path.stat().st_size > MAX_BYTES:
                    continue
                item = convert(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                log.info("persona import: %s skipped: %s", path, exc)
                continue
            found.append({"id": f"{level}:{path.stem}", "level": level, "source": str(path), **item})
    return found


def _tool(name: str) -> str | None:
    """One Claude tool as XO's neutral word; None when it has none."""
    name = name.strip()
    if name in _NEUTRAL:
        return _NEUTRAL[name]
    if (shell := _SHELL.match(name)) is not None:
        return f"shell:{shell.group(1)}"
    if name.startswith("mcp__"):
        server, _, tool = name[len("mcp__"):].partition("__")
        return f"mcp:{server}/{tool or '*'}" if server else None
    return None


def _tools(value: Any, key: str, dropped: list[str]) -> list[str]:
    names = value.split(",") if isinstance(value, str) else list(value or ())
    words: list[str] = []
    for name in (str(n).strip() for n in names):
        word = _tool(name) if name else None
        if word is None:
            if name:
                dropped.append(f"{key}: {name}")
        elif word not in words:
            words.append(word)
    return words


def convert(text: str) -> dict[str, Any]:
    """``{name, text, dropped}``: the file in XO's shape, and what it leaves
    out. ``ValueError`` when it isn't an agent file (no front matter, no name)."""
    match = _FRONT.match(text.lstrip("﻿"))
    if match is None:
        raise ValueError("no front matter")
    front = yaml.safe_load(match.group(1)) or {}
    if not isinstance(front, dict) or not isinstance(front.get("name"), str) or not front["name"].strip():
        raise ValueError("no name in its front matter")
    out: dict[str, Any] = {k: front[k] for k in KEPT if front.get(k) not in (None, "")}
    if out.get("model") == "inherit":           # Claude Code's word for "the session's own": XO's default
        del out["model"]
    out["runtime"] = RUNTIME
    dropped: list[str] = []
    for key in ("tools", "disallowedTools"):
        if key in front:
            out[key] = _tools(front[key], key, dropped)
    if "mcpServers" in front:
        servers = front["mcpServers"] if isinstance(front["mcpServers"], list) else [front["mcpServers"]]
        named = [s for s in servers if isinstance(s, str)]
        dropped += [f"mcpServers: {next(iter(s), 'inline')} (inline)" if isinstance(s, dict) else "mcpServers"
                    for s in servers if not isinstance(s, str)]
        if named:
            out["mcpServers"] = named
    dropped += [key for key in front if key not in (*KEPT, "tools", "disallowedTools", "mcpServers")]
    head = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in out.items())
    return {"name": out["name"].strip(), "text": f"---\n{head}\n---\n{match.group(2)}", "dropped": dropped}
