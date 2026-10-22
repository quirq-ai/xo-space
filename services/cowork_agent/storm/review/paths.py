"""Where a path lands, for the policy engine (design ch 15 §2.2, §4; ch 16 §2.1).

Protected paths are matched relative to the worktree, never by an absolute
segment: ``.xo/**`` and ``.git/**`` are core's; each agent's project config
(``project_config_paths`` in its feature list and its manifest) is added by
the caller, so this module names no CLI. The CLIs' homes come from their manifests' ``home_dir``.

A path is judged at both its written form and its resolved form (symlinks
followed), and the worse of the two wins: a link in the worktree that points
at ``~/.ssh`` is a secret, not a routine read.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from services.cowork_agent.registry.agent_registry import all_agents


class Place(Enum):
    """Ordered from harmless to worst."""

    INSIDE = 0          # the session's own folder (a thread's worktree, a plain session's cwd)
    OUTSIDE = 1         # anywhere else, /tmp included
    HOME_CONFIG = 2     # a dotfile or dot folder in the person's home: shells and tools run what it says
    OTHER_WORKTREE = 3  # another thread's checkout
    CLI_HOME = 4        # an agent CLI's own folder (its config, logins, transcripts)
    STATE_ROOT = 5      # XO's own state
    SECRET = 6          # credentials by name or place

    def worse(self, other: "Place") -> "Place":
        return self if self.value >= other.value else other


#: Where XO keeps the files the owner attaches, relative to the project folder.
ATTACHMENTS = PurePosixPath(".xo/attachments")
#: Core's protected paths, relative to the worktree.
CORE_PROTECTED = (".xo/**", ".git", ".git/**")
#: Secrets by name, anywhere (ch 15 §2.2).
SECRET_NAMES = (".env", ".env.*", "*.pem", "id_*")
#: Secrets by place, under the person's home: credential folders and files
#: (ch 15 §2.2's, and the common tools' own).
SECRET_HOME = (".ssh", ".aws", ".config/gh", ".netrc", ".git-credentials", ".docker/config.json", ".npmrc",
               ".pypirc", ".kube/config", ".config/gcloud", ".azure", ".gnupg", ".pgpass", ".cargo/credentials",
               ".cargo/credentials.toml", ".gem/credentials", ".terraform.d/credentials.tfrc.json", ".vault-token",
               ".config/hub")
#: The processes' environments (``/proc/<pid>/environ``) hold their tokens.
PROC = Path("/proc")
#: Shell brace expansion ({a,b} or {1..3}): one word becomes several paths.
_BRACES = re.compile(r"\{[^{}]*(,|\.\.)[^{}]*\}")


@dataclass(frozen=True)
class Places:
    """Everything the engine needs to know about where a session stands."""

    session_folder: Path                 # resolved
    home: Path
    state_root: Path
    worktree: Path | None = None         # a thread's checkout; None for plain and coordinator sessions
    project_folder: Path | None = None   # the project's own checkout, holding .xo/ and its worktrees
    worktrees_root: Path | None = None   # <project>/.xo/worktrees
    git_common_dir: Path | None = None   # <project>/.git: its config and hooks are git's own
    cli_homes: tuple[Path, ...] = ()
    project_config_paths: tuple[str, ...] = ()   # the union of every agent's list (runtimes and manifests)

    def resolve(self, path: str, cwd: Path | None) -> tuple[Path, Path] | None:
        """``path``'s written and real forms, absolute; None when it can't be
        known: a variable, a glob, brace expansion, ``~user``, or a relative
        path while the working folder is unknown (``cwd`` None, after a
        ``cd`` XO can't follow)."""
        if not path or any(c in path for c in "$*?[") or _BRACES.search(path) or (
                path.startswith("~") and not path.startswith("~/") and path != "~"):
            return None
        expanded = str(self.home) + path[1:] if path.startswith("~") else path
        if not os.path.isabs(expanded) and cwd is None:
            return None
        written = Path(os.path.normpath(expanded if os.path.isabs(expanded) else cwd / expanded))
        return written, Path(os.path.realpath(written))

    def place(self, path: str, cwd: Path | None) -> Place | None:
        forms = self.resolve(path, cwd)
        if forms is None:
            return None
        place = Place.INSIDE
        for form in forms:
            place = place.worse(self._place_of(form))
        return place

    def holds_secret(self, path: str, cwd: Path | None, *, hidden: bool = True) -> bool:
        """Whether a read of everything under ``path`` reaches a place that
        holds credentials or private state (a secret place in the home, XO's
        state root, a CLI's home, the processes' environments). ``hidden``
        False: the read skips dot files and folders below ``path``."""
        forms = self.resolve(path, cwd)
        if forms is None:
            return False
        secrets = (*(self.home / name for name in SECRET_HOME), self.state_root, *self.cli_homes, PROC)
        for form in forms:
            for secret in secrets:
                if secret != form and _within(secret, form) and (
                        hidden or not any(part.startswith(".") for part in secret.relative_to(form).parts)):
                    return True
        return False

    def relative(self, path: str, cwd: Path | None) -> str | None:
        """``path`` relative to the session's folder (what a rule's glob
        matches), or None when it is outside or can't be known."""
        forms = self.resolve(path, cwd)
        if forms is None or not _within(forms[0], self.session_folder):
            return None
        return forms[0].relative_to(self.session_folder).as_posix()

    def protected(self, path: str, cwd: Path | None) -> str | None:
        """Why writing ``path`` is never allowed, or None. Only for writes:
        reading a protected file is an ordinary read."""
        forms = self.resolve(path, cwd)
        if forms is None:
            return None
        for form in forms:
            why = self._protected(form)
            if why:
                return why
        return None

    def _place_of(self, path: Path) -> Place:
        if _secret(path, self.home):
            return Place.SECRET
        if _within(path, self.state_root):
            return Place.STATE_ROOT
        if any(_within(path, h) or str(path).startswith(str(h)) for h in self.cli_homes):
            return Place.CLI_HOME
        if self.worktrees_root and _within(path, self.worktrees_root) and not (
                self.worktree and _within(path, self.worktree)):
            return Place.OTHER_WORKTREE
        if _within(path, self.session_folder) or self._attachment(path):
            return Place.INSIDE
        if _within(path, self.home) and path != self.home and path.relative_to(self.home).parts[0].startswith("."):
            return Place.HOME_CONFIG
        return Place.OUTSIDE

    def _attachment(self, path: Path) -> bool:
        """A file the owner attached in this session's project: XO wrote it in
        ``<project>/.xo/attachments/`` and the envelope names it, so reading it
        is routine from any of the project's checkouts. Writing there stays
        protected (``.xo/**``), and a link in it is judged by where it leads."""
        return self.project_folder is not None and _within(path, self.project_folder / ATTACHMENTS)

    def _checkout_of(self, path: Path) -> Path | None:
        """The checkout ``path`` is in: this session's own folder first, then
        another thread's worktree (only what is inside one: a worktree folder
        itself, or a file beside them, is the project's ``.xo/``), then the
        project folder. Each loads its own agent config, so the protected
        paths hold in every one of them. All paths here are resolved."""
        own = self.worktree or self.session_folder
        if _within(path, own):
            return own
        if self.worktrees_root and _within(path, self.worktrees_root):
            parts = path.relative_to(self.worktrees_root).parts
            if len(parts) > 1:
                return self.worktrees_root / parts[0]
        if self.project_folder and _within(path, self.project_folder):
            return self.project_folder
        return None

    def _protected(self, path: Path) -> str | None:
        if _within(path, self.state_root):
            return "it is XO's own state"
        if any(_within(path, h) or str(path).startswith(str(h)) for h in self.cli_homes):
            return "it is an agent CLI's own folder"
        if self.git_common_dir and (path == self.git_common_dir / "config"
                                    or _within(path, self.git_common_dir / "hooks")
                                    or path.name == "config.worktree"):
            return "it is git's own config or hooks, which run code"
        root = self._checkout_of(path)
        if root is None:
            return None
        rel = PurePosixPath(path.relative_to(root).as_posix())
        for pattern in (*CORE_PROTECTED, *self.project_config_paths):
            if glob_match(rel, pattern):
                return f"it is a protected path ({pattern}): files there change how agents and git behave"
        return None


def cli_homes(home: Path | None = None) -> tuple[Path, ...]:
    """Every agent CLI's own folder, from the manifests' ``home_dir``: the
    top-level folder under the person's home it lives in (a CLI keeps its
    config, logins and plugins beside its ``home_dir``), or the ``home_dir``
    itself when it isn't under the home."""
    home = home or Path.home()
    homes = []
    for manifest in all_agents():
        path = Path(os.path.expanduser(str(manifest.home_dir)))
        if _within(path, home) and path != home:
            path = home / path.relative_to(home).parts[0]
        if path not in homes:
            homes.append(path)
    return tuple(homes)


def declared_project_config_paths() -> tuple[str, ...]:
    """Every agent's project config, as its manifest declares it
    (``project_config_paths``): watched and protected in every checkout,
    whichever agent runs there (ch 16 §2.1: a thread of one CLI that writes
    another's hooks arms that CLI for the person)."""
    return tuple(sorted({glob for manifest in all_agents() for glob in manifest.project_config_paths}))


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _secret(path: Path, home: Path) -> bool:
    if any(fnmatchcase(path.name, pattern) for pattern in SECRET_NAMES):
        return True
    if _within(path, PROC) and path.name == "environ":
        return True
    return any(_within(path, home / name) for name in SECRET_HOME)


def glob_match(rel: PurePosixPath, pattern: str) -> bool:
    """``dir/**`` is the folder and everything in it; anything else is an
    fnmatch on the whole relative path."""
    if pattern.endswith("/**"):
        top = pattern[:-3]
        return str(rel) == top or str(rel).startswith(top + "/")
    return fnmatchcase(str(rel), pattern)
