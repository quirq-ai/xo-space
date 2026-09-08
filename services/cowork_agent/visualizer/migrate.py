"""T21 — the one-time, idempotent move of the pre-T19 runtime tier.

docs/syncplan.md §9 (T19, T21). A project created before the tier split has the
old **flat** layout: machine-local telemetry — ``stats.json``, ``sync.json``,
``timeline.jsonl`` (plus its rotations) and the whole ``sessions/`` index — sat
inside ``<project>/.xo/`` next to the synced contract. T19 moved every writer
and reader to ``~/.quirq/projects/<key>/`` and left a read-through so nothing
was lost, but it moved no bytes. This module moves them.

**Why the bytes have to move, given the read-through already works.**
``<project>/.xo/`` is the SYNCED tier, and T21 *force-includes* it in the backup
tarball (``xo_projects_sync/tarball.py``). A ``timeline.jsonl`` or a session
index left behind there is therefore machine-local telemetry that would travel
to every other machine and into every restore — precisely what §2 R-TIER
forbids. The same argument the workspace views make in
``visualizer/workspace/views.sweep_abandoned`` (T20), which owns the workspace
half of this and is deliberately not duplicated here.

**When it runs.** Once, from the server lifespan, *before* the watcher task is
created and before the app yields — so no sink and no request handler can be
writing the same paths while it works. It is idempotent and inert once done: a
migrated machine costs a handful of ``stat`` calls per project and rewrites
nothing.

**What it never does.** It does not overwrite. A file already present in the
runtime tier was written *after* the move, so it is the newer of the two and
wins; the stale in-tree copy is dropped rather than promoted. And it never
raises: a startup pass that cannot migrate one project must not stop the server
from booting.

It names no agent — pure infrastructure.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.sinks import project_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)


# Flat files that used to live in the project's synced directory and are
# machine-local runtime state (syncplan §4). The synced contract —
# ``project.json``, ``agent.json``, ``peers.json``, ``todos.json`` — is
# deliberately absent: it stays in the project tree and must travel.
#
# ``activity.json`` is on the list for a different reason from the rest:
# nothing has written it since presence moved to ``~/.quirq/watcher/activity/``,
# so every copy on disk is already dead. It is relocated rather than deleted —
# a migration that destroys data on a guess is a worse failure than one that
# leaves a stale file in a directory nothing syncs.
_RUNTIME_FILES: tuple[str, ...] = (
    "stats.json",
    "sync.json",
    "activity.json",
)

# The append-only log and every rotation ``sinks/timeline.py`` renamed
# (``timeline.<stamp>.jsonl``, five kept).
_RUNTIME_GLOBS: tuple[str, ...] = ("timeline*.jsonl",)

# Lines a project ``.gitignore`` may carry that hide the whole synced tier.
# Only the blanket forms: see :func:`_narrow_gitignore` for why this list is
# deliberately not extended.
_BLANKET_IGNORE_LINES = frozenset({".xo", ".xo/", "/.xo", "/.xo/"})


def _drop(path: Path) -> None:
    """Delete ``path`` — a stale source the destination already supersedes."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _relocate(src: Path, dst: Path) -> None:
    """Move ``src`` → ``dst``. The destination always wins.

    Three cases, and the third is the one that matters:

    * ``dst`` absent — a plain move, which is a rename inside one filesystem.
    * both are directories — merged child by child, recursively, so a runtime
      ``sessions/`` that already holds shards keeps them *and* adopts the
      pre-move whole-file index beside them (``engine.sessions_io`` reads the
      whole file at lower precedence than the shards, which is exactly the
      order those two want).
    * anything else — the destination is the newer copy, so the source is
      dropped rather than promoted over it.
    """
    if not src.exists() and not src.is_symlink():
        return
    if not dst.exists() and not dst.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return
    if (
        src.is_dir()
        and not src.is_symlink()
        and dst.is_dir()
        and not dst.is_symlink()
    ):
        for child in sorted(src.iterdir()):
            _relocate(child, dst / child.name)
        _drop(src)
        return
    _drop(src)


def _pending_sources(synced: Path) -> list[Path]:
    """Runtime-tier files still sitting in a project's synced directory.

    Computed before anything else happens so a migrated machine can answer
    "nothing to do" without minting identities or touching the runtime home.
    """
    pending: list[Path] = []
    for fname in _RUNTIME_FILES:
        candidate = synced / fname
        if candidate.exists():
            pending.append(candidate)
    for pattern in _RUNTIME_GLOBS:
        pending.extend(sorted(synced.glob(pattern)))
    sessions = synced / project_layout.LEGACY_SESSIONS_SUBDIR
    if sessions.is_dir() and not sessions.is_symlink():
        pending.append(sessions)
    return pending


def _narrow_gitignore(name: str) -> bool:
    """Stop a project ``.gitignore`` from ignoring all of its synced tier.

    Returns ``True`` iff the file was rewritten.

    ``project_template/AGENTS.md`` tells every agent that the synced tier is
    "portable project metadata, gitignored" while the template ships no
    ``.gitignore``. An agent that acts on that instruction inside a git repo
    removes the directory from ``git ls-files --cached --others
    --exclude-standard`` — which is how ``xo_projects_sync/tarball.py`` picks
    its file list in a repo — and the backup then carries no ``project.json``,
    so no ``pid``, so a restore mints a fresh one and the two copies become
    different projects. Un-ignoring is the tidy half of closing that hole.

    **Only the blanket lines are dropped**, and the list is deliberately not
    extended to ``.xo/*``, ``**/.xo/`` or a negation. Rewriting a pattern whose
    intent we are guessing at is how a tidy-up eats a user's file. The actual
    guarantee is the force-include in ``tarball.py``: it does not care what the
    ``.gitignore`` says, in any of its forms.
    """
    gitignore = project_layout.project_dir(name) / ".gitignore"
    try:
        if not gitignore.is_file():
            return False
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    kept = [line for line in lines if line.strip() not in _BLANKET_IGNORE_LINES]
    if len(kept) == len(lines):
        return False
    try:
        gitignore.write_text(
            "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8"
        )
    except OSError:
        logger.warning("migrate: could not rewrite %s", gitignore)
        return False
    logger.info(
        "migrate: %s/.gitignore no longer hides the synced project tier", name
    )
    return True


def _migrate_one(name: str) -> bool:
    """Migrate one project. Returns ``True`` iff anything on disk changed."""
    synced = project_layout.xo_dir(name)
    if not synced.is_dir():
        return False

    changed = _narrow_gitignore(name)

    pending = _pending_sources(synced)
    if not pending:
        return changed

    # The runtime home is keyed by ``project.json:pid``, so the identity has to
    # exist before there is a stable place to move anything to. The fill is
    # idempotent, no-ops once the pid is minted, and refuses to mint over a
    # project.json it cannot read (so a corrupt file is a deferral, never a
    # rewritten identity).
    try:
        project_json.fill_identity(synced, name)
    except Exception:
        logger.exception("migrate: identity fill failed for %s", name)

    meta = project_layout.load_project(name) or {}
    if meta.get("_template") or not meta.get("pid"):
        # No stable key yet. The read-through in ``project_layout`` keeps every
        # one of these files readable where it is, and the next startup — after
        # a watcher tick has minted the pid — picks the project up.
        logger.info("migrate: %s has no pid yet; deferring the tier move", name)
        return changed

    runtime = project_layout.runtime_dir_for_project(name, create=True)
    if runtime is None:
        logger.warning("migrate: %s has no resolvable runtime home; skipping", name)
        return changed

    for src in pending:
        _relocate(src, runtime / src.relative_to(synced))
    logger.info(
        "migrate: moved %d runtime path(s) out of %s into %s",
        len(pending),
        synced,
        runtime,
    )
    return True


def migrate_runtime_layout() -> int:
    """Migrate every project on disk. Returns how many changed.

    Safe on every startup: idempotent, cheap once migrated, and it never
    raises — a project that cannot be migrated is logged and skipped, and the
    read-through keeps its files readable exactly where they are.
    """
    changed = 0
    try:
        names = list_project_ids()
    except Exception:
        logger.exception("migrate: could not list projects; skipping the pass")
        return 0
    for name in names:
        try:
            if _migrate_one(name):
                changed += 1
        except Exception:
            logger.exception("migrate: failed for project %s", name)
    if changed:
        logger.info("migrate: updated the on-disk tier layout for %d project(s)", changed)
    return changed
