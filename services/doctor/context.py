"""What one doctor run knows: the roots, the time, and what it already read."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from services.doctor import inventory
from services.doctor.reading import ReadResult, classify

if TYPE_CHECKING:
    from services.doctor.projects import Project


def _root(variable: str, default: str) -> Path:
    # The same resolution project_layout._resolved_root applies, without the
    # mkdir xo_projects_root() performs. Inside the server, roots.env has
    # already been applied to the environment (server.py:63-98).
    raw = (os.getenv(variable, "") or "").strip() or default
    return Path(raw).expanduser().resolve()


@dataclass
class Context:
    state_root: Path
    projects_root: Path
    now: float
    host_state: str = ""
    host_projects: str = ""
    _reads: dict = field(default_factory=dict, repr=False)
    _state_files: Optional[tuple[list[Path], bool]] = field(default=None, repr=False)
    _projects: Optional[list["Project"]] = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, now: Optional[float] = None) -> "Context":
        return cls(
            state_root=_root("QUIRQ_STATE_ROOT", "~/.quirq"),
            projects_root=_root("XO_PROJECTS_ROOT", "~/xo-projects"),
            now=time.time() if now is None else now,
            host_state=(os.getenv("QUIRQ_HOST_STATE_ROOT", "") or "").strip(),
            host_projects=(os.getenv("QUIRQ_HOST_PROJECTS_ROOT", "") or "").strip(),
        )

    def read(self, path: Path, spec: Optional[inventory.Spec]) -> ReadResult:
        """Each file is parsed at most once per run, for each spec it is read with."""
        key = (Path(path), spec)
        if key not in self._reads:
            accepted = inventory.accepted(spec) if spec is not None else None
            self._reads[key] = classify(Path(path), now=self.now, accepted=accepted)
        return self._reads[key]

    def state_files(self) -> tuple[list[Path], bool]:
        if self._state_files is None:
            self._state_files = inventory.walk_files(self.state_root)
        return self._state_files

    def projects(self) -> list["Project"]:
        if self._projects is None:
            from services.doctor import projects

            self._projects = projects.scan(self)
        return self._projects

    def display(self, path: Path) -> str:
        """The host path in Docker, where container paths mean nothing to a person."""
        for root, host in ((self.state_root, self.host_state), (self.projects_root, self.host_projects)):
            if not host:
                continue
            try:
                return str(Path(host) / Path(path).relative_to(root))
            except ValueError:
                continue
        return str(path)
