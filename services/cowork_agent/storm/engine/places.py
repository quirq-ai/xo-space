"""Where a session stands: its project, the policy's ``Places`` and
``SessionRef``, and the per-project session-index row observation reads.

Blocking (path resolution, file writes): call these in a thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from services.cowork_agent.engine import sessions_io
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.storm.review import SessionRef
from services.cowork_agent.storm.review.paths import Places, cli_homes
from services.storage.paths import quirq_state_dir

#: The policy's role for each session role (ch 10 §5; ch 12 §3).
_POLICY_ROLE = {"plain": "plain", "thread": "thread", "project-coordinator": "coordinator",
                "workspace-coordinator": "coordinator"}
#: A thread's checkouts live here, inside its project (ch 13 §1).
WORKTREES = Path(".xo") / "worktrees"


@dataclass(frozen=True)
class Project:
    """The project a folder belongs to: its name under the projects root
    ("" for the root itself or a folder outside it) and its own checkout
    (None outside the projects root)."""

    name: str
    folder: Path | None


def project_of(cwd: str) -> Project:
    root = Path(os.path.realpath(xo_projects_root()))
    real = Path(os.path.realpath(cwd))
    if real == root or root not in real.parents:
        return Project("", None)
    name = real.relative_to(root).parts[0]
    return Project(name, root / name)


def policy_view(*, session_id: str, role: str, preset: str, cwd: str, thread_ts: str | None,
                project_pid: str | None, project_config_paths: tuple[str, ...]) -> tuple[Places, SessionRef]:
    """The ``Places`` and ``SessionRef`` the policy decides a session's
    checks with. ``project_folder`` is always set (B1 handoff §9): the
    project's checkout, or the session's own folder outside any project."""
    folder = Path(os.path.realpath(cwd))
    project = project_of(cwd)
    project_folder = project.folder or folder
    policy_role = "job" if preset == "job" else _POLICY_ROLE[role]
    git_dir = project_folder / ".git"
    places = Places(
        session_folder=folder, home=Path.home(), state_root=Path(os.path.realpath(quirq_state_dir())),
        worktree=folder if policy_role in ("thread", "job") and folder != project_folder else None,
        project_folder=project_folder, worktrees_root=project_folder / WORKTREES,
        git_common_dir=git_dir if git_dir.is_dir() else None, cli_homes=cli_homes(),
        project_config_paths=project_config_paths)
    return places, SessionRef(id=session_id, role=policy_role, cwd=folder, project_pid=project_pid,
                              thread_ts=thread_ts)


def write_index_row(*, session_id: str, agent: str, cwd: str, native_id: str, usage: dict[str, int],
                    updated_ms: int) -> bool:
    """Today's per-project session-index row (``sessionslist.d``), for an
    engine session (ch 07 §11), so observation and usage see it exactly like
    an adapter's own. Its ``sessionId`` is the CLI's native id: the UI reads a
    session's history at ``/api/messages/{native_id}`` (ch 17 §2.1)."""
    project = project_of(cwd)
    row = {"sessionId": native_id, "nativeSessionId": native_id, "directory": cwd, "backend": agent,
           "updatedAt": updated_ms, "usage": usage}
    return sessions_io.write_session_row(project.name, f"{agent}:{project.name}:storm:{session_id}", row)
