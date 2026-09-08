"""
Canonical project layout for ~/xo-projects/<name>/.

A project is just a folder. It is backend-agnostic: any agent (claude_code,
openclaw, future tools) can launch against it. The on-disk shape is what
makes the agent perform well — this module owns it.

    ~/xo-projects/<name>/
    ├── AGENTS.md            stable prefix, universal contract
    ├── OBJECTIVES.md        north-star outcomes
    ├── WORKSPACE.md         current state of play
    ├── CLAUDE.md            one-line pointer to AGENTS.md
    └── .xo/
        ├── project.json     identity {schema, pid, name, owner_user_id, created_at}
        │                    + {display_name, description} + git provenance
        ├── memory/{semantic,episodic,procedural,working}/
        ├── artifacts/{drafts,final}/
        ├── state/           SOUL.md, STATUS.md, IDENTITY.md, USER.md
        ├── skills/{user-built,learned}/
        └── context/         config.json, cache.md

Concerns:
- path resolution (env-driven root, every subfolder)
- idempotent scaffolding (re-running fills in missing pieces, never clobbers)
- project metadata read/write
- filesystem-driven listing (no backend coupling)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.local_state import quirq_state_dir
from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_atomic,
    write_json_owned,
)

logger = logging.getLogger(__name__)

# ── Template source ────────────────────────────────────────────────────────────

_SKIP_NAMES = {".git"}


_BUNDLED_TEMPLATE = Path(__file__).parent / "project_template"


def _template_dir() -> Path:
    """Return the project template directory.

    Priority: ``XO_PROJECT_TEMPLATE`` env var → bundled ``project_template/``
    shipped with this package (always present).
    """
    raw = (os.getenv("XO_PROJECT_TEMPLATE", "") or "").strip()
    if raw:
        t = Path(raw).expanduser().resolve()
        if t.is_dir():
            return t
    return _BUNDLED_TEMPLATE


def _copy_template(src: Path, dst: Path) -> None:
    """Recursively copy src → dst, skipping .git, never clobbering existing files."""
    for item in src.iterdir():
        if item.name in _SKIP_NAMES:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_template(item, target)
        elif not target.exists():
            target.write_bytes(item.read_bytes())


# ── Roots ─────────────────────────────────────────────────────────────────────


# Memo for the realpath walk inside :func:`xo_projects_root`. Keyed on
# everything the resolution depends on, so an env change is a miss, not a
# stale hit. See :func:`_resolved_root` for why it exists.
_ROOT_RESOLUTION_CACHE: dict[tuple[str, str, str], Path] = {}
_ROOT_RESOLUTION_CACHE_MAX = 64


def _resolved_root(raw: str) -> Path:
    """``Path(raw).expanduser().resolve()``, with the realpath walk memoized.

    ``resolve()`` lstats every component of the path, and
    :func:`xo_projects_root` runs roughly ``10N + 49`` times per watcher
    tick (docs/syncplan.md §10, T23), so that walk dominated the tick's
    lstat count for a value that cannot change between two calls unless
    the environment does.

    The key therefore carries the raw setting *and* the two env vars
    ``expanduser`` consults — tests patch ``HOME`` — so a changed
    environment always misses. A relative setting additionally depends on
    the process CWD, which is not in the key, so those are never cached.
    """
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return expanded.resolve()
    key = (raw, os.environ.get("HOME", ""), os.environ.get("USERPROFILE", ""))
    hit = _ROOT_RESOLUTION_CACHE.get(key)
    if hit is not None:
        return hit
    resolved = expanded.resolve()
    # Bounded: a long-lived process only ever sees one or two roots; a
    # test suite churns through temp dirs. Clearing wholesale keeps the
    # dict from growing without needing an LRU.
    if len(_ROOT_RESOLUTION_CACHE) >= _ROOT_RESOLUTION_CACHE_MAX:
        _ROOT_RESOLUTION_CACHE.clear()
    _ROOT_RESOLUTION_CACHE[key] = resolved
    return resolved


def xo_projects_root() -> Path:
    """User-facing projects directory.

    Sourced from ``XO_PROJECTS_ROOT`` env var; defaults to ``~/xo-projects``.
    Created on read so callers never have to guard for first-run.
    """
    raw = (os.getenv("XO_PROJECTS_ROOT", "") or "").strip() or "~/xo-projects"
    root = _resolved_root(raw)
    # Create-on-read is a contract other callers depend on, so the check
    # stays on every call — but it is now a single stat instead of a
    # mkdir that fails with EEXIST *plus* the is_dir() stat pathlib does
    # to decide whether EEXIST was acceptable. A root that is missing —
    # never created, or deleted underneath us — is still created here,
    # and a root occupied by a non-directory still raises, exactly as
    # ``mkdir(parents=True, exist_ok=True)`` did.
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_xo_dir() -> Path:
    """Workspace-tier ``.xo/`` directory at ``~/xo-projects/.xo/``.

    The **synced** half of the workspace tier. Since T20 it holds only the
    documents a clone would want — ``space.json`` (the durable Space record),
    ``projects.json`` (the registry) and ``xo.json`` (the frontend manifest).
    Every derived workspace view left it for :func:`workspace_runtime_dir`.

    Does not auto-create on read — the record writers create it when they
    write, and a reader must not conjure a directory it only wanted to look in.
    """
    return xo_projects_root() / ".xo"


def workspace_runtime_dir() -> Path:
    """``~/.quirq/workspace/`` — the derived workspace views (syncplan T20).

    R-TIER, applied to the workspace tier the way T19 applied it per project:
    the rollups (``graph.json``, ``dashboard.json``, ``sessions.json``,
    ``stats.json``, ``timeline.jsonl`` and everything under ``sessions/``) are
    100% recomputed from a walk of the projects root, so they are machine-local
    and never sync. That is also what makes their two unlocked writers — the
    watcher tick and a request thread rebuilding a stale view — harmless: the
    worst case a lost update can cost here is one rebuild.

    Unlike :func:`xo_runtime_root` this is not resolved and joins no untrusted
    segment, so it needs none of that function's clamp; it is a plain sibling
    of ``~/.quirq/watcher/`` (``visualizer.state.watcher_state_dir``). Nothing
    is created here either — the sinks' atomic writes create the parents.
    """
    return quirq_state_dir() / "workspace"


def workspace_sessions_dir() -> Path:
    """``~/.quirq/workspace/sessions/`` — the workspace-tier session views.

    The union of every project's session index and augment file. Derived, so
    it moved with the rest of the workspace views in T20; readers and the one
    writer both come through here rather than hand-building the join, which is
    what the chokepoint guard is for. Creates nothing.
    """
    return workspace_runtime_dir() / "sessions"


# ── Runtime home (machine-local; never synced) ─────────────────────────────────
#
# Machine-local telemetry lives OUTSIDE every project tree, in the Quirq state
# home keyed by ``project.json:pid`` (docs/syncplan.md §4). This makes
# share-safety a filesystem invariant rather than a ``.gitignore`` policy:
# runtime cannot be committed, tarred, or leaked through a missing ``.git/``
# because it is not in the project tree at all.
#
# These helpers are pure path math and name NO agent — the tier decision belongs
# here, never in an adapter.


def xo_runtime_root() -> Path:
    """Per-project runtime home, ``~/.quirq/projects/`` by default.

    Rooted at :func:`~services.cowork_agent.local_state.quirq_state_dir`
    (``QUIRQ_STATE_ROOT``); there is deliberately no separate root env var, so
    one ``rm -rf ~/.quirq`` is still a clean total reset.

    **The result is resolved**, which is load-bearing rather than cosmetic:
    :func:`runtime_dir` clamps with ``target.resolve().relative_to(root)``, and
    an *unresolved* root makes that comparison fail spuriously wherever the path
    has a symlink component — macOS ``/tmp``, a symlinked home, a Docker bind
    mount. ``quirq_state_dir()`` does not resolve, so the resolution happens
    here and both sides of the clamp are then in realpath form.

    Unlike :func:`xo_projects_root` this **does not create anything**. It is
    reached on read paths (a route asking about a project id that may not
    exist), and a helper that ``mkdir``s on read would conjure a runtime
    directory for every id anyone ever asks about. The runtime writers already
    ``mkdir(parents=True)`` before writing, which is the right place for it.
    """
    return _resolved_root(str(quirq_state_dir())) / "projects"


# The runtime key is a single path segment joined straight into the runtime
# home, and it comes from ``project.json`` — which is the SYNCED tier. That file
# is designed to travel between machines and a restore drops a snapshot's copy
# into place wholesale, so its ``pid`` is untrusted input. An absolute or
# traversing value silently redirects every runtime write
# (``Path("~/.quirq/projects") / "/etc/cron.d"`` is just ``/etc/cron.d``), and
# the runtime writers ``mkdir(parents=True)`` before writing — so a bad key is
# an attacker-chosen *write*, not a bad read. Keys are minted as UUIDs, so a
# conservative charset costs nothing.
#
# ``normalize_agent_id`` is NOT a substitute: it lowercases and *substitutes*
# invalid characters rather than rejecting them, and it has no traversal or
# symlink check. It is only ever used here as the fallback *key*, whose output
# charset (``[a-z0-9_-]``, ≤ 64, or ``"main"``) already satisfies the shape
# below.

_SAFE_RUNTIME_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _truncate(value: str, limit: int = 64) -> str:
    """Shorten an untrusted value for log/error output (it may be huge)."""
    return value if len(value) <= limit else value[:limit] + "...(truncated)"


def _is_safe_runtime_key(value: str) -> bool:
    """True iff ``value`` is safe to use as a single runtime path segment.

    Accepts UUID-shaped pids (the minted form) and the same conservative
    ``[A-Za-z0-9_-]{1,64}`` shape the fallback key — ``normalize_agent_id`` —
    already produces. That charset excludes ``/``, ``\\``, ``.`` and NUL, so
    absolute paths, ``..`` traversal and the ``.``/``..`` self-references cannot
    survive it; the explicit checks first just make the intent unmissable.
    """
    if not value:
        return False
    if any(bad in value for bad in ("/", "\\", "\x00")):
        return False
    if "." in value:  # covers "." and ".." as well as any dotted segment
        return False
    return bool(_SAFE_RUNTIME_KEY.fullmatch(value))


def runtime_dir(pid: str) -> Path:
    """Per-project runtime directory ``~/.quirq/projects/<pid>/`` (pid-keyed).

    ``pid`` must be a single safe segment (see :func:`_is_safe_runtime_key`).
    Callers holding an untrusted value straight out of ``project.json`` should
    resolve it through :func:`runtime_key` / :func:`project_runtime_dir`, which
    sanitise and fall back; here an unusable key raises ``ValueError`` rather
    than resolving to a path outside the runtime home.

    The asymmetry with :func:`runtime_key` is deliberate. ``runtime_key`` has a
    safe answer for a bad pid (the folder name: wrong-but-contained), so it
    falls back. This function has none, so it fails **closed**.
    """
    root = xo_runtime_root()
    key = str(pid)
    if not _is_safe_runtime_key(key):
        raise ValueError(
            f"unsafe runtime key {_truncate(key)!r}: expected a single "
            "[A-Za-z0-9_-]{1,64} path segment (resolve untrusted pids via runtime_key)"
        )
    target = (root / key).resolve()
    # Belt and braces. The charset check above already makes escape impossible,
    # but the clamp is what keeps a future caller honest — and it also catches a
    # symlinked ``<root>/<key>`` pointing out of the runtime home, which the
    # charset check cannot see. ``root`` is resolved by ``xo_runtime_root()``,
    # so this compares realpath to realpath and cannot fail spuriously on a
    # symlinked root.
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(
            f"runtime key {_truncate(key)!r} resolves outside the runtime home {root}"
        ) from None
    return target


def runtime_sessions_dir(pid: str) -> Path:
    """Per-project runtime sessions dir ``~/.quirq/projects/<pid>/sessions/``."""
    return runtime_dir(pid) / "sessions"


# ── Name → runtime key resolution (the one place that reads project.json) ──────


def runtime_key(name: str) -> str:
    """Resolve a project folder name to its runtime-store key.

    The key is ``project.json:pid`` once the watcher has minted it. Until then
    (template not yet filled, or no ``project.json``) we fall back to the
    normalized folder name. The fallback is transitional: it only applies in
    the brief pre-mint window on a brand-new project. Runtime is machine-local,
    so a short-lived folder-name key is harmless.

    The pid is validated before it is handed to the path layer: ``project.json``
    is synced, so a corrupt or hostile copy can arrive from another machine or a
    snapshot restore. An unusable pid falls back to the same folder-name key —
    wrong-but-contained beats redirecting every runtime write out of the runtime
    home.
    """
    meta = load_project(name)
    if isinstance(meta, dict):
        pid = meta.get("pid")
        if pid and not meta.get("_template", False):
            key = str(pid)
            if _is_safe_runtime_key(key):
                return key
            logger.warning(
                "project %s: ignoring unsafe pid %r in .xo/project.json; "
                "keying runtime by folder name instead",
                name,
                _truncate(key),
            )
    return normalize_agent_id(name)


def project_runtime_dir(name: str) -> Path:
    """Runtime directory for a project given its folder name."""
    return runtime_dir(runtime_key(name))


def project_runtime_sessions_dir(name: str) -> Path:
    """Runtime sessions directory for a project given its folder name."""
    return runtime_sessions_dir(runtime_key(name))


# ── The T19 runtime tier: resolution, sub-paths, and the read-through ─────────
#
# ``stats.json``, ``timeline.jsonl``, ``sync.json`` and everything under
# ``sessions/`` moved out of ``<project>/.xo/`` and into
# ``~/.quirq/projects/<key>/`` (syncplan §9, T19). Three things had to be true
# for that move to be safe, and all three live here rather than at the call
# sites:
#
# 1. **The resolution is applied here, not by the caller.** No adapter imported
#    ``resolve_project_dirname`` anywhere in the tree before T19 — the hermes
#    and openclaw bypasses were not two stragglers, they were the universal
#    state. A helper that trusts its caller to have normalised the name would
#    have re-created exactly that hole one layer down.
# 2. **A missing project resolves to nothing, not to a fresh directory.** The
#    identity sink refuses to mint a pid when the project folder does not exist
#    (``visualizer/sinks/project_json.fill_identity``), so a pid may
#    legitimately never appear. Resolution therefore SKIPS — returns ``None`` —
#    instead of ``mkdir``-ing a runtime home for a project that isn't there.
#    ``create=True`` is opt-in and is what a writer passes.
# 3. **Reads fall through to the pre-move location.** There is no startup file
#    mover; the migration is the read-through + copy-on-first-write pattern the
#    ``~/.xo-cowork`` → ``~/.quirq`` move already used three times. The
#    read-through is expressed as a path choice (:func:`runtime_read_path`) so
#    every reader gets it from one place.

# Sub-paths *below* a project's runtime directory. Exported as constants so
# callers never join the literals themselves — that join is the tier decision,
# and it belongs to this module (see tests/test_path_chokepoint_guard.py).
RUNTIME_SESSIONS_SUBDIR = Path("sessions")

# The partitioned session index (syncplan T19, inherited from T4).
# ``sessionslist.json`` had 15 unlocked read-modify-write sites on ONE file, so
# two concurrent writers holding two distinct rows lost one of them. R-CONTEND
# says partition before you lock: each row is now its own shard file here, and
# readers merge the directory. A writer never reads or rewrites another
# writer's row, so there is no lost-update window left to bound.
RUNTIME_SESSION_SHARDS_SUBDIR = RUNTIME_SESSIONS_SUBDIR / "sessionslist.d"

# Pre-T19 home of the moved files, still read (never written) so a project that
# predates the move keeps serving its history.
LEGACY_SESSIONS_SUBDIR = Path("sessions")


def _project_dirname_if_present(name: str) -> str | None:
    """Resolved directory name for ``name``, or ``None`` if no such folder.

    ``resolve_project_dirname`` answers with the normalised id for a project
    that does not exist yet — correct for creation, wrong for runtime, where
    the answer decides whether we conjure a directory. One ``is_dir()`` on the
    resolved name settles it exactly (cases 1-2 of the resolver only ever
    return a listed directory, so this cannot be fooled by a case-insensitive
    filesystem the way a bare probe of the normalised name could).
    """
    dirname = resolve_project_dirname(name)
    if not _is_safe_segment(dirname):
        return None
    try:
        if not (xo_projects_root() / dirname).is_dir():
            return None
    except OSError:
        return None
    return dirname


# Pre-mint homes already adopted by this process, so the check below costs one
# ``is_dir()`` the first time a project is resolved and nothing after that.
# Adoption is idempotent and can only ever be needed once per project — the
# moment a pid exists, no writer resolves to the pre-mint key again. Bounded
# the same way as the root caches above: a long-lived process sees a handful of
# projects, a test suite churns through temp roots.
_PREMINT_ADOPTED: set[str] = set()
_PREMINT_ADOPTED_MAX = 256


def _remember_adopted(marker: str) -> None:
    if len(_PREMINT_ADOPTED) >= _PREMINT_ADOPTED_MAX:
        _PREMINT_ADOPTED.clear()
    _PREMINT_ADOPTED.add(marker)


def _merge_runtime_tree(src: Path, dst: Path) -> None:
    """Move every file under ``src`` into ``dst``, never overwriting.

    A file already present under ``dst`` was written *after* the pid was
    minted, so it is the newer of the two and wins.
    """
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)


def _adopt_premint_runtime(dirname: str, target: Path) -> None:
    """Fold a pre-mint runtime home into the pid-keyed one, once.

    ``runtime_key`` falls back to the normalized folder name until the identity
    sink has minted a pid (T17, deliberately: wrong-but-contained beats
    failing). A writer that runs inside that window — a chat started in the
    second between ``scaffold_project`` and the watcher's next tick — therefore
    publishes into ``<runtime>/<folder name>/`` while every reader afterwards
    looks in ``<runtime>/<pid>/``. Nothing errors; the row is simply invisible
    forever.

    So the fallback is reconciled rather than merely tolerated. One rename
    where the pid-keyed home does not exist yet (the common case, and atomic),
    a no-overwrite merge where both do, and a logged skip on any failure —
    this is a repair, and a repair must never be able to fail a read.
    """
    premint = xo_runtime_root() / normalize_agent_id(dirname)
    marker = str(premint)
    if marker in _PREMINT_ADOPTED:
        return
    try:
        if not premint.is_dir():
            _remember_adopted(marker)
            return
        try:
            premint.rename(target)
        except OSError:
            # The pid-keyed home already exists (or the rename raced another
            # process doing the same thing). Merge instead.
            _merge_runtime_tree(premint, target)
            shutil.rmtree(premint, ignore_errors=True)
    except OSError:
        logger.warning(
            "project %s: could not adopt pre-mint runtime dir %s", dirname, premint
        )
        return
    logger.info("project %s: adopted pre-mint runtime state into %s", dirname, target)
    _remember_adopted(marker)


def runtime_dir_for_project(name: str, *, create: bool = False) -> Path | None:
    """``~/.quirq/projects/<key>/`` for a project, or ``None`` to skip.

    ``name`` may be any caller-supplied id: the folder resolution happens
    *here*. ``None`` means "there is nothing to resolve" — the project folder
    does not exist, or its pid is unusable as a path segment — and every caller
    treats that as an empty read or a skipped write, never as an error.

    Callers on the watcher tick must run ``project_json.fill_identity`` BEFORE
    calling this: the pid is minted there, and resolving first keys the runtime
    home by folder name for one tick and by pid forever after, splitting the
    project's state across two directories. Anything a *request* thread wrote
    during that same window is folded in by :func:`_adopt_premint_runtime`,
    which is why this is the only function that resolves a runtime home.
    """
    dirname = _project_dirname_if_present(name)
    if dirname is None:
        return None
    key = runtime_key(dirname)
    try:
        target = runtime_dir(key)
    except ValueError:
        # ``runtime_key`` sanitises, so this is only reachable through a
        # symlinked <root>/<key> pointing out of the runtime home. Fail closed
        # and quietly: a runtime read must never become a 500.
        logger.warning("project %s: runtime directory unresolvable", dirname)
        return None
    if key != normalize_agent_id(dirname):
        # A pid is in play, so a pre-mint home may exist alongside it.
        _adopt_premint_runtime(dirname, target)
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("project %s: could not create runtime dir %s", dirname, target)
            return None
    return target


def runtime_sessions_dir_for_project(name: str, *, create: bool = False) -> Path | None:
    """``~/.quirq/projects/<key>/sessions/``, or ``None`` to skip."""
    root = runtime_dir_for_project(name)
    if root is None:
        return None
    target = root / RUNTIME_SESSIONS_SUBDIR
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
    return target


def legacy_runtime_dir_for_project(name: str) -> Path | None:
    """The project's ``.xo/`` — where the moved files used to live.

    Read-only. Returns ``None`` when the project folder does not exist.
    """
    dirname = _project_dirname_if_present(name)
    if dirname is None:
        return None
    return xo_dir(dirname)


def runtime_read_roots(name: str) -> tuple[Path | None, Path | None]:
    """``(runtime root, pre-move root)`` for one project, resolved once.

    Both halves need the same folder resolution and the same "does this
    project exist" answer, so a caller that wants both — every reader of a
    moved file does — should ask for them together rather than resolve twice.
    ``(None, None)`` when the project folder is not there.
    """
    dirname = _project_dirname_if_present(name)
    if dirname is None:
        return None, None
    return runtime_dir_for_project(dirname), xo_dir(dirname)


def runtime_read_path(name: str, relative: str | Path) -> Path | None:
    """Where to READ one per-project runtime file from.

    The runtime copy when it exists, the pre-T19 in-project copy when only
    that does, and otherwise the runtime path (so a caller can report the
    location it would have read). ``None`` only when the project folder itself
    is absent.

    This is the read half of the migration: the first write lands in the
    runtime tier and from then on it is the only file that answers, so the
    copy happens on first write with no mover and no startup pass.
    """
    root, legacy_root = runtime_read_roots(name)
    if root is None:
        return None
    target = root / relative
    try:
        if target.exists():
            return target
    except OSError:
        return target
    if legacy_root is not None:
        legacy = legacy_root / relative
        try:
            if legacy.exists():
                return legacy
        except OSError:
            pass
    return target


# ── Per-project paths ─────────────────────────────────────────────────────────


def _is_safe_segment(value: str) -> bool:
    """True iff ``value`` is a single, non-hidden path component that can
    be joined onto the root without escaping it."""
    if not value or value in (".", ".."):
        return False
    if value.startswith("."):
        return False
    if "\x00" in value or "/" in value or "\\" in value:
        return False
    return Path(value).name == value


# Cache of the root's directory listing, keyed on the resolved root path.
# Value: ``(stamp, dirnames)`` where ``stamp`` is the root's stat signature
# taken *before* the walk. See :func:`_root_dirnames`.
_DIRNAMES_CACHE: dict[str, tuple[tuple, tuple[str, ...]]] = {}
_DIRNAMES_CACHE_MAX = 64


def _root_stamp(root: Path) -> tuple | None:
    """Cheap change signature for a directory: one ``stat``.

    docs/syncplan.md §10 (T23) specifies the key as ``(resolved root,
    st_mtime_ns, entry_count)``. ``entry_count`` cannot actually be read
    back without the very ``iterdir`` the cache exists to avoid, so this
    uses the strictly stronger signature the same single ``stat`` already
    returns: ``st_nlink`` *is* the kernel's subdirectory count on ext4/xfs
    (2 + subdirs), so a project appearing or disappearing changes the key
    even when the mtime second collides — which is the real hazard on
    1-second-granularity Docker bind mounts (gRPC-FUSE, virtiofs). On a
    filesystem that pins directory nlink to 1 the key degrades to
    mtime/ctime, which there have nanosecond granularity.
    """
    try:
        st = root.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_nlink, st.st_size)


def _root_dirnames(root: Path, *, force: bool = False) -> tuple[tuple[str, ...], bool]:
    """Return ``(sorted non-hidden dirnames, served_from_cache)``.

    The uncached form — ``sorted(root.iterdir())`` plus an ``is_dir()``
    stat per entry — ran ``5N + E`` times per tick inside
    :func:`resolve_project_dirname`, which is where the tick's ``5N²``
    stat term came from.
    """
    key = str(root)
    stamp = _root_stamp(root)
    if not force and stamp is not None:
        hit = _DIRNAMES_CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1], True

    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    names = tuple(
        e.name for e in entries if e.is_dir() and not e.name.startswith(".")
    )

    if stamp is not None:
        if len(_DIRNAMES_CACHE) >= _DIRNAMES_CACHE_MAX:
            _DIRNAMES_CACHE.clear()
        # Store the stamp taken *before* the walk: if the root changed
        # while we were listing it, the next call's stat differs and we
        # re-list, instead of caching a half-seen listing as current.
        _DIRNAMES_CACHE[key] = (stamp, names)
    return names, False


def _match_dirname(name: str, normalized: str, dirnames: tuple[str, ...]) -> str | None:
    """Cases 1-2 of :func:`resolve_project_dirname`, or ``None`` for
    "no directory in the root answers to this name"."""
    if _is_safe_segment(name) and name in dirnames:
        return name
    for dirname in dirnames:
        if normalize_agent_id(dirname) == normalized:
            return dirname
    return None


def resolve_project_dirname(name: str) -> str:
    """Map a caller-supplied project name onto the **actual** directory
    name under ``xo_projects_root()``.

    The directory name is the project id (see :func:`list_projects`), and
    discovery hands that literal name to every other helper here. But not
    every folder a user drops into the root is already in
    ``normalize_agent_id`` form: ``Agno-RAG-Tester`` normalises to
    ``agno-rag-tester``. Normalising unconditionally therefore pointed
    every write at a *different* path than the one discovery found, and
    the first write conjured an empty ghost folder holding nothing but
    ``.xo/`` next to the real project — which then registered as a second,
    empty project of its own.

    Resolution order:

    1. the literal name, when it is a safe segment exactly matching an
       existing dir
    2. an existing dir that normalises to the same id (the reverse lookup,
       for callers holding an already-normalised id)
    3. the normalised name — the canonical id for a project that does not
       exist yet (new-project creation keeps its old behaviour)

    Existence is checked against the directory *listing*, never with a
    bare ``is_dir()`` probe: on a case-insensitive filesystem
    (macOS/Windows) probing ``agno-rag-tester`` succeeds when only
    ``Agno-RAG-Tester`` exists, which would hand back an id that names no
    on-disk entry. Matching listed names keeps the returned id identical
    to what discovery reports on every platform.

    Cases 1-2 only ever return the name of a directory that exists in the
    root, and case 3 is sanitised, so the result is always a safe leaf:
    callers get the traversal defence ``normalize_agent_id`` gave them.

    The listing is cached per root on a one-``stat`` signature
    (:func:`_root_dirnames`). Case 3 — "no such directory" — is the one
    answer that is dangerous to serve from a cache, because it is what a
    freshly created project looks like to a stale listing, so reaching it
    from cached data forces one fresh walk and retries first.
    """
    root = xo_projects_root()
    normalized = normalize_agent_id(name)

    dirnames, from_cache = _root_dirnames(root)
    resolved = _match_dirname(name, normalized, dirnames)
    if resolved is None and from_cache:
        dirnames, _ = _root_dirnames(root, force=True)
        resolved = _match_dirname(name, normalized, dirnames)

    return resolved if resolved is not None else normalized


def project_dir(name: str) -> Path:
    return xo_projects_root() / resolve_project_dirname(name)


def xo_dir(name: str) -> Path:
    return project_dir(name) / ".xo"


def memory_dir(name: str) -> Path:
    return xo_dir(name) / "memory"


def state_dir(name: str) -> Path:
    return xo_dir(name) / "state"


def artifacts_dir(name: str) -> Path:
    return xo_dir(name) / "artifacts"


def skills_dir(name: str) -> Path:
    return xo_dir(name) / "skills"


def context_dir(name: str) -> Path:
    return xo_dir(name) / "context"


def project_metadata_path(name: str) -> Path:
    return xo_dir(name) / "project.json"


# ── Scaffold ──────────────────────────────────────────────────────────────────


def scaffold_project(
    name: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
) -> dict:
    """Create or fill in the canonical project tree from the template.

    Copies every file from the template directory (``~/ultimate-work`` or
    ``XO_PROJECT_TEMPLATE`` env var) into the project folder. Idempotent:
    existing files are never overwritten; missing files and directories are
    added.

    **Nothing runtime-tier is seeded here any more** (syncplan T19). The
    template used to ship ``stats.json``, ``timeline.jsonl``, ``sync.json``
    and ``sessions/``, and this function re-created
    ``sessions/sessionslist.json`` afterwards so the deletion would have been
    undone at every scaffold. Those files are machine-local now, they live
    outside the project tree, and the session index is partitioned across
    shard files — so there is no single document left to seed. Each writer
    creates its own shard on first use.

    Returns the project metadata dict (created or already present).
    """
    pid = resolve_project_dirname(name)
    pdir = project_dir(pid)
    xdir = xo_dir(pid)

    pdir.mkdir(parents=True, exist_ok=True)
    xdir.mkdir(parents=True, exist_ok=True)

    _copy_template(_template_dir(), pdir)

    return _upsert_metadata(pid, display_name=display_name, description=description)


def _upsert_metadata(
    pid: str,
    *,
    display_name: str | None,
    description: str | None,
) -> dict:
    """Read .xo/project.json, fill in any missing fields, optionally update
    display_name/description, write back, return the result.

    Ownership (docs/syncplan.md §5.1): this function owns ``display_name``
    and ``description``. It also *seeds* ``name`` and ``created_at`` —
    only while they are still empty, because it is what creates the file
    at scaffold time — but it never mints or overwrites ``pid``. That key
    belongs to the watcher's identity sink
    (``visualizer/sinks/project_json.fill_identity``), which generates it
    exactly once and never again, along with ``schema`` and
    ``owner_user_id``. ``git`` belongs to the git refresher. The write is a
    key-scoped merge through ``write_json_owned``, so those keys are
    carried through untouched rather than rebuilt — and the declared
    ownership set is the enforcement, not a comment.

    "Missing" means **absent or null**: the bundled template ships every
    identity key present-but-null, so an ``in``-only check left ``name``
    and ``created_at`` null on a freshly scaffolded project.
    """
    meta_path = project_metadata_path(pid)
    corrupt = False
    meta: dict = {}
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            corrupt = True
        else:
            if isinstance(loaded, dict):
                meta = loaded
            else:
                corrupt = True

    # ``values`` carries every key this call writes; ``owns`` is exactly
    # those plus any key being deleted. A key that is *not* listed is left
    # to its owner — which is why ``name``/``created_at`` appear only while
    # they still need seeding, and ``pid``/``schema``/``owner_user_id``/
    # ``git`` never appear at all.
    values: dict = {}

    if meta.get("name") is None:
        values["name"] = pid

    if meta.get("created_at") is None:
        # Same format the identity sink writes — one format per field.
        values["created_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    if display_name is not None:
        values["display_name"] = display_name
    elif meta.get("display_name") is None:
        values["display_name"] = pid
    else:
        # Re-declared at its current value rather than omitted: under
        # ``write_json_owned`` an owned key with no value is a *deletion*.
        values["display_name"] = meta["display_name"]

    if description is not None:
        values["description"] = description
    elif meta.get("description") is None:
        values["description"] = ""
    else:
        values["description"] = meta["description"]

    # Drop the template marker as soon as a real project is written. The
    # sink's guard is `not _template and pid`, so leaving it set made the
    # sink rebuild the document on every tick. Declared owned and omitted
    # from ``values``, which is how ``write_json_owned`` deletes a key.
    # Defence in depth: the sink drops it too.
    owns = set(values)
    if "_template" in meta:
        owns.add("_template")

    result = {k: v for k, v in meta.items() if k != "_template"}
    result.update(values)

    if corrupt:
        # Unparseable: there is no merge base, so there is nothing to carry
        # forward and ``write_json_owned`` would (correctly) refuse. Repair
        # the file, which is what this function has always done and loses
        # nothing recoverable — unlike ``fill_identity`` this function never
        # mints a ``pid``, so a repair cannot forge an identity.
        write_json_atomic(meta_path, result)
    else:
        try:
            # Atomic and key-scoped: the watcher thread writes this file
            # concurrently and there is no lock, so a bare write_text
            # exposed a torn read and a full-payload write dropped the
            # watcher's keys. ``volatile=()``: nothing here is a per-tick
            # timestamp, so every difference is a real change.
            write_json_owned(
                meta_path, owns=frozenset(owns), values=values, volatile=()
            )
        except CorruptDocumentError:
            # Readable a moment ago, not now — a concurrent truncation.
            write_json_atomic(meta_path, result)

    return result


# ── Read / list ───────────────────────────────────────────────────────────────


def load_project(name: str) -> dict | None:
    """Read .xo/project.json for an existing project, or None if absent."""
    path = project_metadata_path(resolve_project_dirname(name))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def project_exists(name: str) -> bool:
    """True iff the project folder has a .xo/project.json record."""
    return project_metadata_path(resolve_project_dirname(name)).exists()


def list_projects() -> list[dict]:
    """Filesystem-driven project list. Backend-agnostic.

    Returns one dict per directory under xo-projects that has
    ``.xo/project.json``. Hidden directories are skipped. Missing or
    malformed metadata yields a minimal entry with just ``name`` and
    ``path`` so the UI can still surface the folder.

    **The directory name is the project id.** Every other helper here
    resolves paths as ``xo_projects_root() / name``, so a stale
    ``name`` inside ``.xo/project.json`` (left behind when a folder is
    renamed) must never win: it would point consumers at the wrong
    folder — or at another project's — and two folders carrying the
    same stored name would collide into one id. ``project.json`` is
    therefore read for descriptive fields only.
    """
    root = xo_projects_root()
    out: list[dict] = []
    if not root.exists():
        return out

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        meta_path = entry / ".xo" / "project.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        stored_name = str(meta.get("name") or "")
        meta["name"] = entry.name
        display = str(meta.get("display_name") or "")
        # A display name that merely echoes a stale stored name is stale
        # too; only a deliberately different label survives the rename.
        if not display or (stored_name != entry.name and display == stored_name):
            display = entry.name
        meta["display_name"] = display
        meta["path"] = str(entry)
        out.append(meta)

    return out


def project_dir_exists(name: str) -> bool:
    """True iff the project root directory exists (scaffolded or not)."""
    return project_dir(name).is_dir()


def list_project_tree(name: str, relative_path: str = "") -> dict | None:
    """List dirs/files at a path inside a project.

    Returns ``None`` if the project doesn't exist or the resolved
    relative path doesn't point to an existing directory. Raises
    ``ValueError`` for invalid ``relative_path`` (contains ``..`` or
    ``.``, a leading separator, a null byte, or escapes the project
    root after resolve).

    The returned entries are raw — the BFF layer applies UI filtering
    (hidden entries, agent files at root). This helper only enforces
    path safety.
    """
    project_id = resolve_project_dirname(name)
    root = project_dir(project_id)
    if not root.is_dir():
        return None
    root_resolved = root.resolve()

    rel = (relative_path or "")
    if "\x00" in rel:
        raise ValueError("relative_path must not contain null bytes")
    if rel.startswith("/") or rel.startswith("\\"):
        raise ValueError("relative_path must not start with a path separator")
    rel = rel.replace("\\", "/").strip("/")
    if rel:
        parts = rel.split("/")
        if any(p in ("..", ".") or p == "" for p in parts):
            raise ValueError("relative_path must not contain '..' or '.' segments")

    target = (root_resolved / rel) if rel else root_resolved
    try:
        target = target.resolve()
        target.relative_to(root_resolved)
    except ValueError:
        raise ValueError("relative_path escapes project root") from None

    if not target.is_dir():
        return None

    dirs: list[dict] = []
    files: list[dict] = []
    for entry in sorted(target.iterdir()):
        entry_rel = str(entry.relative_to(root_resolved)).replace("\\", "/")
        info: dict = {"name": entry.name, "relative_path": entry_rel}
        # stat() is best-effort: a broken symlink or a race with a delete
        # must degrade to a listed entry with no detail, never a 500.
        try:
            st = entry.stat()
            info["modified_at"] = st.st_mtime
        except OSError:
            st = None
        if entry.is_dir():
            if st is not None:
                try:
                    # Cheap one-level count so the UI can say "12 items"
                    # without a second request per folder.
                    info["entries"] = sum(1 for _ in os.scandir(entry))
                except OSError:
                    pass
            dirs.append(info)
        else:
            if st is not None:
                info["size_bytes"] = st.st_size
            files.append(info)

    parent_rel: str | None
    if rel:
        head = "/".join(rel.split("/")[:-1])
        parent_rel = head
    else:
        parent_rel = None

    return {
        "project_id": project_id,
        "relative_path": rel,
        "parent_relative_path": parent_rel,
        "dirs": dirs,
        "files": files,
    }


def read_project_file(name: str, relative_path: str, *, max_bytes: int) -> dict | None:
    """Read one text file from inside a project, for preview.

    Returns ``None`` when the project or the file does not exist. Raises
    ``ValueError`` for an unsafe ``relative_path`` — the same rules as
    :func:`list_project_tree`, which is the only path validation in this
    module and must stay the only one.

    Reads at most ``max_bytes`` and reports ``truncated`` rather than
    streaming a 200 MB file into a browser. Decoding is non-strict: a preview
    of a file with one bad byte is more useful than an error.
    """
    project_id = resolve_project_dirname(name)
    root = project_dir(project_id)
    if not root.is_dir():
        return None
    root_resolved = root.resolve()

    rel = (relative_path or "")
    if not rel:
        raise ValueError("relative_path is required")
    if "\x00" in rel:
        raise ValueError("relative_path must not contain null bytes")
    if rel.startswith("/") or rel.startswith("\\"):
        raise ValueError("relative_path must not start with a path separator")
    rel = rel.replace("\\", "/").strip("/")
    parts = rel.split("/")
    if any(p in ("..", ".") or p == "" for p in parts):
        raise ValueError("relative_path must not contain '..' or '.' segments")

    target = root_resolved / rel
    try:
        target = target.resolve()
        target.relative_to(root_resolved)
    except ValueError:
        raise ValueError("relative_path escapes project root") from None

    # is_file() follows symlinks; the relative_to() check above already
    # rejected a link pointing out of the project, so this only excludes
    # directories and specials.
    if not target.is_file():
        return None

    size = target.stat().st_size
    with open(target, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]

    return {
        "project_id": project_id,
        "relative_path": rel,
        "name": target.name,
        "size_bytes": size,
        "modified_at": target.stat().st_mtime,
        "truncated": truncated,
        "content": raw.decode("utf-8", errors="replace"),
    }


def list_unscaffolded_dirs() -> list[dict]:
    """Directories under xo-projects/ that lack ``.xo/project.json``.

    Same baseline filtering as ``list_projects`` (non-hidden, is a
    directory), but returns only the entries that have NOT been
    scaffolded — useful when the UI wants to surface "complete this
    folder's setup" prompts.

    Each entry has ``name`` (the directory name) and ``mtime`` (POSIX
    timestamp of the directory; ``None`` if ``stat`` failed). ISO
    conversion and any further filtering (e.g. system-leaf names) is
    the BFF layer's job, not this helper's.
    """
    root = xo_projects_root()
    out: list[dict] = []
    if not root.exists():
        return out

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / ".xo" / "project.json").exists():
            continue
        try:
            mtime: float | None = entry.stat().st_mtime
        except OSError:
            mtime = None
        out.append({"name": entry.name, "mtime": mtime})

    return out
