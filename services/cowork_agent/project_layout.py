"""Canonical project layout for ~/xo-projects/<name>/."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

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
# everything the resolution depends on, so an env change is a miss, not a stale
# hit.
_ROOT_RESOLUTION_CACHE: dict[tuple[str, str, str], Path] = {}
_ROOT_RESOLUTION_CACHE_MAX = 64


def _resolved_root(raw: str) -> Path:
    """``Path(raw).expanduser().resolve()``, with the realpath walk memoized."""
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return expanded.resolve()
    key = (raw, os.environ.get("HOME", ""), os.environ.get("USERPROFILE", ""))
    hit = _ROOT_RESOLUTION_CACHE.get(key)
    if hit is not None:
        return hit
    resolved = expanded.resolve()
    # Bounded: a long-lived process only ever sees one or two roots; a test
    # suite churns through temp dirs.
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
    # Create-on-read is a contract other callers depend on, so the check stays
    # on every call — but it is now a single stat instead of a mkdir that fails
    # with EEXIST *plus* the is_dir() stat pathlib does to decide whether
    # EEXIST was acceptable.
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_xo_dir() -> Path:
    """Workspace-tier ``.xo/`` directory at ``~/xo-projects/.xo/``."""
    return xo_projects_root() / ".xo"


def workspace_runtime_dir() -> Path:
    """``~/.quirq/workspace/`` — the derived workspace views (syncplan T20)."""
    return quirq_state_dir() / "workspace"


def workspace_sessions_dir() -> Path:
    """``~/.quirq/workspace/sessions/`` — the workspace-tier session views."""
    return workspace_runtime_dir() / "sessions"


# ── Runtime home (machine-local; never synced) ─────────────────────────────────
# Machine-local telemetry lives OUTSIDE every project tree, in the Quirq state
# home keyed by ``project.json:pid`` (docs/syncplan.md §4).


def xo_runtime_root() -> Path:
    """Per-project runtime home, ``~/.quirq/projects/`` by default."""
    return _resolved_root(str(quirq_state_dir())) / "projects"


# The runtime key is a single path segment joined straight into the runtime
# home, and it comes from ``project.json`` — which is the SYNCED tier.

_SAFE_RUNTIME_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _truncate(value: str, limit: int = 64) -> str:
    """Shorten an untrusted value for log/error output (it may be huge)."""
    return value if len(value) <= limit else value[:limit] + "...(truncated)"


def _is_safe_runtime_key(value: str) -> bool:
    """True iff ``value`` is safe to use as a single runtime path segment."""
    if not value:
        return False
    if any(bad in value for bad in ("/", "\\", "\x00")):
        return False
    if "." in value:  # covers "." and ".." as well as any dotted segment
        return False
    return bool(_SAFE_RUNTIME_KEY.fullmatch(value))


def runtime_dir(pid: str) -> Path:
    """Per-project runtime directory ``~/.quirq/projects/<pid>/`` (pid-keyed)."""
    root = xo_runtime_root()
    key = str(pid)
    if not _is_safe_runtime_key(key):
        raise ValueError(
            f"unsafe runtime key {_truncate(key)!r}: expected a single "
            "[A-Za-z0-9_-]{1,64} path segment (resolve untrusted pids via runtime_key)"
        )
    target = (root / key).resolve()
    # Belt and braces.
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
    """Resolve a project folder name to its runtime-store key."""
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
# ``stats.json``, ``timeline.jsonl``, ``sync.json`` and everything under
# ``sessions/`` moved out of ``<project>/.xo/`` and into
# ``~/.quirq/projects/<key>/`` (syncplan §9, T19).

# Sub-paths *below* a project's runtime directory.
RUNTIME_SESSIONS_SUBDIR = Path("sessions")

# The partitioned session index (syncplan T19, inherited from T4).
RUNTIME_SESSION_SHARDS_SUBDIR = RUNTIME_SESSIONS_SUBDIR / "sessionslist.d"

# Pre-T19 home of the moved files, still read (never written) so a project that
# predates the move keeps serving its history.
LEGACY_SESSIONS_SUBDIR = Path("sessions")


def _project_dirname_if_present(name: str) -> str | None:
    """Resolved directory name for ``name``, or ``None`` if no such folder."""
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
_PREMINT_ADOPTED: set[str] = set()
_PREMINT_ADOPTED_MAX = 256


def _remember_adopted(marker: str) -> None:
    if len(_PREMINT_ADOPTED) >= _PREMINT_ADOPTED_MAX:
        _PREMINT_ADOPTED.clear()
    _PREMINT_ADOPTED.add(marker)


def _merge_runtime_tree(src: Path, dst: Path) -> None:
    """Move every file under ``src`` into ``dst``, never overwriting."""
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)


def _adopt_premint_runtime(dirname: str, target: Path) -> None:
    """Fold a pre-mint runtime home into the pid-keyed one, once."""
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
    """``~/.quirq/projects/<key>/`` for a project, or ``None`` to skip."""
    dirname = _project_dirname_if_present(name)
    if dirname is None:
        return None
    key = runtime_key(dirname)
    try:
        target = runtime_dir(key)
    except ValueError:
        # ``runtime_key`` sanitises, so this is only reachable through a
        # symlinked <root>/<key> pointing out of the runtime home.
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


def runtime_read_roots(name: str) -> tuple[Path | None, Path | None]:
    """``(runtime root, pre-move root)`` for one project, resolved once."""
    dirname = _project_dirname_if_present(name)
    if dirname is None:
        return None, None
    return runtime_dir_for_project(dirname), xo_dir(dirname)


def runtime_read_path(name: str, relative: str | Path) -> Path | None:
    """Where to READ one per-project runtime file from."""
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
_DIRNAMES_CACHE: dict[str, tuple[tuple, tuple[str, ...]]] = {}
_DIRNAMES_CACHE_MAX = 64


def _root_stamp(root: Path) -> tuple | None:
    """Cheap change signature for a directory: one ``stat``."""
    try:
        st = root.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_nlink, st.st_size)


def _root_dirnames(root: Path, *, force: bool = False) -> tuple[tuple[str, ...], bool]:
    """Return ``(sorted non-hidden dirnames, served_from_cache)``."""
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
        # Store the stamp taken *before* the walk: if the root changed while we
        # were listing it, the next call's stat differs and we re-list, instead
        # of caching a half-seen listing as current.
        _DIRNAMES_CACHE[key] = (stamp, names)
    return names, False


def _match_dirname(name: str, normalized: str, dirnames: tuple[str, ...]) -> str | None:
    """
    Cases 1-2 of :func:`resolve_project_dirname`, or ``None`` for "no directory
    in the root answers to this name".
    """
    if _is_safe_segment(name) and name in dirnames:
        return name
    for dirname in dirnames:
        if normalize_agent_id(dirname) == normalized:
            return dirname
    return None


def resolve_project_dirname(name: str) -> str:
    """
    Map a caller-supplied project name onto the **actual** directory name under
    ``xo_projects_root()``.
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
    """Create or fill in the canonical project tree from the template."""
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
    """
    Read .xo/project.json, fill in any missing fields, optionally update
    display_name/description, write back, return the result.
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

    # ``values`` carries every key this call writes; ``owns`` is exactly those
    # plus any key being deleted.
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

    # Drop the template marker as soon as a real project is written.
    owns = set(values)
    if "_template" in meta:
        owns.add("_template")

    result = {k: v for k, v in meta.items() if k != "_template"}
    result.update(values)

    if corrupt:
        # Unparseable: there is no merge base, so there is nothing to carry
        # forward and ``write_json_owned`` would (correctly) refuse.
        write_json_atomic(meta_path, result)
    else:
        try:
            # Atomic and key-scoped: the watcher thread writes this file
            # concurrently and there is no lock, so a bare write_text exposed a
            # torn read and a full-payload write dropped the watcher's keys.
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


def git_repo_dirs() -> list[Path]:
    """Immediate subdirs of xo-projects that are git repos (have a ``.git`` dir).

    Same visibility rules as ``list_projects``: hidden names are skipped and the
    directory name is the project id. Used by the commit relay; callers decide
    what to do when a repo appears more than once.
    """
    root = xo_projects_root()
    out: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.name.startswith(".") and (entry / ".git").is_dir():
            out.append(entry)
    return out


def relative_path_suffix(relative_path: str) -> str:
    """Return a project-relative path's final suffix without filesystem access."""
    return PurePosixPath(relative_path).suffix


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
