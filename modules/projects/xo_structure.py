"""The canonical ``.xo/`` directory every xo-project carries.

One definition of a project's portable metadata, and one idempotent function
that makes a folder conform to it. Every way a project comes to exist ends
here, so every project on every machine carries the same ``.xo/``:

- scaffolded: ``project_layout.scaffold_project`` (``POST /api/files/mkdir``
  with ``scaffold: true``, and agent creation);
- cloned through ``POST /api/xo-projects``
  (``services/project_management.clone_project``);
- auto-cloned by project sharing (``project_sharing/clone.py``);
- cloned or copied by hand straight into the projects root, which the watcher
  picks up on its next tick (:func:`ensure_xo_structure_if_changed`).

::

    .xo/                 committed with the project; travels through git and backups
    ├── project.json     identity: pid, name, owner, created_at, display name
    ├── todos.json       session-scoped todos           (todo API writes it)
    ├── workitems.json   durable work items             (workitems API)
    └── peers.json       who the project is shared with (peers API)

``agent.json`` is the one optional member: an adapter writes it when a folder
is attached to an agent backend, and its presence is the signal.

The golden sample is ``tests/fixtures/xo-project/``;
``tests/test_xo_structure.py`` holds this module, the sample, the schemas and
every creation path to one another.

Because this runs against folders a person owns:

- **Additive only.** A missing file is created. An existing file is never
  rewritten, repaired or reformatted: not a store's document, not a hand edit,
  and not a file that fails to parse (its store reports that one; writing over
  it would hide the damage and lose whatever it still holds).
- **Never raises.** A read-only folder, a symlinked ``.xo``, a vanished
  directory: each is reported on the returned :class:`EnsureReport` and logged
  once, and the caller carries on.
- **Only ``.xo/``.** A repository cloned into the root gains ``.xo/`` and
  nothing else; the work-tier template (``AGENTS.md``, ``memory/``) is for
  projects XO Space scaffolds.
- **Creation, not ownership.** Each store document starts empty, byte for byte
  as its owning store would write it, built from that store's own constants;
  from then on the store is the document's only writer. Identity comes from
  the identity sink itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from services.cowork_agent import project_layout
from . import peers_store, todos_store, workitems_store
from services.cowork_agent.visualizer.sinks import project_json
from services.storage.atomic_write import create_json_exclusive
from services.storage.reader import read_json

logger = logging.getLogger(__name__)

XO_DIRNAME = ".xo"

PROJECT = "project.json"
TODOS = "todos.json"
WORKITEMS = "workitems.json"
PEERS = "peers.json"

#: Every project's ``.xo/``, in creation order. ``.xo/`` travels through git,
#: so nothing here ignores it: an ignored file is one git may overwrite
#: silently on a merge.
CANONICAL_FILES: tuple[str, ...] = (PROJECT, TODOS, WORKITEMS, PEERS)

#: May appear in a project's ``.xo/``; never created here.
OPTIONAL_FILES: tuple[str, ...] = ("agent.json",)

# Records that belong to a projects root's own .xo/, never to a project's. A
# folder whose .xo/ holds one is a former projects root (the root was moved
# to its parent), not a project to fill in.
_WORKSPACE_RECORDS = ("space.json", "projects.json")


def empty_documents() -> dict[str, dict]:
    """The three store-owned documents, empty, as each store first writes one.

    Built from each store's own constants, so the schema a new project starts
    on cannot drift from the schema its store reads and writes. ``updated_at``
    is ``null`` because nothing has been recorded yet.
    """
    return {
        TODOS: {
            "$schema": todos_store._SCHEMA_REF,
            "schema": todos_store.TODOS_SCHEMA,
            "updated_at": None,
            "sessions": {},
        },
        WORKITEMS: {
            "$schema": workitems_store._SCHEMA_REF,
            "schema": workitems_store.WORKITEMS_SCHEMA,
            "updated_at": None,
            "items": {},
        },
        PEERS: {
            "$schema": peers_store._SCHEMA_REF,
            "schema": peers_store.PEERS_SCHEMA,
            "updated_at": None,
            "peers": [],
        },
    }


@dataclass(frozen=True)
class EnsureReport:
    """What one structure check found and did."""

    project_id: str
    #: Files this call created, in :data:`CANONICAL_FILES` order.
    created: tuple[str, ...] = ()
    #: Why the folder was not checked at all, or ``None`` when it was.
    skipped: str | None = None
    #: What was checked but could not be made canonical.
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.skipped is None and not self.problems


def ensure_xo_structure(project_id: str) -> EnsureReport:
    """Give a project's folder the canonical ``.xo/``, adding only what is missing.

    ``project_id`` is the folder's name under the projects root. Never raises.
    """
    try:
        return _ensure(project_id)
    except Exception:
        logger.exception("xo structure: unexpected failure checking %s", project_id)
        return EnsureReport(project_id, skipped="unexpected error; see the log")


# The last .xo/ signature each project was checked at (see below). Bounded like
# every other remember-once map in this tree.
_CHECKED: dict[str, tuple[int, int] | None] = {}
_CHECKED_MAX = 1024


def ensure_xo_structure_if_changed(project_id: str) -> EnsureReport | None:
    """:func:`ensure_xo_structure`, skipped while the folder's ``.xo/`` is unchanged.

    For the watcher, which visits every project every tick: an unchanged
    ``.xo/`` costs one ``lstat``. Creating, deleting or replacing any file in
    it moves the directory's mtime, so a deleted document is restored on the
    next tick, and a store's own atomic write costs one cheap re-check.
    Returns ``None`` when the check was skipped. Never raises.
    """
    try:
        xo = project_layout.xo_dir(project_id)
    except Exception:
        return None
    key = str(xo)
    if key in _CHECKED and _CHECKED[key] == _signature(xo):
        return None
    report = ensure_xo_structure(project_id)
    if len(_CHECKED) >= _CHECKED_MAX:
        _CHECKED.clear()
    _CHECKED[key] = _signature(xo)
    return report


def _signature(xo: Path) -> tuple[int, int] | None:
    try:
        st = xo.lstat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns)


def _ensure(project_id: str) -> EnsureReport:
    project = project_layout.project_dir(project_id)
    name = project.name
    if not project.is_dir():
        # Never mkdir a ghost project for an id that names no folder.
        return EnsureReport(name, skipped="project folder does not exist")

    xo = project / XO_DIRNAME
    if xo.is_symlink() or (xo.exists() and not xo.is_dir()):
        _warn_once(xo, "is a symlink or not a directory; leaving it alone")
        return EnsureReport(name, skipped=".xo is a symlink or not a directory")
    if any((xo / record).exists() for record in _WORKSPACE_RECORDS):
        _warn_once(
            xo,
            "holds workspace records (space.json / projects.json), so this "
            "folder is a former projects root, not a project; leaving it alone",
        )
        return EnsureReport(name, skipped=".xo holds workspace records")

    try:
        xo.mkdir(exist_ok=True)
    except OSError as exc:
        _warn_once(xo, f"cannot be created ({_why(exc)})")
        return EnsureReport(name, skipped=f"cannot create .xo ({_why(exc)})")

    created: list[str] = []
    problems: list[str] = []
    documents = empty_documents()
    for filename in CANONICAL_FILES:
        if filename == PROJECT:
            _ensure_identity(xo, name, created, problems)
            continue
        path = xo / filename
        try:
            made = create_json_exclusive(path, documents[filename])
        except OSError as exc:
            problems.append(f"{filename}: not created ({_why(exc)})")
            continue
        if made:
            created.append(filename)

    if created:
        logger.info("xo structure: %s gained %s", name, ", ".join(created))
    for problem in problems:
        _warn_once(xo, problem)
    return EnsureReport(name, tuple(created), None, tuple(problems))


def _ensure_identity(xo: Path, name: str, created: list[str], problems: list[str]) -> None:
    """``project.json`` through its own writers: identity, then descriptive fields."""
    path = xo / PROJECT
    existed = path.exists()
    try:
        # The identity sink mints into an absent file and refuses, with its own
        # warning, to write over one it cannot parse.
        project_json.fill_identity(xo, name)
    except OSError as exc:
        problems.append(f"{PROJECT}: identity not recorded ({_why(exc)})")
        return
    meta = read_json(path)
    if not isinstance(meta, dict):
        if path.exists():
            problems.append(f"{PROJECT}: does not parse; left untouched")
        return
    if not existed:
        created.append(PROJECT)
    if "display_name" in meta and "description" in meta:
        return
    try:
        project_layout.seed_project_metadata(name)
    except OSError as exc:
        problems.append(f"{PROJECT}: display name not recorded ({_why(exc)})")


def _why(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


# (path, message) pairs already logged, so the watcher repeats nothing per tick.
_WARNED: set[tuple[str, str]] = set()
_WARNED_MAX = 1024


def _warn_once(xo: Path, message: str) -> None:
    key = (str(xo), message)
    if key in _WARNED:
        return
    if len(_WARNED) >= _WARNED_MAX:
        _WARNED.clear()
    _WARNED.add(key)
    logger.warning("xo structure: %s %s", xo, message)
