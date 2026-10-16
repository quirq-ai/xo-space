"""The one wording table (design ch 09 §2): every activity row and group
header is built here from the tool ``kind`` the runtime supplies (ch 01
§5.3), the same for every CLI. Nothing else in storm writes display copy for
a tool call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Wording:
    running: str            # the row while it runs; {target} is filled in
    done: str               # the row once it finished ok
    verb: str | None        # the header's verb ("created"); None: not counted (ch 09 §2's "—")
    one: str = ""           # the count phrase for one ("a file")
    many: str = ""          # for several ("{n} files")


_FILES = ("a file", "{n} files")
WORDING: dict[str, Wording] = {
    "create_file": Wording("Writing `{target}`", "Created `{target}`", "created", *_FILES),
    "edit_file": Wording("Editing `{target}`", "Edited `{target}`", "edited", *_FILES),
    "delete_file": Wording("Deleting `{target}`", "Deleted `{target}`", "deleted", *_FILES),
    "read": Wording("Reading `{target}`", "Read `{target}`", "read", *_FILES),
    "search": Wording("Searching for `{target}`", "Searched for `{target}`", "searched", "once", "{n} times"),
    "list": Wording("Searching for `{target}`", "Searched for `{target}`", "searched", "once", "{n} times"),
    "command": Wording("{title}", "{title}", "ran", "a command", "{n} commands"),
    "web_fetch": Wording("Reading `{target}`", "Read `{target}`", "looked up", "a page", "{n} pages"),
    "web_search": Wording("Searching the web for {target}", "Searched the web for {target}", "looked up",
                          "a page", "{n} pages"),
    "mcp": Wording("{target}", "{target}", "used", "a tool", "{n} tools"),
    "subagent": Wording("Sub-agent: `{title}`", "Sub-agent: `{title}`", "started", "a sub-agent", "{n} sub-agents"),
    "ask": Wording("Asking you: {title}", "Asked you: {title}", None),
    "other": Wording("{tool}", "{tool}", "did", "another step", "{n} other steps"),
}
#: The kind whose calls are shown as the turn's Plan block, never as rows.
PLAN_KIND = "plan"
#: A call whose input streams before the runtime says what it is (claude's
#: file writes, ch 09 §2.1 note in ``wire.py``): only its size is known.
PENDING_ROW = "Writing…"
#: How a finished row that didn't succeed is counted in its group's header.
NOT_DONE = ("failed", "denied", "cancelled")
_STATUS_WORD = {"error": "failed", "denied": "denied", "cancelled": "cancelled"}


def row_line(kind: str, *, tool: str, title: str, target: str | None, status: str,
             size: int | None = None) -> str:
    """One activity row. A running file write carries its size as it grows
    ("Writing `app.py` · 3.4 KB"); a row that didn't succeed keeps the
    runtime's own plain-language title, and its status says how it ended."""
    wording = WORDING.get(kind, WORDING["other"])
    if status in _STATUS_WORD:
        return title or tool
    form = wording.done if status == "ok" else wording.running
    line = form.format(target=_shown(kind, target) or title or tool, title=title or tool, tool=tool or title)
    if status != "ok" and size is not None and kind in ("create_file", "edit_file"):
        line = f"{line} · {human_size(size)}"
    return line


def pending_line(size: int) -> str:
    return f"{PENDING_ROW} · {human_size(size)}"


def group_header(rows: Iterable[tuple[str, str]], *, running_line: str | None = None) -> str:
    """The activity group's header from its rows' ``(kind, status)``: counts
    by verb, in the order the verbs first appear ("Created 2 files, ran a
    command"), then what didn't succeed ("1 step denied"). While nothing has
    finished yet it is the running row's own line."""
    counts: dict[str, int] = {}
    phrases: dict[str, Wording] = {}
    misses: dict[str, int] = {}
    for kind, status in rows:
        if status == "ok":
            wording = WORDING.get(kind, WORDING["other"])
            if wording.verb is not None:
                key = f"{wording.verb}|{wording.one}"
                counts[key] = counts.get(key, 0) + 1
                phrases[key] = wording
        elif status in _STATUS_WORD:
            word = _STATUS_WORD[status]
            misses[word] = misses.get(word, 0) + 1
    parts = [f"{phrases[k].verb} {phrases[k].one if n == 1 else phrases[k].many.format(n=n)}"
             for k, n in counts.items()]
    parts += [f"{n} {'step' if n == 1 else 'steps'} {word}" for word in NOT_DONE if (n := misses.get(word))]
    if not parts:
        return running_line or ""
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def thinking_header(*, step_title: str | None, duration_ms: int | None, done: bool) -> str:
    """"Thinking…" (or the latest step's title) while it streams; "Thought
    for 32s" once done. A runtime that streams no thinking text keeps
    "Thinking…" until the steps land (ch 09 §2.1)."""
    if not done:
        return step_title or "Thinking…"
    if duration_ms is None:
        return "Thought"
    return f"Thought for {max(1, round(duration_ms / 1000))}s"


def plan_header(items: Iterable[tuple[str, str]]) -> str:
    states = [state for _, state in items]
    return f"Plan · {sum(1 for s in states if s == 'done')} of {len(states)} done"


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _shown(kind: str, target: str | None) -> str | None:
    """What a row names: a file's name, a page's host, an MCP tool on its
    server; the pattern or query as given."""
    if not target:
        return None
    if kind in ("create_file", "edit_file", "delete_file", "read"):
        return os.path.basename(target.rstrip("/")) or target
    if kind == "web_fetch":
        return urlsplit(target).hostname or target
    if kind == "mcp":
        server, _, tool = target.partition("/")
        return f"`{tool}` on `{server}`" if tool else f"`{server}`"
    return target
