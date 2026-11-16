"""The modularity invariant (DEVELOPING.md §6): core code never names an agent.

**Agent names are discovered, never listed here:** every ``config/agents/<name>/``
with a manifest and every ``services/cowork_agent/adapters/<name>/`` package,
plus each manifest's CLI ``binary`` where it isn't the agent's name (the code
says ``claude`` and ``agy`` as often as their agents' names).

**Core** is every Python file git tracks or would track (new files included;
ignored ones such as ``venv/`` not), except:
- the agent-owned trees, ``adapters/<agent>/`` and ``config/agents/<agent>/``,
  where agent code belongs;
- ``tests/``, which tests each adapter against its own fake by design (the
  conformance suite, a fake CLI per protocol): an agent named there never
  makes the broker branch on it.
``scripts/`` and ``plugins/`` are core: they ship with the Space and serve
whichever agent runs.

**Naming** is an agent's name or CLI as a whole word (any case; ``_``, ``-``,
a space or a camelCase step as the word break, so ``codex_setup``,
``ClaudeCode`` and ``CODEX_HOME`` count, ``strategy`` doesn't) in a string
literal, an identifier, an attribute, a def or class, an argument or keyword,
or an import. Docstrings and comments are prose and don't count.

``ALLOWED`` is DEVELOPING.md §6's frozen allowlist, exactly; ``PREDATING``
holds the namings that were already there when the guard was committed.
Both are held exactly: an entry no longer found must be removed, and a
predating count may only fall (lower it when it does). Run: ``venv/bin/python -m pytest -q tests/test_modularity.py``.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import subprocess
import unittest
from pathlib import Path
from typing import Iterable

from tests.required_tools import clean_env, require_tools

REPO = Path(__file__).resolve().parent.parent
ADAPTERS = "services/cowork_agent/adapters"
CONFIGS = "config/agents"
#: Agent names that are also ordinary words in this code, which a whole-word
#: match can't tell from the agent: checked by hand instead, with the date.
AMBIGUOUS = {
    "cursor": "the DB-API's cursor and pagination cursors (2026-11-11: each of the 33 core files that say "
              "cursor means one of those, none the agent)",
}
#: DEVELOPING.md §6's four frozen exceptions, as (file, word, why); the
#: OAuth keys are one exception naming two agents.
ALLOWED = (
    ("services/cowork_agent/registry/agent_registry.py", "claude_code",
     "the safe-boot default: the server starts with no agent configured"),
    ("services/cowork_agent/providers_status_lib.py", "claude_code", "the /providers/status OAuth keys"),
    ("services/cowork_agent/providers_status_lib.py", "codex", "the /providers/status OAuth keys"),
    ("routers/cowork_agent/legacy/openclaw_usage.py", "openclaw", "the legacy /openclaw/usage URL alias"),
    ("routers/auth/codex_setup.py", "openclaw", "codex's legacy openclaw-gateway credential writes"),
)
#: Namings that predate this guard (2026-11-11), outside ``ALLOWED``: mostly
#: each agent's connect and setup flow, whose URLs and answers are frozen
#: (``test_connect_routes.py``, ``test_frozen_surfaces.py``), so moving them
#: into their adapters is a change of its own. (file, word) -> how many times.
#: Not exceptions: a count may only fall, and a file not listed may name none.
PREDATING: dict[tuple[str, str], int] = {
    # Each agent's connect flow, a core router (frozen URLs, mounted under every agent).
    ("routers/auth/claude_setup_token.py", "claude"): 22,
    ("routers/auth/claude_setup_token.py", "claude_code"): 5,
    ("routers/auth/codex_setup.py", "codex"): 105,
    ("routers/auth/codex_setup.py", "hermes"): 33,           # codex's login also writes hermes's auth
    # The app: mounting those routers, each CLI's path, Claude's auth debug.
    ("server.py", "claude"): 21,
    ("server.py", "claude_code"): 4,
    ("server.py", "codex"): 12,
    ("server.py", "openclaw"): 3,                           # mounting the §6 legacy alias
    # /providers/status: the Claude CLI's auth probe, beyond the OAuth keys §6 names.
    ("services/cowork_agent/providers_status_lib.py", "claude"): 16,
    # Reading Claude Code's native JSONL in core.
    ("services/cowork_agent/engine/messages.py", "claude"): 1,
    ("services/cowork_agent/helpers.py", "claude"): 1,
    # The legacy ~/claude-cowork folder (CLAUDE_COWORK_ROOT): the product's old name, not the agent.
    ("services/cowork_agent/registry/settings.py", "claude"): 3,
    ("routers/cowork_agent/chat.py", "claude"): 2,
    # A model family: model.startswith("claude").
    ("routers/cowork_agent/bff/_visualizer_presenter.py", "claude"): 1,
    # Plugin packaging: each CLI's plugin folder and instructions file.
    ("plugins/quirq/scripts/package_plugin.py", "claude"): 1,
    ("plugins/quirq/scripts/package_plugin.py", "codex"): 1,
}


def agents() -> tuple[set[str], set[str]]:
    """(agent names, CLI names that aren't an agent's name), from the tree."""
    names = {p.name for p in (REPO / CONFIGS).iterdir() if (p / "manifest.json").is_file()}
    names |= {p.name for p in (REPO / ADAPTERS).iterdir() if (p / "__init__.py").is_file()}
    clis = set()
    for manifest in (REPO / CONFIGS).glob("*/manifest.json"):
        binary = json.loads(manifest.read_text(encoding="utf-8")).get("binary")
        if binary and binary not in names:
            clis.add(binary)
    return names, clis


def word_pattern(words: Iterable[str]) -> tuple[re.Pattern, list[str]]:
    """One pattern for every word, longest first (``claude_code`` before
    ``claude``); group ``i + 1`` is ``words[i]``."""
    ordered = sorted(words, key=len, reverse=True)

    def spelled(word: str) -> str:
        return r"[_\- ]?".join("".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else re.escape(c) for c in part)
                               for part in word.split("_"))
    starts = r"(?:(?<![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z]))"
    return re.compile(starts + "(?:" + "|".join(f"({spelled(w)})" for w in ordered) + r")(?![a-z0-9])"), ordered


def core_files(git: str, owners: set[str]) -> list[str]:
    listed = subprocess.run([git, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"],
                            cwd=REPO, env=clean_env(), capture_output=True, text=True, timeout=60)
    if listed.returncode != 0:
        raise RuntimeError(f"git can't list the repository's files ({listed.stderr.strip()}); run this from a checkout")
    out = []
    for name in sorted(set(filter(None, listed.stdout.split("\0")))):
        parts = name.split("/")
        if parts[0] == "tests" or not (REPO / name).is_file():
            continue
        if name.startswith(f"{ADAPTERS}/") and len(parts) > 4 and parts[3] in owners:
            continue
        if name.startswith(f"{CONFIGS}/") and len(parts) > 3 and parts[2] in owners:
            continue
        out.append(name)
    return out


def namings(source: str, pattern: re.Pattern, words: list[str]) -> list[tuple[int, str, str]]:
    """Each (line, word, where) in ``source`` that names one of ``words``."""
    tree = ast.parse(source)
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}
    found = []
    for node in ast.walk(tree):
        texts: list[tuple[str, str]] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            texts.append(("string", node.value))
        elif isinstance(node, ast.Name):
            texts.append(("name", node.id))
        elif isinstance(node, ast.Attribute):
            texts.append(("attribute", node.attr))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(("definition", node.name))
        elif isinstance(node, ast.arg):
            texts.append(("argument", node.arg))
        elif isinstance(node, ast.keyword) and node.arg:
            texts.append(("keyword", node.arg))
        elif isinstance(node, ast.alias):
            texts.extend(("import", text) for text in (node.name, node.asname) if text)
        elif isinstance(node, ast.ImportFrom) and node.module:
            texts.append(("import", node.module))
        for where, text in texts:
            for word in sorted({words[m.lastindex - 1] for m in pattern.finditer(text)}):
                found.append((getattr(node, "lineno", 0), word, where))
    return found


@functools.cache
def scan(git: str) -> dict[tuple[str, str], list[tuple[int, str]]]:
    """Every naming in core: (file, word) -> [(line, where)]."""
    names, clis = agents()
    pattern, words = word_pattern((names | clis) - set(AMBIGUOUS))
    out: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for name in core_files(git, names):
        for line, word, where in namings((REPO / name).read_text(encoding="utf-8"), pattern, words):
            out.setdefault((name, word), []).append((line, where))
    return out


class ModularityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.found = scan(require_tools(self, "git")["git"])

    def test_core_names_no_agent(self) -> None:
        allowed = {(file, word) for file, word, _why in ALLOWED}
        new = {key: lines for key, lines in self.found.items()
               if key not in allowed and len(lines) > PREDATING.get(key, 0)}
        if new:
            self.fail("core code names an agent; reach it through the capability loader or the runtime contract's "
                      "features instead (DEVELOPING.md §6):\n" + "\n".join(
                          f"  {file}:{line}: {word} ({where})" for (file, word), lines in sorted(new.items())
                          for line, where in lines))

    def test_every_exception_and_predating_entry_still_applies(self) -> None:
        for file, word, why in ALLOWED:
            with self.subTest(file=file, word=word):
                self.assertIn((file, word), self.found,
                              f"{file} no longer names {word} ({why}): take it off ALLOWED and DEVELOPING.md §6")
        for (file, word), count in PREDATING.items():
            with self.subTest(file=file, word=word):
                now = len(self.found.get((file, word), ()))
                self.assertEqual(now, count, f"{file} names {word} {now} time(s), not {count}: set PREDATING to "
                                             "the new count (it may only fall), or drop the entry at 0")

    def test_the_guard_reads_code_as_meant(self) -> None:
        pattern, words = word_pattern({"claude_code", "claude", "codex", "openclaw", "agy"})
        source = '''
"""A module docstring may say codex."""
import openclaw_bridge                      # a comment may say codex
from x import ClaudeCodeAdapter as agy_adapter
def run(codex_home, **kw):
    """So may a function's."""
    strategy = conn.cursor()
    if kw.get("agent") == "claude":
        return OpenClawGateway(CODEX_HOME=1).recodexed
'''
        found = sorted((line, word) for line, word, _where in namings(source, pattern, words))
        self.assertEqual(found, [(3, "openclaw"), (4, "agy"), (4, "claude_code"), (5, "codex"), (8, "claude"),
                                 (9, "codex"), (9, "openclaw")])


if __name__ == "__main__":
    unittest.main()
