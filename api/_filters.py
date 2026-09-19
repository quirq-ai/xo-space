"""Visibility and validation predicates for the BFF layer.

Lives separately from any route so the rules are auditable in one place
(see P4 in docs/bff-endpoints-design.md). Imports only the stdlib `re`;
specifically NEVER imports `os` or `pathlib` — that's the inner layer's
job (see P2).
"""

from __future__ import annotations

import re

# ── Env-var keys ──────────────────────────────────────────────────────────────

# POSIX shell convention: leading uppercase letter or underscore, then
# uppercase letters / digits / underscores.
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Characters that would break .env round-tripping if present in a value.
_FORBIDDEN_VALUE_CHARS = ("\n", "\r", "\x00")

# Runtime controls have their own typed, validated API. Keeping them out of the
# generic write-only secret store prevents an advanced variable from silently
# overriding the setup panel on the next restart.
HIDDEN_KEYS: frozenset[str] = frozenset({
    "AGENT_NAME",
    "QUIRQ_WATCHER_ENABLED",
    "QUIRQ_WATCHER_INTERVAL_SECONDS",
    "QUIRQ_WATCHER_SOURCE_MODE",
})


def is_valid_key(key: str) -> bool:
    return bool(_ENV_KEY_RE.match(key))


def is_valid_value(value: str) -> bool:
    return not any(ch in value for ch in _FORBIDDEN_VALUE_CHARS)


def is_hidden_key(key: str) -> bool:
    return key in HIDDEN_KEYS


def preview_value(value: str) -> str | None:
    """Return a fixed mask, or None if the value is empty.

    Neither the value, its length, nor prefix/suffix fragments cross the wire.
    """
    if not value:
        return None
    return "••••••"


# ── Project entries ───────────────────────────────────────────────────────────

# Defensive filter for the /api/xo-projects listing: directories under
# the projects root whose names clash with conventional system leaves
# are excluded even if a user creates them by accident.
PROJECT_SYSTEM_LEAVES: frozenset[str] = frozenset({
    "agents", "memory", "state", "projects",
})


# ── Project tree (interior listing) ───────────────────────────────────────────

# Canonical agent meta-files. Hidden ONLY at the project root, never in
# subdirectories — a `src/AGENTS.md` would be legitimate.
PROJECT_ROOT_AGENT_FILES: frozenset[str] = frozenset({
    # "WORKSPACE.md", "AGENTS.md", "OBJECTIVES.md", "CLAUDE.md",
})

# Editor/IDE/OS detritus that shouldn't appear in any project tree level.
_TEMP_SUFFIXES = (".tmp", ".swp", ".swo", ".bak", ".orig")
_TEMP_PREFIXES = ("~$",)


def is_hidden_name(name: str) -> bool:
    """True iff a filesystem entry should be hidden from the UI at every
    tree level. Dotfiles (`.xo`, `.git`, `.env`, …) and editor temp/backup
    files match."""
    if not name:
        return True
    if name.startswith("."):
        return True
    if any(name.endswith(suffix) for suffix in _TEMP_SUFFIXES):
        return True
    if any(name.startswith(prefix) for prefix in _TEMP_PREFIXES):
        return True
    return False


def is_root_only_hidden(name: str) -> bool:
    """True iff the entry is hidden only when listed at the project root
    (e.g. canonical agent files). Returns False everywhere else."""
    return name in PROJECT_ROOT_AGENT_FILES


# ── Commit relay ──────────────────────────────────────────────────────────────

_WORKSPACE_ID_RE = re.compile(r"^\S{1,100}$")


def is_valid_workspace_id(value: str) -> bool:
    """A recipient workspace id: non-blank after strip, no whitespace, ≤100 chars
    (the swarm column width). The relay never interprets it further."""
    return isinstance(value, str) and bool(_WORKSPACE_ID_RE.match(value.strip()))
