"""What one doctor run knows: the roots, the time, and what it already read."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from services.doctor import inventory
from services.doctor.model import printable
from services.doctor.reading import ReadResult, classify

if TYPE_CHECKING:
    from services.doctor.projects import Project


def _components_snapshot() -> dict:
    """The in-process task record (services/background.py). Empty outside
    the server, where there is no task to report on."""
    try:
        from services import background

        return background.snapshot()
    except Exception:  # noqa: BLE001 - the record must never cost a report
        return {}


def _root(variable: str, default: str) -> Path:
    # The same resolution project_layout._resolved_root applies, without the
    # mkdir xo_projects_root() performs. Inside the server, roots.env has
    # already been applied to the environment (server.py:63-98).
    raw = (os.getenv(variable, "") or "").strip() or default
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        # A symlink loop or an unknown ~user: not a folder anyone can use.
        # Kept unresolved, so readable_dir() reports it as unavailable instead
        # of the whole run raising (and GET /api/doctor answering 500).
        return Path(os.path.abspath(os.path.expanduser(raw)))


@dataclass
class Context:
    state_root: Path
    projects_root: Path
    now: float
    host_state: str = ""
    host_projects: str = ""
    #: services.background.snapshot() taken when the run started.
    components: dict = field(default_factory=dict)
    _reads: dict = field(default_factory=dict, repr=False)
    _state_files: Optional[tuple[list[Path], bool, list[Path]]] = field(default=None, repr=False)
    _projects: Optional[list["Project"]] = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, now: Optional[float] = None) -> "Context":
        return cls(
            state_root=_root("QUIRQ_STATE_ROOT", "~/.quirq"),
            projects_root=_root("XO_PROJECTS_ROOT", "~/xo-projects"),
            now=time.time() if now is None else now,
            host_state=(os.getenv("QUIRQ_HOST_STATE_ROOT", "") or "").strip(),
            host_projects=(os.getenv("QUIRQ_HOST_PROJECTS_ROOT", "") or "").strip(),
            components=_components_snapshot(),
        )

    def read(self, path: Path, spec: Optional[inventory.Spec]) -> ReadResult:
        """Each file is parsed at most once per run, for each spec it is read with."""
        key = (Path(path), spec)
        if key not in self._reads:
            accepted = inventory.accepted(spec) if spec is not None else None
            stamped = inventory.stamp_required(spec) if spec is not None else True
            self._reads[key] = classify(Path(path), now=self.now, accepted=accepted, stamped=stamped)
        return self._reads[key]

    def state_files(self) -> tuple[list[Path], bool, list[Path]]:
        if self._state_files is None:
            self._state_files = inventory.walk_files(self.state_root)
        return self._state_files

    def projects(self) -> list["Project"]:
        if self._projects is None:
            from services.doctor import projects

            self._projects = projects.scan(self)
        return self._projects

    def project_label(self, key: str) -> str:
        """The project folder name that uses runtime key ``key`` (a pid or a
        folder key), else a short form of the key."""
        for project in self.projects():
            if key in project.keys_in_use:
                return project.name
        return f"{key[:8]}…" if len(key) > 12 else key

    def display(self, path: Path) -> str:
        """The host path in Docker, where container paths mean nothing to a
        person. Always encodable as UTF-8, because it also reaches the error
        messages of the move-aside action, which the report sanitizer never sees."""
        for root, host in ((self.state_root, self.host_state), (self.projects_root, self.host_projects)):
            if not host:
                continue
            try:
                return printable(str(Path(host) / Path(path).relative_to(root)))
            except ValueError:
                continue
        return printable(str(path))
