"""The shell's environment and starting folder.

The environment is the server's own, like Coder's or VS Code's terminal, so
``git push`` (Coder's ``GIT_ASKPASS``/``GIT_SSH_COMMAND``) and the agent CLIs
keep working. What is removed is what belongs to *this server process*, not
to the user: the Space's own swarm credential (``XO_API_KEY``), the server's
``PORT``/``HOST`` (a dev server started in the terminal would try to take
Space's port) and the server's Python virtualenv. Removing variables is
hygiene, not a boundary: the shell runs as the same user and can read the
server's files and /proc entry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from services.cowork_agent import project_layout
from services.errors import ServiceError

_DENY = frozenset({
    "XO_API_KEY", "PORT", "HOST", "UVICORN_RELOAD", "VIRTUAL_ENV", "PYTHONHOME",
    "PWD", "OLDPWD", "LINES", "COLUMNS", "TERM_PROGRAM_VERSION",
})


def _extra_denied() -> set[str]:
    raw = os.getenv("QUIRQ_TERMINAL_ENV_DENY", "") or ""
    return {name.strip() for name in raw.split(",") if name.strip()}


def _without_server_venv(path: str) -> str:
    """PATH minus this server's virtualenv ``bin``, so ``python``/``pip`` in the
    terminal are the user's own and cannot install into Space's environment."""
    if sys.prefix == sys.base_prefix:
        return path
    venv_bin = os.path.realpath(os.path.join(sys.prefix, "bin"))
    kept = [p for p in path.split(os.pathsep) if p and os.path.realpath(p) != venv_bin]
    return os.pathsep.join(kept)


def child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for name in _DENY | _extra_denied():
        env.pop(name, None)
    env["PATH"] = _without_server_venv(env.get("PATH", "") or "/usr/local/bin:/usr/bin:/bin")
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["TERM_PROGRAM"] = "xo-space"
    return env


class LocationError(ServiceError):
    pass


def start_folder(project: str | None, path: str | None = None) -> Path:
    """Where a terminal starts: the project's folder, or a folder inside it
    (``path``, relative to the project or absolute: a thread's worktree, say),
    or for the workspace terminal (no project) the projects root itself."""
    root = project_layout.xo_projects_root()
    if project is None:
        if path:
            raise LocationError("bad_path", "A folder can only be given together with its project.", status=400)
        return root
    name = project.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise LocationError("bad_project", "That is not a project name.", status=400)
    folder = project_layout.project_dir(name)
    if folder.parent != root or not folder.is_dir():
        raise LocationError("project_not_found", f"No project folder named {name!r} under {root}.", status=404)
    if not path:
        return folder
    if "\0" in path or ".." in Path(path).parts:
        raise LocationError("bad_path", "The folder must be inside the project.", status=400)
    inner = folder / path  # an absolute path stays absolute; either way it must land inside
    try:
        inside = inner.resolve().is_relative_to(folder.resolve())
    except OSError:
        inside = False
    if not inside:
        raise LocationError("bad_path", "The folder must be inside the project.", status=400)
    if not inner.is_dir():
        raise LocationError("path_not_found", f"No folder {path!r} in project {name!r}.", status=404)
    return inner


def project_path(project: str | None, path: str | None) -> str | None:
    """``path`` as stored and listed: relative to the project folder, normalised
    (``.xo/worktrees/thread-1`` whether it was given that way or absolute).
    None for the project folder itself or the workspace."""
    if project is None or not path:
        return None
    rel = os.path.relpath(start_folder(project, path), start_folder(project))
    return None if rel == "." else rel
