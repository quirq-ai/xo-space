"""Space graph builder — maps ``~/xo-projects`` to the xo-atlas space.json shape.

Pure reader: scans the projects root and returns the graph document that
``v3.html`` consumes. Writes nothing.
Served by ``routers/space.py`` (GET /space/data/space.json).

Watcher-owner seam: ``materialize(path)`` writes the same output atomically
for event-driven freshness; nothing calls it in v1 — see
docs/space-module-design.md.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import (
    list_projects,
    project_dir,
    xo_projects_root,
)

# Muted category palette, >=3:1 contrast on the UI background #0b0c0f.
_PALETTE = [
    "#a2b56b", "#7fb3c8", "#c8a06b", "#b58a9e",
    "#8fbf9f", "#c4bd72", "#9a93d0", "#c88585",
]

_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c",
    ".cpp", ".h", ".sh", ".ps1", ".html", ".css", ".json", ".yml",
    ".yaml", ".toml", ".sql",
}
_DOC_EXT = {".md", ".txt", ".rst", ".pdf", ".docx"}

# Mirrors routers/cowork_agent/bff/filters.is_hidden_name — duplicated
# because services must not import from routers (dependency direction).
_TEMP_SUFFIXES = (".tmp", ".swp", ".swo", ".bak", ".orig")
_TEMP_PREFIXES = ("~$",)
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}

# Hard bounds — the API runs inside every user's workspace, so the builder
# must stay cheap regardless of how much is on disk. Each stage is capped.
MAX_LEAVES_PER_PROJECT = 400          # per-project output bound (newest-first)
MAX_TOTAL_LEAVES = 1500               # whole-graph output bound (browser must render it)
MIN_LEAVES_PER_PROJECT = 40           # workspace-cap floor — no project trimmed to zero
MAX_FILES_SCANNED_PER_PROJECT = 2000  # traversal bound (walk stops here)
BUILD_DEADLINE_S = 10.0               # whole-build wall-clock bound
MAX_LEAVES_PER_GROUP = 40             # bigger dirs split into per-subdir groups
_MAX_GROUP_SPLIT_DEPTH = 4            # deepest path segment the split recurses to
MAX_TIES = 60                         # cross-tie output bound

# Per-project git-history bounds (the timeline's "By project" mode). One
# entry per day with commits, so the payload grows with project age, not
# commit volume; both axes are capped anyway.
MAX_HISTORY_DAYS = 730                # newest day-entries kept per project
_HISTORY_SUBJECTS_PER_DAY = 3         # commit subjects sampled per day
_HISTORY_SUBJECT_CHARS = 80           # subject truncation

# Cross-tie derivation bounds. Ties are derived facts, never editorial:
# files that share commits, docs that name a file's path, test_x <-> x.
_TIE_MIN_COCHANGES = 3                # pairs must share >= this many commits
_TIE_MAX_FILES_PER_COMMIT = 20        # bulk commits are noise for co-change
_TIE_MAX_COMMITS = 500                # most recent commits considered
_TIE_MAX_DOCS_PER_PROJECT = 30        # docs scanned for path references
_DOC_SCAN_BYTES = 65536               # max bytes read per doc
_DOC_SCAN_EXT = {".md", ".rst", ".txt"}


def _is_hidden(name: str) -> bool:
    if not name or name.startswith("."):
        return True
    if name in _SKIP_DIRS:
        return True
    if any(name.endswith(s) for s in _TEMP_SUFFIXES):
        return True
    return any(name.startswith(p) for p in _TEMP_PREFIXES)


def _shape_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _CODE_EXT:
        return "disc"
    if ext in _DOC_EXT:
        return "ring"
    return "diamond"


def _iter_files_pruned(base: Path):
    """Yield files under ``base``, pruning hidden/junk dirs DURING traversal.

    ``os.walk`` with in-place ``dirnames`` filtering never *enters* a pruned
    directory — a project with a 100k-file node_modules costs nothing here.
    (``rglob`` + post-filter would enumerate all of it first: filtering after
    enumeration is O(everything on disk); pruning is O(what we keep).)"""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(n for n in dirnames if not _is_hidden(n))
        for name in sorted(filenames):
            if not _is_hidden(name):
                yield Path(dirpath) / name


_GIT_TIMEOUT_S = 5
_HEADER_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _git_facts(pdir: Path) -> tuple[
    dict[str, str], Optional[str], list[list[str]], list[tuple[str, str]]
]:
    """Per-file first-appearance date, the project's first-commit date, each
    commit's file list, and each commit's (date, subject), all oldest first,
    from one ``git log``. Any failure (not a repo, no git binary, no commits,
    timeout) → empty facts, and the project's leaves stay undated.

    The full history (no ``--diff-filter``) serves three consumers at once: a
    path's first appearance is its add date, the per-commit file lists feed
    co-change ties, and the (date, subject) stream feeds the timeline's
    per-project git history. ``%x01`` makes git emit a control byte prefix on
    each commit header line so file-path lines can never be confused with
    headers; ``%x02`` splits date from subject inside the header.

    Subjects are attacker-ish input (any byte but NUL/newline): the stream is
    framed on ``\\n`` only (``splitlines()`` also splits on VT/FF/NEL/U+2028,
    letting a subject fabricate lines), every header date must be an ISO day
    before it is trusted, remaining control bytes are stripped from stored
    subjects, and the decode is non-strict so one legacy non-UTF-8 subject
    cannot wipe the whole project's facts."""
    try:
        # A project's facts must come from its own repo. Without this gate a
        # plain folder nested in some enclosing repo (a dotfiles $HOME, the
        # projects root itself) inherits the parent's entire history, and
        # the timeline would render that foreign log as the project's lane.
        if not (pdir / ".git").exists():
            return {}, None, [], []
        out = subprocess.run(
            [
                "git", "-C", str(pdir), "log",
                "--reverse", "--date=short",
                "--pretty=format:%x01%ad%x02%s", "--name-only",
            ],
            capture_output=True, text=True, errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return {}, None, [], []
    if out.returncode != 0:
        return {}, None, [], []

    created: dict[str, str] = {}
    first_commit: Optional[str] = None
    current: Optional[str] = None
    commits: list[list[str]] = []
    history: list[tuple[str, str]] = []
    files: list[str] = []
    for line in out.stdout.split("\n"):
        if line.startswith("\x01"):
            date_part, _, subject = line[1:].partition("\x02")
            date_part = date_part.strip()
            if not _HEADER_DATE_RE.match(date_part):
                continue  # fabricated header: a control byte inside a subject
            if current is not None:
                commits.append(files)
            current = date_part
            files = []
            history.append((current, _CTRL_RE.sub(" ", subject).strip()))
            if first_commit is None:
                first_commit = current
        elif line.strip() and current:
            rel = line.strip()
            # oldest-first (--reverse): setdefault keeps the first
            # appearance, surviving later delete/re-add churn.
            created.setdefault(rel, current)
            files.append(rel)
    if current is not None:
        commits.append(files)
    return created, first_commit, commits, history


def _aggregate_history(history: list[tuple[str, str]]) -> list[dict]:
    """Collapse an oldest-first (date, subject) commit stream into per-day
    entries ``{"d", "n", "s"}`` (day, commit count, sampled subjects),
    chronological, capped to the newest MAX_HISTORY_DAYS days."""
    days: dict[str, dict] = {}
    for day, subject in history:
        entry = days.setdefault(day, {"d": day, "n": 0, "s": []})
        entry["n"] += 1
        if subject and len(entry["s"]) < _HISTORY_SUBJECTS_PER_DAY:
            entry["s"].append(subject[:_HISTORY_SUBJECT_CHARS])
    out = [days[key] for key in sorted(days)]
    if len(out) > MAX_HISTORY_DAYS:
        out = out[-MAX_HISTORY_DAYS:]
    return out


def _walk_project(pid: str, cat: str, created_dates: dict) -> tuple[list[dict], list[dict]]:
    """Groups + leaves for one project. Level-1 dirs become groups; files at
    any depth roll up into their level-1 group; root files get a root group.
    Traversal is pruned and stops at MAX_FILES_SCANNED_PER_PROJECT.
    Raises OSError if the project directory is unreadable (caller skips)."""
    pdir = project_dir(pid)
    groups: list[dict] = []
    leaves: list[dict] = []
    scanned = 0

    def add_leaf(group_id: str, rel: str, f: Path) -> None:
        # Dates come from git only: a file's first-appearance commit day.
        # Files git does not know (and whole non-git projects) get null and
        # sit out the timeline; an mtime would stamp them all "touched
        # recently" and pile every undated artifact onto today.
        leaves.append({
            "id": f"{pid}:{rel}",
            "group": group_id,
            "shape": _shape_for(f.name),
            "tag": (f.suffix.lstrip(".").upper() or "FILE"),
            "label": f.name,
            "date": created_dates.get(rel),
            "blurb": rel,
            "path": f"{pid}/{rel}",
        })

    entries = sorted(pdir.iterdir(), key=lambda e: e.name)
    root_files = [e for e in entries if e.is_file() and not _is_hidden(e.name)]
    subdirs = [e for e in entries if e.is_dir() and not _is_hidden(e.name)]

    if root_files:
        groups.append({
            "id": f"g_{pid}_root", "cat": cat,
            "label": "(root)", "blurb": "Files at the project root.",
        })
        for f in root_files:
            if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
                break
            add_leaf(f"g_{pid}_root", f.name, f)
            scanned += 1

    for d in subdirs:
        if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
            break
        collected: list[Path] = []
        for f in _iter_files_pruned(d):
            if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
                print(f"space_index: {pid}: scan budget hit "
                      f"({MAX_FILES_SCANNED_PER_PROJECT}); rest of project skipped")
                break
            collected.append(f)
            scanned += 1
        if not collected:
            continue

        # A dir over the group cap splits into one group per subdir,
        # recursing (bounded) while a bucket still exceeds the cap —
        # balanced clusters render as constellations instead of one dense
        # ball, and keep the force sim far from its stiffness limit. Files
        # sitting directly at a split level stay in that level's group.
        def emit_group(files: list[Path], depth: int, gid: str, label: str) -> None:
            if len(files) > MAX_LEAVES_PER_GROUP and depth < _MAX_GROUP_SPLIT_DEPTH:
                buckets: dict = {}
                for f in files:
                    parts = f.relative_to(pdir).parts
                    seg = parts[depth] if len(parts) > depth + 1 else None
                    buckets.setdefault(seg, []).append(f)
                if set(buckets) != {None}:
                    for seg in sorted(buckets, key=lambda s: (s is not None, s or "")):
                        if seg is None:
                            emit_final(buckets[None], gid, label)
                        else:
                            emit_group(buckets[seg], depth + 1,
                                       f"{gid}__{seg}", f"{label} · {seg}")
                    return
            emit_final(files, gid, label)

        def emit_final(files: list[Path], gid: str, label: str) -> None:
            for f in files:
                add_leaf(gid, f.relative_to(pdir).as_posix(), f)
            groups.append({
                "id": gid, "cat": cat,
                "label": label, "blurb": f"{len(files)} files",
            })

        emit_group(collected, 1, f"g_{pid}_{d.name}", d.name)

    if len(leaves) > MAX_LEAVES_PER_PROJECT:
        dropped = len(leaves) - MAX_LEAVES_PER_PROJECT
        # Undated (non-git) leaves sort as oldest, so they are trimmed first.
        leaves.sort(key=lambda leaf: leaf["date"] or "", reverse=True)
        leaves = leaves[:MAX_LEAVES_PER_PROJECT]
        print(f"space_index: {pid}: dropped {dropped} oldest leaves (cap {MAX_LEAVES_PER_PROJECT})")

    return groups, leaves


def _newest_first(leaves: list[dict]) -> list[dict]:
    """Newest git first-appearance date first; undated leaves last."""
    return sorted(leaves, key=lambda leaf: leaf["date"] or "", reverse=True)


def _trim_total(
    leaves: list[dict],
    cap: int = MAX_TOTAL_LEAVES,
    floor: int = MIN_LEAVES_PER_PROJECT,
) -> list[dict]:
    """Bound the whole graph at ``cap`` without trimming any project to zero.

    The old rule kept the newest ``cap`` leaves workspace-wide by git
    first-appearance date. Undated leaves — every file of a non-git project —
    sort as older than everything, so a few big repos filled the budget and
    each non-git project lost *all* of its files; the Files list then said
    "no files yet" for folders that plainly have files (#36).

    Now every project is first guaranteed a floor — its own count, or
    ``max(floor, cap // n_projects)``, whichever is smaller — and the
    remaining budget is filled newest-first from what is left, so the big
    repos absorb the cuts. When even the floors alone overflow the cap
    (hundreds of projects), the floor shrinks to ``max(1, cap // n)``:
    the cap may then be exceeded slightly, but a project with files on
    disk never disappears from the graph.
    """
    if len(leaves) <= cap:
        return leaves

    # "path" is "<pid>/<rel>" and a pid is one path segment, so the split is
    # unambiguous ("id" is "<pid>:<rel>", but a folder name may contain ":").
    by_project: dict[str, list[dict]] = {}
    for leaf in leaves:
        by_project.setdefault(leaf["path"].split("/", 1)[0], []).append(leaf)

    n = len(by_project)
    per_project_floor = max(floor, cap // n)
    if sum(min(len(bucket), per_project_floor) for bucket in by_project.values()) > cap:
        per_project_floor = max(1, cap // n)

    kept: list[dict] = []
    overflow: list[dict] = []
    for bucket in by_project.values():
        bucket = _newest_first(bucket)
        kept.extend(bucket[:per_project_floor])
        overflow.extend(bucket[per_project_floor:])

    remaining = cap - len(kept)
    if remaining > 0:
        kept.extend(_newest_first(overflow)[:remaining])

    dropped = len(leaves) - len(kept)
    print(f"space_index: dropped {dropped} leaves workspace-wide "
          f"(cap {cap}, per-project floor {per_project_floor}); "
          f"empty groups pruned")
    return _newest_first(kept)


def _build_ties(leaves: list[dict], commits_by_pid: dict[str, list[list[str]]]) -> list[dict]:
    """Derived cross-ties between kept leaves, strongest first, capped.

    Three honest derivations (no editorial content): files that repeatedly
    share commits (git co-change), docs whose text names another file's
    relative path, and test_x <-> x name pairing. Runs after every leaf cap
    so a tie can never reference a dropped node — v3 crashes on unknown
    edge endpoints."""
    rel_to_id: dict[str, dict[str, str]] = {}
    for leaf in leaves:
        pid, rel = leaf["id"].split(":", 1)
        rel_to_id.setdefault(pid, {})[rel] = leaf["id"]

    seen: set = set()
    cands: list[tuple[int, str, str, str]] = []  # (strength, s, t, label)

    def add(strength: int, s: str, t: str, label: str) -> None:
        key = tuple(sorted((s, t)))
        if key not in seen:
            seen.add(key)
            cands.append((strength, s, t, label))

    # 1. git co-change: pairs sharing >= _TIE_MIN_COCHANGES recent commits.
    for pid, commits in commits_by_pid.items():
        rels = rel_to_id.get(pid)
        if not rels:
            continue
        counts: dict = {}
        for files in commits[-_TIE_MAX_COMMITS:]:
            if len(files) > _TIE_MAX_FILES_PER_COMMIT:
                continue
            kept = sorted({f for f in files if f in rels})
            for i in range(len(kept)):
                for j in range(i + 1, len(kept)):
                    pair = (kept[i], kept[j])
                    counts[pair] = counts.get(pair, 0) + 1
        for (a, b), n in counts.items():
            if n >= _TIE_MIN_COCHANGES:
                add(n, rels[a], rels[b], f"changed together ×{n}")

    # 2. docs referencing a file's relative path.
    for pid, rels in rel_to_id.items():
        scanned_docs = 0
        for rel, lid in rels.items():
            if Path(rel).suffix.lower() not in _DOC_SCAN_EXT:
                continue
            if scanned_docs >= _TIE_MAX_DOCS_PER_PROJECT:
                break
            scanned_docs += 1
            try:
                with open(project_dir(pid) / rel, encoding="utf-8", errors="ignore") as fp:
                    text = fp.read(_DOC_SCAN_BYTES)
            except OSError:
                continue
            for other_rel, oid in rels.items():
                if oid != lid and other_rel in text:
                    add(2, lid, oid, "references")

    # 3. test_x <-> x pairing by filename.
    for pid, rels in rel_to_id.items():
        by_name: dict = {}
        for rel, lid in rels.items():
            by_name.setdefault(Path(rel).name, []).append(lid)
        for rel, lid in rels.items():
            name = Path(rel).name
            if name.startswith("test_"):
                for oid in by_name.get(name[5:], []):
                    add(2, lid, oid, "tests")

    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    if len(cands) > MAX_TIES:
        print(f"space_index: kept strongest {MAX_TIES} of {len(cands)} derived ties")
    return [{"s": s, "t": t, "label": label} for _, s, t, label in cands[:MAX_TIES]]


def build_space_data() -> dict:
    root = xo_projects_root()
    projects = list_projects()

    categories: dict = {}
    hub_angles: dict = {}
    hubs: list[dict] = []
    groups: list[dict] = []
    leaves: list[dict] = []
    milestones: list[dict] = []
    commits_by_pid: dict[str, list[list[str]]] = {}
    git_history: dict[str, list[dict]] = {}

    n = max(len(projects), 1)
    deadline = time.monotonic() + BUILD_DEADLINE_S
    for i, meta in enumerate(projects):
        if time.monotonic() > deadline:
            print(f"space_index: build deadline ({BUILD_DEADLINE_S}s) hit; "
                  f"skipped {len(projects) - i} of {len(projects)} projects")
            break
        pid = str(meta["name"])
        cat = f"p_{pid}"
        display = str(meta.get("display_name") or pid)
        created_dates, first_commit, p_commits, p_history = _git_facts(
            project_dir(pid))

        try:
            p_groups, p_leaves = _walk_project(pid, cat, created_dates)
        except OSError:
            print(f"space_index: skipping unreadable project {pid}")
            continue

        categories[cat] = {
            "name": display,
            "color": _PALETTE[i % len(_PALETTE)],
        }
        hub_angles[cat] = -math.pi / 2 + i * 2 * math.pi / n
        hubs.append({
            "id": cat, "cat": cat, "label": display,
            "blurb": str(meta.get("description") or f"Project {display}."),
        })
        groups.extend(p_groups)
        leaves.extend(p_leaves)
        commits_by_pid[pid] = p_commits
        if p_history:
            git_history[cat] = _aggregate_history(p_history)
        if first_commit:
            milestones.append({"d": first_commit, "t": f"{display} first commit"})

    if len(leaves) > MAX_TOTAL_LEAVES:
        leaves = _trim_total(leaves)
        kept_groups = {leaf["group"] for leaf in leaves}
        groups = [g for g in groups if g["id"] in kept_groups]

    ties = _build_ties(leaves, commits_by_pid)

    today = date.today()
    # The axis must hold both modes: leaf dates (By file) and commit days
    # (By project); a commit can predate every kept leaf. Undated leaves
    # (files git does not know) do not stretch the axis.
    dates = sorted(
        [leaf["date"] for leaf in leaves if leaf["date"]]
        + [day["d"] for hist in git_history.values() for day in hist]
    )
    if dates:
        start = (date.fromisoformat(dates[0]) - timedelta(days=7)).isoformat()
        end = (date.fromisoformat(dates[-1]) + timedelta(days=7)).isoformat()
    else:
        start = (today - timedelta(days=7)).isoformat()
        end = (today + timedelta(days=7)).isoformat()

    return {
        "meta": {
            "title": "Space",
            "tagline": "an xo-projects knowledge graph",
            "mappedOn": today.strftime("%d %B %Y"),
            "workspace": str(root),
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": start, "end": end},
        "root": {
            "id": "xo",
            "label": "xo-projects",
            "blurb": f"{len(projects)} projects under {root}",
        },
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": ties,
        "milestones": milestones,
        "gitHistory": git_history,
    }


def materialize(path: Path) -> None:
    """Atomically write ``build_space_data()`` output to ``path``.

    NOT called anywhere in v1. Integration seam for the watcher owner:
    call from the workspace re-aggregate step in ``watcher.tick()`` for
    event-driven freshness, then point the route at the file. See
    docs/space-module-design.md."""
    from services.cowork_agent.visualizer.atomic_write import write_json_atomic

    write_json_atomic(path, build_space_data())
