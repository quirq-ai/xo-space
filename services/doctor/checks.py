"""The doctor's checks. Each takes a Context, returns findings, and writes nothing."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from services.cowork_agent import runtime_config
from services.cowork_agent.visualizer.state import watcher_heartbeat_path
from services.doctor import inventory
from services.doctor.context import Context
from services.doctor.model import FAIL, OK, WARN, Finding, ago, size
from services.doctor.reading import MAX_WALK_ENTRIES, ReadResult, measure_tree, readable_dir
from services.storage import layout
from services.timestamps import parse_ts
from utils import runtime_env

MAX_UNKNOWN_LISTED = 50

#: First path segment of every KEEP state pattern, to decide whether an
#: unlistable folder could be hiding a file whose loss is a FAIL (§7.1).
_KEEP_TOP_SEGMENTS = frozenset(
    spec.pattern.split("/", 1)[0]
    for spec in inventory.SPECS
    if spec.base == inventory.STATE and spec.klass == inventory.KEEP
)


def roots(ctx: Context) -> list[Finding]:
    if readable_dir(ctx.projects_root):
        return []
    return [Finding(
        "roots.projects_unavailable", FAIL, "projects root", ctx.display(ctx.projects_root),
        "The projects folder is missing or can't be read.",
        "No project's .xo/ can be checked. Check that the folder is mounted and that XO_PROJECTS_ROOT is right.",
    )]


#: Under this, a write of any size is likely to fail outright.
DISK_FAIL_BYTES = 50 * 1024 * 1024
#: Under this, the state root is close enough that someone should look.
DISK_WARN_BYTES = 500 * 1024 * 1024
#: Free inodes under this run out before the bytes do.
DISK_WARN_INODES = 10_000


def disk_space(ctx: Context) -> list[Finding]:
    """A full filesystem is the most ordinary cause of the empty and
    half-written files every other check reports."""
    try:
        info = os.statvfs(ctx.state_root)
    except OSError:
        return []  # a filesystem that won't answer is the roots check's business
    out: list[Finding] = []
    free = info.f_bavail * info.f_frsize
    if free < DISK_WARN_BYTES:
        level = FAIL if free < DISK_FAIL_BYTES else WARN
        out.append(Finding(
            "disk.low_space", level, "state root", ctx.display(ctx.state_root),
            f"The filesystem holding the state folder has {size(free)} free.",
            "Every store writes a whole temp file before replacing its target, so a write that runs "
            "out of room leaves the old file intact but records nothing new. Free some space."))
    # f_files == 0 means the filesystem does not report inodes at all.
    if info.f_files > 0 and info.f_favail < DISK_WARN_INODES:
        out.append(Finding(
            "disk.low_inodes", WARN, "state root", ctx.display(ctx.state_root),
            f"The filesystem holding the state folder has {info.f_favail:,} free inodes.",
            "Inodes run out before space does on a folder of many small files, and a store that "
            "cannot create its temp file records nothing new."))
    return out


def _read_finding(ctx: Context, path: Path, subject: str, spec: inventory.Spec, result: ReadResult) -> Finding:
    keep = spec.klass == inventory.KEEP
    level = FAIL if keep else WARN
    if result.outcome == "schema_unsupported":
        finding_id = "schema.unsupported"
        observed, why = {
            "newer": (f"Schema {result.schema} was written by a newer xo-space than this one.",
                      "This xo-space refuses or ignores the file. Update xo-space on this machine."),
            "older": (f"Schema {result.schema} is older than this xo-space reads.",
                      "The store that owns this file refuses it, so what it holds is not in use."),
        }.get(result.detail, ("The file has no schema number.",
                              "The store that owns this file can't tell which version it is, so it refuses it."))
    else:
        finding_id = "read." + result.outcome
        observed = {
            "unreadable": f"The file can't be read ({result.detail}).",
            "empty": "The file is empty.",
            "invalid_json": f"The file is not valid JSON ({result.detail}).",
            "wrong_type": f"The file holds a JSON {result.detail}, not an object.",
            "special": f"This is {result.detail}, not a file.",
            "file_too_large": f"The file is {result.detail}, too large to check.",
        }[result.outcome]
        if result.outcome == "unreadable":
            why = "This is not corruption. Check the file's permissions and the disk; until then nothing can use it."
        elif result.outcome == "special":
            why = ("Reading it could wait or run forever, so it was not opened, and the store that owns this "
                   "name can't use it either. Replace it with the real file.")
        elif result.outcome == "file_too_large":
            why = ("It was not read, so it was not checked. State files are small: something may be writing "
                   "to this one without limit.")
        elif keep:
            why = "The store that owns it can't use it, and what it records exists nowhere else."
        else:
            why = ("It is rebuilt from other files, so deleting it is safe: the server writes it again, most "
                   "within seconds, the GitHub issue mirror on its next poll, and xo.json when the server restarts.")
    return Finding(finding_id, level, subject, ctx.display(path), observed, why,
                   details={"class": spec.klass, "outcome": result.outcome})


def _judge(ctx: Context, out: list[Finding], path: Path, subject: str, spec: inventory.Spec) -> None:
    if spec.klass == inventory.UNPARSED:
        return
    result = ctx.read(path, spec)
    if result.outcome not in ("ok", "absent", "recent"):
        out.append(_read_finding(ctx, path, subject, spec, result))


def _listing_error(path: Path) -> str:
    try:
        with os.scandir(path):
            pass
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    return "unknown error"


def _unreadable_dir_finding(ctx: Context, path: Path, rel: str) -> Finding:
    top = rel.split("/", 1)[0]
    level = FAIL if top in _KEEP_TOP_SEGMENTS else WARN
    return Finding("read.unreadable", level, rel, ctx.display(path),
                   f"The folder can't be listed ({_listing_error(path)}).",
                   "This is not corruption. Check the folder's permissions; nothing inside it can be checked or used until then.")


def reads(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    unknown: list[tuple[str, Path]] = []
    files, truncated, unreadable_dirs = ctx.state_files()
    # Path.relative_to(...).as_posix() is one of the two hot spots on a large
    # state root (F7); a plain string slice does the same job on Linux, where
    # os.sep is already "/".
    prefix = os.path.join(str(ctx.state_root), "")
    # read.too_large and the unreadable-folder findings go first: run._cap
    # sorts stably by level, so on a badly broken state root with well over
    # MAX_FINDINGS_PER_CHECK per-file FAILs, these two are the ones that say
    # the report is incomplete and must never fall past the cap into the
    # rollup themselves.
    if truncated:
        out.append(Finding("read.too_large", FAIL, "state root", ctx.display(ctx.state_root),
                           f"The state folder has more than {MAX_WALK_ENTRIES:,} entries; the rest weren't checked.",
                           "Files whose loss cannot be recovered were not checked for corruption, so a healthy report here does not mean the state is healthy."))
    for path in sorted(unreadable_dirs):
        out.append(_unreadable_dir_finding(ctx, path, str(path)[len(prefix):]))
    for path in files:
        rel = str(path)[len(prefix):]
        spec = inventory.spec_for(inventory.STATE, rel)
        if spec is None:
            unknown.append((rel, path))
            continue
        _judge(ctx, out, path, rel, spec)
    for name in inventory.names(inventory.WORKSPACE):
        _judge(ctx, out, ctx.projects_root / ".xo" / name, f"<projects root>/.xo/{name}",
               inventory.spec_for(inventory.WORKSPACE, name))
    for project in ctx.projects():
        for name in inventory.names(inventory.PROJECT):
            _judge(ctx, out, project.xo / name, f"{project.name}/.xo/{name}",
                   inventory.spec_for(inventory.PROJECT, name))
    for rel, path in unknown[:MAX_UNKNOWN_LISTED]:
        out.append(Finding("inventory.unknown_file", OK, rel, ctx.display(path),
                           "A file this version of the doctor doesn't know.", "Listed for information only."))
    return out


#: space.json refreshes at most this often by default (space_json.py:197).
SPACE_REFRESH_S = 60


def _resolved(recorded: str) -> Path | None:
    """A root space.json records, resolved the way the server resolves its
    own, or None when it can't be: a NUL byte, an unknown ~user or a symlink
    loop. None never equals a current root, so it is reported as differing
    instead of turning the whole check into ERROR."""
    try:
        return Path(recorded).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def space_identity(ctx: Context) -> list[Finding]:
    """F1: space.json copies XO_SPACE_ID with no carry-forward (space_json.py:197)."""
    path = ctx.projects_root / ".xo" / "space.json"
    result = ctx.read(path, inventory.spec_for(inventory.WORKSPACE, "space.json"))
    if result.outcome != "ok":
        return []  # absent, or reported by reads()
    document = result.value
    shown = ctx.display(path)
    expected = (os.getenv("XO_SPACE_ID", "") or "").strip() or None
    stored = document.get("xo_space_id")
    out: list[Finding] = []
    if expected and stored is None:
        out.append(Finding("space.identity", FAIL, "space.json", shown,
                           "space.json has no xo_space_id, but XO_SPACE_ID is set.",
                           "Project sharing, usage reporting and Composio identify this Space by that id; the record no longer says which Space this is."))
    elif expected and stored != expected:
        out.append(Finding("space.identity", FAIL, "space.json", shown,
                           f"space.json names Space {stored}, but XO_SPACE_ID is {expected}.",
                           "The record describes a different Space than the one this server runs as."))
    elif not expected and stored:
        out.append(Finding("space.identity", WARN, "space.json", shown,
                           "XO_SPACE_ID is not set, but space.json has an xo_space_id.",
                           "The next write of space.json will erase the id. Set XO_SPACE_ID to keep it."))
    updated = parse_ts(document.get("updated_at"))
    stored_roots = document.get("roots") if isinstance(document.get("roots"), dict) else {}
    if updated is not None and ctx.now - updated.timestamp() >= SPACE_REFRESH_S:
        pairs = (("projects_root", ctx.projects_root), ("state_root", ctx.state_root))
        stale = [name for name, current in pairs
                 if isinstance(stored_roots.get(name), str)
                 and _resolved(stored_roots[name]) != current]
        if stale:
            out.append(Finding("space.identity", WARN, "space.json roots", shown,
                               f"space.json records different {' and '.join(stale)} than this server uses.",
                               "Anything that reads the roots from space.json looks in the wrong folder until the watcher rewrites it."))
    return out


def duplicate_ids(ctx: Context) -> list[Finding]:
    """F2: two folders with one pid share one runtime folder."""
    by_pid: dict[str, list[str]] = {}
    for project in ctx.projects():
        if project.pid:
            by_pid.setdefault(project.pid, []).append(project.name)
    return [
        Finding("projects.duplicate_id", WARN, pid, ctx.display(ctx.projects_root),
                f"Folders {', '.join(names)} have the same pid {pid}.",
                "They write to one runtime folder, so their stats, sessions and timelines merge. "
                "If one folder is a copy meant to be its own project, delete the \"pid\" line from that copy's "
                ".xo/project.json and the server gives it a new pid within seconds. Leave the original's pid alone.")
        for pid, names in sorted(by_pid.items()) if len(names) > 1
    ]


TMP_MIN_AGE_S = 300
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 10_000


def _is_temp_name(name: str) -> bool:
    return name.endswith(".tmp") or ".tmp." in name


#: Hidden files that are not a temp file anyone left behind. A state root
#: browsed from macOS or kept under git legitimately holds these, and calling
#: one an interrupted write tells a person to delete the wrong thing.
INERT_HIDDEN_NAMES = frozenset({".DS_Store", ".localized", ".gitkeep", ".gitignore", ".keep"})


def stale_temps(ctx: Context) -> list[Finding]:
    """F4. One agent-neutral rule instead of a list of writers (architecture §8.2).

    In the state root a hidden file also counts (mkstemp names such as
    ``.state-XXXX.json``), except for the names in INERT_HIDDEN_NAMES
    (``.DS_Store``, ``.localized``, ``.gitkeep``, ``.gitignore``, ``.keep``),
    which are legitimate there too. In a git-tracked ``.xo/`` a hidden file
    never counts, because names like ``.gitkeep`` are legitimate there.
    """
    skip_top = {".locks", layout.quarantine_dir().name}
    candidates: list[tuple[str, Path]] = []
    files, _, _ = ctx.state_files()
    prefix = os.path.join(str(ctx.state_root), "")
    for path in files:
        # Cheap name check first: spec_for (a pattern scan) only runs for the
        # small minority of files that look like a temp name at all (F7).
        if not (_is_temp_name(path.name)
                or (path.name.startswith(".") and path.name not in INERT_HIDDEN_NAMES)):
            continue
        rel = str(path)[len(prefix):]
        if rel.split("/", 1)[0] in skip_top or inventory.spec_for(inventory.STATE, rel) is not None:
            continue
        candidates.append((rel, path))
    xo_dirs = [("<projects root>/.xo", ctx.projects_root / ".xo")]
    xo_dirs += [(f"{project.name}/.xo", project.xo) for project in ctx.projects()]
    for label, xo in xo_dirs:
        try:
            entries = sorted(xo.iterdir())
        except OSError:
            continue
        for path in entries:
            if _is_temp_name(path.name) and path.is_file() and not path.is_symlink():
                candidates.append((f"{label}/{path.name}", path))
    out: list[Finding] = []
    for subject, path in sorted(candidates):
        try:
            age = ctx.now - path.lstat().st_mtime
        except OSError:
            continue
        if age >= TMP_MIN_AGE_S:
            out.append(Finding("tmp.stale", WARN, subject, ctx.display(path),
                               f"A temporary file left {ago(age)} ago.",
                               "A write was interrupted here. The file it belongs to kept its previous content, and this temporary file can be deleted."))
    return out


#: The inventory patterns whose contents are private. The doctor never opens
#: these files; it only reports when the filesystem lets others read them.
PRIVATE_PATTERNS: tuple[str, ...] = ("secrets/**", "settings/*.env")
#: Any group or other permission bit on a private file.
_TOO_OPEN = 0o077


def private_permissions(ctx: Context) -> list[Finding]:
    """Credentials and machine settings that other users on this machine can
    read or change. Nothing here opens a file (§12 invariant 6)."""
    out: list[Finding] = []
    files, _, _ = ctx.state_files()
    prefix = os.path.join(str(ctx.state_root), "")
    for path in files:
        rel = str(path)[len(prefix):]
        # Cheap prefix check first: spec_for (a pattern scan) only runs for
        # the small minority of files that could even be private (F7).
        if not rel.startswith(("secrets/", "settings/")):
            continue
        spec = inventory.spec_for(inventory.STATE, rel)
        if spec is None or spec.pattern not in PRIVATE_PATTERNS:
            continue
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
        except OSError:
            continue
        if mode & _TOO_OPEN:
            out.append(Finding(
                "perms.too_open", WARN, rel, ctx.display(path),
                f"The file's mode is {mode:04o}, so other users on this machine can read it.",
                "It holds credentials or machine settings. Nothing but this server needs it: chmod 600 the file. "
                "Some mounts (Windows or WSL bind mounts, some network filesystems) ignore chmod and show every "
                "file as open; there, keep the state folder on a filesystem that stores permissions.",
                details={"mode": f"{mode:04o}"}))
    return out


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _heartbeat_age(ctx: Context, path: Path, spec: inventory.Spec | None) -> float | None:
    value = ctx.read(path, spec).value
    stamp = parse_ts(value.get("last_tick_at")) if isinstance(value, dict) else None
    return None if stamp is None else max(0.0, ctx.now - stamp.timestamp())


def _watcher_enabled() -> bool:
    """``QUIRQ_WATCHER_ENABLED`` (default true), parsed the same way
    ``runtime_config.effective_settings`` does, but without resolving the
    active agent: that also happens inside ``effective_settings`` and has
    nothing to do with these two watcher env vars, so a broken agent setup
    must not turn this or the layout check into ERROR."""
    as_bool = getattr(runtime_config, "_as_bool", None)
    if as_bool is None:  # pragma: no cover - defensive fallback only
        def as_bool(value: str | None, *, default: bool) -> bool:
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return as_bool(os.getenv("QUIRQ_WATCHER_ENABLED"), default=True)


def _stale_after() -> float:
    # Borrowed inside the function: a rename upstream must turn this one check
    # into an ERROR result, not stop the server importing the doctor router.
    from services.cowork_agent.quirq_catalog import _stale_after_seconds  # the Quirq view's liveness rule, shared

    return _stale_after_seconds(runtime_env.watcher_tick_interval_seconds())


def layout_moves(ctx: Context) -> list[Finding]:
    """Files still at a path from before the state root had folders (layout.MOVES)."""
    old_heartbeat = next(
        (move.old() for move in layout.MOVES if move.new is not None and move.new() == watcher_heartbeat_path()),
        None,
    )
    age = _heartbeat_age(ctx, old_heartbeat, None) if old_heartbeat is not None else None
    old_server_fresh = age is not None and age <= _stale_after()
    if old_server_fresh:
        old_copy_why = "A server from an older xo-space still writes this old path. Update or stop that install before deleting anything here."
        not_migrated_why = "A server from an older xo-space is still running and writing the old layout. Update that install."
    else:
        old_copy_why = "Every reader ignores the old copy, but it looks like live data. Delete it once you've checked nothing in it is needed."
        not_migrated_why = "Restart the server once to move or clear these files."

    old_left: list[Finding] = []
    pending: list[str] = []
    for move in layout.MOVES:
        old = move.old()
        if old is None or not _exists(old):
            continue
        new = move.new() if move.new is not None else None
        if new is not None and _exists(new):
            old_left.append(Finding("layout.old_copy_left", WARN, move.what, ctx.display(old),
                                    f"An old copy of {move.what} is still at {ctx.display(old)}; the current one is {ctx.display(new)}.",
                                    old_copy_why))
        else:
            pending.append(move.what)
    out = list(old_left)
    if pending:
        out.append(Finding("layout.not_migrated", WARN, "state root", ctx.display(ctx.state_root),
                           f"{len(pending)} item(s) are still at their old paths: {', '.join(pending)}.", not_migrated_why))
    return out


def legacy_pending(ctx: Context) -> list[Finding]:
    """Pre-T19 runtime files still inside a project's .xo/ (visualizer/migrate.py)."""
    from services.cowork_agent.visualizer.migrate import _pending_sources  # pure: exists() and glob() only

    out: list[Finding] = []
    for project in ctx.projects():
        try:
            if project.xo.is_symlink() or not project.xo.is_dir():
                continue
            pending = _pending_sources(project.xo)
        except OSError:
            # A project that can't be reached (a folder this user can't enter,
            # a stale mount) is reported by runtime.keys_unknown; it must not
            # turn this whole check into ERROR.
            continue
        if pending:
            names = ", ".join(path.name for path in pending)
            out.append(Finding("legacy.pending", WARN, project.name, ctx.display(project.xo),
                               f"{len(pending)} runtime file(s) from before runtime data moved to the state folder: {names}.",
                               "They are no longer written. Restart the server once to move them into the state folder."))
    return out


def heartbeat(ctx: Context) -> list[Finding]:
    if not _watcher_enabled():
        return []
    path = watcher_heartbeat_path()
    age = _heartbeat_age(ctx, path, inventory.spec_for(inventory.STATE, "cache/heartbeat.json"))
    why = "Stats, timelines and the Inbox stop updating while the watcher isn't ticking."
    if age is None:
        return [Finding("watcher.heartbeat", WARN, "watcher", ctx.display(path),
                        "The watcher is enabled but there is no readable heartbeat yet.", why)]
    if age > _stale_after():
        return [Finding("watcher.heartbeat", WARN, "watcher", ctx.display(path),
                        f"The watcher is enabled but last ticked {ago(age)} ago.", why)]
    return []


def _count_entries(path: Path, stop: int) -> int:
    count = 0
    try:
        with os.scandir(path) as entries:
            for _ in entries:
                count += 1
                if count > stop:
                    break
    except OSError:
        return 0
    return count


def growth(ctx: Context) -> list[Finding]:
    """Things nothing trims (architecture §8.3). WARN only."""
    state = ctx.state_root
    out: list[Finding] = []
    files = [state / "projects" / "timeline.jsonl",
             *sorted((state / "scheduler" / "runs").glob("*.jsonl")),
             *sorted((state / "logs" / "scheduler").glob("*.log"))]
    for path in files:
        try:
            nbytes = path.stat().st_size
        except OSError:
            continue
        if nbytes > MAX_FILE_BYTES:
            out.append(Finding("growth.file_size", WARN, path.relative_to(state).as_posix(), ctx.display(path),
                               f"The file is {size(nbytes)}.", "Nothing rotates or trims this file; it only gets bigger."))
    quarantine = state / layout.quarantine_dir().name
    if quarantine.is_dir() and not quarantine.is_symlink():
        tree = measure_tree(quarantine)
        if tree.bytes > MAX_FILE_BYTES:
            out.append(Finding("growth.quarantine", WARN, "quarantine", ctx.display(quarantine),
                               f"Moved-aside data takes {size(tree.bytes)}.",
                               "It stays until you delete it by hand. Check nothing in it is needed, then delete the folders you don't want."))
    locks = state / ".locks"
    if _count_entries(locks, MAX_ENTRIES) > MAX_ENTRIES:
        out.append(Finding("growth.locks", WARN, ".locks", ctx.display(locks),
                           f"More than {MAX_ENTRIES:,} lock files.",
                           "Lock files are never removed. Each is tiny, but a huge folder slows every lock."))
    offsets = state / "projects" / "offsets.json"
    result = ctx.read(offsets, inventory.spec_for(inventory.STATE, "projects/offsets.json"))
    entries = result.value.get("offsets") if result.outcome == "ok" else None
    if isinstance(entries, dict) and len(entries) > MAX_ENTRIES:
        out.append(Finding("growth.offsets", WARN, "projects/offsets.json", ctx.display(offsets),
                           f"{len(entries):,} reading positions are stored.",
                           "Positions for deleted session files are never dropped, so the file only grows."))
    return out
