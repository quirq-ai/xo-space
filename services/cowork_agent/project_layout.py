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
        ├── project.json     {name, display_name, description, created_at}
        ├── memory/{semantic,episodic,procedural,working}/
        ├── sessions/        sessionslist.json (metadata only) + compressed/ + index.md
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
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from services.cowork_agent.helpers import normalize_agent_id

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


def xo_projects_root() -> Path:
    """User-facing projects directory.

    Sourced from ``XO_PROJECTS_ROOT`` env var; defaults to ``~/xo-projects``.
    Created on read so callers never have to guard for first-run.
    """
    raw = (os.getenv("XO_PROJECTS_ROOT", "") or "").strip() or "~/xo-projects"
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_xo_dir() -> Path:
    """Workspace-tier ``.xo/`` directory at ``~/xo-projects/.xo/``.

    Mirrors the per-project ``.xo/`` shape but aggregates across all
    projects. Materialised by the watcher's workspace tier (see
    docs/watcher-design.md §3.2). Does not auto-create on read — the
    watcher creates it explicitly on its first tick.
    """
    return xo_projects_root() / ".xo"


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
    """
    root = xo_projects_root()
    normalized = normalize_agent_id(name)

    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    dirnames = [
        e.name for e in entries if e.is_dir() and not e.name.startswith(".")
    ]

    if _is_safe_segment(name) and name in dirnames:
        return name

    for dirname in dirnames:
        if normalize_agent_id(dirname) == normalized:
            return dirname

    return normalized


def project_dir(name: str) -> Path:
    return xo_projects_root() / resolve_project_dirname(name)


def xo_dir(name: str) -> Path:
    return project_dir(name) / ".xo"


def sessions_dir(name: str) -> Path:
    return xo_dir(name) / "sessions"


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

    ``sessions/sessions.json`` is always ensured — it is a system requirement
    not present in the user template.

    Returns the project metadata dict (created or already present).
    """
    pid = resolve_project_dirname(name)
    pdir = project_dir(pid)
    xdir = xo_dir(pid)

    pdir.mkdir(parents=True, exist_ok=True)
    xdir.mkdir(parents=True, exist_ok=True)

    _copy_template(_template_dir(), pdir)

    # sessionslist.json is a system file the harness reads/writes; not in the template.
    # It holds session metadata only — messages stay in the provider's own storage.
    sessions_dir = xdir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sessions_json = sessions_dir / "sessionslist.json"
    if not sessions_json.exists():
        sessions_json.write_text("{}\n", encoding="utf-8")

    return _upsert_metadata(pid, display_name=display_name, description=description)


def _upsert_metadata(
    pid: str,
    *,
    display_name: str | None,
    description: str | None,
) -> dict:
    """Read .xo/project.json, fill in any missing fields, optionally update
    display_name/description, write back, return the result."""
    meta_path = project_metadata_path(pid)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
    else:
        meta = {}

    changed = False

    if "name" not in meta:
        meta["name"] = pid
        changed = True

    if "created_at" not in meta:
        meta["created_at"] = datetime.now(timezone.utc).isoformat()
        changed = True

    if display_name is not None:
        if meta.get("display_name") != display_name:
            meta["display_name"] = display_name
            changed = True
    elif "display_name" not in meta:
        meta["display_name"] = pid
        changed = True

    if description is not None:
        if meta.get("description") != description:
            meta["description"] = description
            changed = True
    elif "description" not in meta:
        meta["description"] = ""
        changed = True

    if changed:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    return dict(meta)


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
