"""T21 — the one-time, idempotent move of the pre-T19 runtime tier."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.sinks import project_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)


# Flat files that used to live in the project's synced directory and are
# machine-local runtime state (syncplan §4).
_RUNTIME_FILES: tuple[str, ...] = (
    "stats.json",
    "sync.json",
    "activity.json",
)

# The append-only log and every rotation ``sinks/timeline.py`` renamed
# (``timeline.<stamp>.jsonl``, five kept).
_RUNTIME_GLOBS: tuple[str, ...] = ("timeline*.jsonl",)

# Lines a project ``.gitignore`` may carry that hide the whole synced tier.
_BLANKET_IGNORE_LINES = frozenset({
    ".xo", ".xo/", "/.xo", "/.xo/", "**/.xo", "**/.xo/", ".xo/*", ".xo/**",
})


def _drop(path: Path) -> None:
    """Delete ``path`` — a stale source the destination already supersedes."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _relocate(src: Path, dst: Path) -> None:
    """Move ``src`` → ``dst``. The destination always wins."""
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
    """Runtime-tier files still sitting in a project's synced directory."""
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


def _warn_if_gitignored(name: str) -> None:
    gitignore = project_layout.project_dir(name) / ".gitignore"
    try:
        if not gitignore.is_file():
            return
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    hits = sorted({s for s in (ln.strip() for ln in lines) if s in _BLANKET_IGNORE_LINES})
    if hits:
        logger.warning(
            "migrate: %s/.gitignore hides the synced project tier (%s); backups "
            "force-include .xo/, but your own commits will not carry project.json",
            name, ", ".join(hits),
        )


def _migrate_one(name: str) -> bool:
    """Migrate one project. Returns ``True`` iff anything on disk changed."""
    synced = project_layout.xo_dir(name)
    if not synced.is_dir():
        return False

    _warn_if_gitignored(name)

    pending = _pending_sources(synced)
    if not pending:
        return False

    # The runtime home is keyed by ``project.json:pid``, so the identity has to
    # exist before there is a stable place to move anything to.
    try:
        # ``upgrade_placeholder_owner=False``: this call exists only to mint a
        # pid, and the migration must not rewrite the synced tree for any other
        # reason.
        project_json.fill_identity(synced, name, upgrade_placeholder_owner=False)
    except Exception:
        logger.exception("migrate: identity fill failed for %s", name)

    meta = project_layout.load_project(name) or {}
    if meta.get("_template") or not meta.get("pid"):
        # No stable key yet.
        logger.info("migrate: %s has no pid yet; deferring the tier move", name)
        return False

    runtime = project_layout.runtime_dir_for_project(name, create=True)
    if runtime is None:
        logger.warning("migrate: %s has no resolvable runtime home; skipping", name)
        return False

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
    """Migrate every project on disk. Returns how many changed."""
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
