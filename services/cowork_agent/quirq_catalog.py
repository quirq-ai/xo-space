"""Read-only, privacy-aware catalog of machine-local Quirq state.

The catalog powers the local Quirq view, opened from the Setup tab's header
(deep link ``#/quirq``). It deliberately reports structure and
operational summaries rather than serving arbitrary files: credential values,
native session contents, cursor paths, and symlink targets never leave the
machine-local state service.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.cowork_agent.local_state import quirq_state_dir
from services.cowork_agent.project_layout import (
    runtime_dir_for_project,
    workspace_runtime_dir,
    xo_projects_root,
)
from services.cowork_agent.registry.agent_env import load_env_entries
from services.cowork_agent.runtime_config import (
    configured_settings,
    effective_settings,
    root_settings,
)


# Liveness thresholds for the watcher heartbeat (see _stale_after_seconds).
_MISSED_TICKS_BEFORE_DEAD = 5.0
_HEARTBEAT_STALE_FLOOR_S = 5.0

_MAX_FILES = 500
_MAX_JSON_BYTES = 2 * 1024 * 1024
_SENSITIVE_NAMES = frozenset({"secrets.env"})

# ``tier`` says which root a per-project file hangs off. It is not decoration:
# syncplan T19 moved four of the six out of ``<project>/.xo/`` and into
# ``~/.quirq/projects/<key>/``, and a catalog that kept looking in the old
# place would render "0 present" for each of them with no error at all — the
# exact silent-empty failure the move is full of. The ``location`` field each
# row publishes is built from the tier, so the UI shows where a file really is.
_TIER_SYNCED = "synced"
_TIER_RUNTIME = "runtime"

_PROJECT_OUTPUT_CONTRACT = (
    {
        "path": "project.json",
        "tier": _TIER_SYNCED,
        "producer": "Watcher identity sink + project scaffold",
        "purpose": "Stable project id, display name, description, and creation time",
        "used_by": "Projects, Graph",
    },
    {
        "path": "sessions/sessionslist.d",
        "tier": _TIER_RUNTIME,
        "producer": "Runtime source adapter",
        "purpose": "Metadata-only index that maps native sessions to this project, one shard file per session",
        "used_by": "Projects APIs, Graph",
    },
    {
        "path": "sessions/sessions-augment.json",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher session sink",
        "purpose": "Derived message, tool, task, model, timing, and usage summaries",
        "used_by": "Project APIs, Graph",
    },
    {
        "path": "todos.json",
        "tier": _TIER_SYNCED,
        "producer": "Todo API (every runtime; there is no watcher todo sink)",
        "purpose": "Per-session work items, their lifecycle state, and deletion tombstones",
        "used_by": "Projects",
    },
    {
        "path": "workitems.json",
        "tier": _TIER_SYNCED,
        "producer": "Workitems API (the routes are the file's only writer)",
        "purpose": (
            "Authored work items and GitHub adoption records, with deletion "
            "tombstones"
        ),
        "used_by": "Workitems",
    },
    {
        "path": "stats.json",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher statistics sink",
        "purpose": "Rolling usage, runtime, model, tool, and daily aggregates",
        "used_by": "Project analytics APIs",
    },
    {
        "path": "timeline.jsonl",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher timeline sink",
        "purpose": "Append-only normalized history of sessions, files, tools, and tasks",
        "used_by": "Projects",
    },
    # The workitems surface spans both tiers on purpose (workitems-plan §3).
    # The authored half above is durable and travels; these two are not. The
    # mirror is re-fetched every 60 s and the claims file is one machine's
    # live process state, so either one written into ``.xo/`` would churn the
    # synced tier at a poller's rate — the exact thing T19/T20 removed. They
    # hang off the runtime root, and a row that said otherwise would render
    # "0 present" while the files sat somewhere else entirely.
    {
        "path": "github/issues.json",
        "tier": _TIER_RUNTIME,
        "producer": "GitHub issue poller",
        "purpose": (
            "Re-fetchable snapshot of the project repo's issues: state, "
            "assignees, poll budget, and last error"
        ),
        "used_by": "Workitems (adopted items)",
    },
    {
        "path": "workitems/claims.json",
        "tier": _TIER_RUNTIME,
        "producer": "Workitem claim API",
        "purpose": (
            "Which agent session is working which workitem; the only input "
            "to the derived in_progress"
        ),
        "used_by": "Workitems",
    },
)

# The workspace tier splits the same way the per-project one does, and for
# the same reason (syncplan T20): the two records a clone would want stay in
# ``<XO root>/.xo/``, every rollup the watcher recomputes from a walk of the
# projects root moved to ``~/.quirq/workspace/``. Rows without the right tier
# would report "0 present" against a directory nothing writes any more.
_WORKSPACE_OUTPUT_CONTRACT = (
    {
        "path": "space.json",
        "tier": _TIER_SYNCED,
        "producer": "Watcher Space record writer",
        "purpose": "The Space record: its captured id, label, roots and attached agent backends",
        "used_by": "Space identity, Setup",
    },
    {
        "path": "projects.json",
        "tier": _TIER_SYNCED,
        "producer": "Watcher workspace rollup",
        "purpose": "The projects registry: every project directory with its pid, scaffold state and git origin",
        "used_by": "Projects, Graph",
    },
    {
        "path": "xo.json",
        "tier": _TIER_SYNCED,
        "producer": "Server startup + status probes",
        "purpose": "Frontend manifest: agent capability flags and live model/channel status",
        "used_by": "Every tab (feature gating)",
    },
    {
        "path": "graph.json",
        "tier": _TIER_RUNTIME,
        "purpose": "The derived workspace graph served at GET /xo/space.json",
        "producer": "Watcher view builder",
        "used_by": "Graph, Tree, Files list",
    },
    {
        "path": "dashboard.json",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher view builder",
        "purpose": "The same workspace scan collapsed into purpose environments",
        "used_by": "Dashboard",
    },
    {
        "path": "sessions.json",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher view builder",
        "purpose": "Session telemetry merged across every runtime that reports it",
        "used_by": "Sessions",
    },
    {
        "path": "sessions/sessionslist.json",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher workspace rollup",
        "purpose": "Union of every project session index",
        "used_by": "Workspace APIs, Graph",
    },
    {
        "path": "sessions/sessions-augment.json",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher workspace rollup",
        "purpose": "Union of watcher-derived session summaries",
        "used_by": "Workspace APIs, Graph",
    },
    {
        "path": "stats.json",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher workspace rollup",
        "purpose": "Aggregated statistics across every project",
        "used_by": "Workspace analytics APIs",
    },
    {
        "path": "timeline.jsonl",
        "tier": _TIER_RUNTIME,
        "producer": "Watcher workspace rollup",
        "purpose": "Multiplexed project timelines tagged with project id",
        "used_by": "Workspace timeline APIs",
    },
)


def _iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None


def _description(relative_path: str, *, is_dir: bool) -> str:
    if is_dir:
        if relative_path == "watcher":
            return "Watcher cursors, locks, and live presence"
        if relative_path == "watcher/activity":
            return "Ephemeral activity snapshots"
        if relative_path == "watcher/activity/projects":
            return "Per-project live presence"
        # The two runtime tiers (syncplan T19/T20). Naming them here is what
        # keeps the tree from rendering the derived state as anonymous
        # "Directory" rows once it stopped living in ``.xo/``.
        if relative_path == "projects":
            return "Per-project runtime tier, keyed by project.json:pid"
        if relative_path == "workspace":
            return "Derived workspace views, recomputed from a walk of the projects root"
        if relative_path.endswith("/sessionslist.d"):
            return "Session index shards, one file per session"
        # The two runtime-tier workitems directories, for the same reason the
        # tiers above are named: they would otherwise be anonymous rows.
        if relative_path.endswith("/github"):
            return "GitHub issue mirror, re-fetched by the poller"
        if relative_path.endswith("/workitems"):
            return "Live workitem claims for this machine"
        return "Directory"
    name = Path(relative_path).name
    if name == "state.json":
        return "Installation and onboarding state"
    if name == "runtime.env":
        return "Non-secret runtime settings"
    if name == "roots.env":
        return "Host roots queued for the installer"
    if name == "secrets.env":
        return "Write-only credentials; values are masked"
    if name == "offsets.json":
        return "Watcher read cursors; source paths are hidden"
    if relative_path == "watcher/heartbeat.json":
        return "Watcher liveness beat, rewritten every tick"
    if relative_path == "watcher/activity/workspace.json":
        return "Workspace-wide live presence"
    if relative_path.startswith("watcher/activity/projects/"):
        return "Project live presence"
    if relative_path.startswith("projects/"):
        return "Project runtime state; re-derivable, never synced"
    if relative_path.startswith("workspace/"):
        return "Derived workspace view; rebuilt from a workspace walk"
    return "Machine-local state file"


def _tree(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    file_count = 0
    directory_count = 0
    total_bytes = 0
    truncated = False
    if not root.is_dir():
        return items, {
            "files": 0,
            "directories": 0,
            "bytes": 0,
            "truncated": False,
        }

    try:
        paths = sorted(root.rglob("*"), key=lambda path: str(path).lower())
    except OSError:
        paths = []
    for path in paths:
        if len(items) >= _MAX_FILES:
            truncated = True
            break
        try:
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            stat = path.stat()
            is_dir = path.is_dir()
        except (OSError, ValueError):
            continue
        size = 0 if is_dir else stat.st_size
        if is_dir:
            directory_count += 1
        else:
            file_count += 1
            total_bytes += size
        items.append(
            {
                "path": relative,
                "name": path.name,
                "depth": len(Path(relative).parts) - 1,
                "kind": "directory" if is_dir else "file",
                "size_bytes": size,
                "modified_at": _iso_time(stat.st_mtime),
                "sensitive": path.name in _SENSITIVE_NAMES,
                "description": _description(relative, is_dir=is_dir),
            }
        )
    return items, {
        "files": file_count,
        "directories": directory_count,
        "bytes": total_bytes,
        "truncated": truncated,
    }


def _activity(root: Path) -> dict[str, Any]:
    activity_root = root / "watcher" / "activity"
    workspace = _read_json(activity_root / "workspace.json")
    workspace_sessions = (
        workspace.get("open_sessions", [])
        if isinstance(workspace, dict)
        else []
    )
    projects: list[dict[str, Any]] = []
    projects_root = activity_root / "projects"
    try:
        project_files = sorted(projects_root.glob("*.json"))
    except OSError:
        project_files = []
    for path in project_files[:_MAX_FILES]:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        sessions = data.get("open_sessions")
        sessions = sessions if isinstance(sessions, list) else []
        runtimes = sorted(
            {
                str(row.get("runtime"))
                for row in sessions
                if isinstance(row, dict) and row.get("runtime")
            }
        )
        projects.append(
            {
                "project_id": path.stem,
                "open_sessions": len(sessions),
                "runtimes": runtimes,
                "updated_at": data.get("updated_at"),
            }
        )
    return {
        "workspace_open_sessions": len(workspace_sessions),
        "workspace_updated_at": (
            workspace.get("updated_at") if isinstance(workspace, dict) else None
        ),
        "projects": projects,
    }


def _parse_iso(value: Any) -> datetime | None:
    """Parse a watcher timestamp, tolerating both ``Z`` and offset forms."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stale_after_seconds(interval_seconds: Any) -> float:
    """Age past which a heartbeat means "not ticking".

    Derived from the configured tick interval rather than a magic
    constant: the loop sleeps ``interval`` *between* ticks and the tick
    itself takes non-zero time, so the observed period is always a
    little longer than the interval. ``_MISSED_TICKS_BEFORE_DEAD``
    missed beats is an unambiguous signal rather than a scheduling
    hiccup, and the floor keeps a 0.25 s interval from flapping on one
    slow tick.
    """
    try:
        interval = float(interval_seconds)
    except (TypeError, ValueError):
        interval = 1.0
    if interval <= 0:
        interval = 1.0
    return max(_HEARTBEAT_STALE_FLOOR_S, interval * _MISSED_TICKS_BEFORE_DEAD)


def _watcher(root: Path) -> dict[str, Any]:
    offsets = _read_json(root / "watcher" / "offsets.json")
    if isinstance(offsets, dict):
        tracked_files = len(offsets)
    elif isinstance(offsets, list):
        tracked_files = len(offsets)
    else:
        tracked_files = 0
    configured = configured_settings()
    applied = effective_settings()

    # Observed liveness. Everything above this line is *configuration* —
    # it says what the watcher was asked to do, never whether the loop is
    # running. The heartbeat is the only signal that distinguishes a
    # ticking watcher from a crashed one, so a missing, unreadable or
    # stale file all resolve to alive=False.
    heartbeat_path = root / "watcher" / "heartbeat.json"
    heartbeat = _read_json(heartbeat_path)
    if not isinstance(heartbeat, dict):
        heartbeat = {}
    last_tick_at = heartbeat.get("last_tick_at")
    last_tick = _parse_iso(last_tick_at)
    stale_after = _stale_after_seconds(applied["watcher_interval_seconds"])
    if last_tick is None:
        age: float | None = None
    else:
        # Clamp: the stamp has second granularity, so a beat written in
        # the same second reads as very slightly in the future.
        age = round(
            max(0.0, (datetime.now(timezone.utc) - last_tick).total_seconds()), 3
        )
    tick_count = heartbeat.get("tick_count")
    duration_ms = heartbeat.get("duration_ms")

    return {
        "enabled": applied["watcher_enabled"],
        "interval_seconds": applied["watcher_interval_seconds"],
        "source_mode": applied["watcher_source_mode"],
        "configured_enabled": configured["watcher_enabled"],
        "tracked_files": tracked_files,
        "offsets_present": (root / "watcher" / "offsets.json").is_file(),
        "heartbeat_present": heartbeat_path.is_file(),
        "last_tick_at": last_tick_at if isinstance(last_tick_at, str) else None,
        "tick_count": (
            tick_count
            if isinstance(tick_count, int) and not isinstance(tick_count, bool)
            else None
        ),
        "last_tick_duration_ms": (
            duration_ms
            if isinstance(duration_ms, (int, float))
            and not isinstance(duration_ms, bool)
            else None
        ),
        "heartbeat_age_seconds": age,
        "heartbeat_stale_after_seconds": stale_after,
        "alive": age is not None and age <= stale_after,
    }


def _install_state(root: Path) -> dict[str, Any]:
    raw = _read_json(root / "state.json")
    if not isinstance(raw, dict):
        return {"present": False}
    return {
        "present": True,
        "onboarding_completed": bool(raw.get("onboarding_completed")),
        "onboarding_completed_at": raw.get("onboarding_completed_at"),
    }


def _measure(path: Path) -> tuple[bool, int, float]:
    """``(present, bytes, newest mtime)`` for a file **or** a directory.

    The session index is a directory of shard files now, so "present" has to
    mean "holds at least one file" for it and "is a file" for everything else.
    Symlinks are never followed, at either level — the catalog reports what is
    on this machine, it does not chase a link out of the tree.
    """
    try:
        if path.is_symlink():
            return False, 0, 0.0
        if path.is_file():
            stat = path.stat()
            return True, stat.st_size, stat.st_mtime
        if path.is_dir():
            total = 0
            latest = 0.0
            found = False
            for child in sorted(path.iterdir()):
                if child.is_symlink() or not child.is_file():
                    continue
                stat = child.stat()
                total += stat.st_size
                latest = max(latest, stat.st_mtime)
                found = True
            return found, total, latest
    except OSError:
        pass
    return False, 0, 0.0


def _contract_status(
    bases: dict[str, list[Path]],
    contract: tuple[dict[str, str], ...],
    *,
    prefixes: dict[str, str],
) -> list[dict[str, Any]]:
    """Roll one contract up across every project.

    ``bases`` and ``prefixes`` are keyed by tier, because both contracts now
    span two roots — the per-project one since T19, the workspace one since
    T20. An entry with no ``tier`` reads as synced, which is the safe default:
    a mis-tiered row reports "0 present" rather than erroring.
    """
    rows: list[dict[str, Any]] = []
    for definition in contract:
        tier = definition.get("tier", _TIER_SYNCED)
        present = 0
        total_bytes = 0
        latest_mtime = 0.0
        for base in bases.get(tier, ()):
            found, size, mtime = _measure(base / definition["path"])
            if not found:
                continue
            present += 1
            total_bytes += size
            latest_mtime = max(latest_mtime, mtime)
        rows.append(
            {
                **definition,
                "location": f"{prefixes[tier]}/{definition['path']}",
                "present_count": present,
                "bytes": total_bytes,
                "updated_at": _iso_time(latest_mtime) if latest_mtime else None,
            }
        )
    return rows


def _project_outputs() -> dict[str, Any]:
    # Same root helper as every other tab — see project_layout.
    projects_root = xo_projects_root()
    host_root = (
        os.getenv("QUIRQ_HOST_PROJECTS_ROOT", "") or ""
    ).strip()
    project_dirs: list[tuple[str, Path]] = []
    if projects_root.is_dir():
        try:
            candidates = sorted(
                projects_root.iterdir(),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            candidates = []
        for candidate in candidates:
            if candidate.name.startswith(".") or candidate.is_symlink():
                continue
            try:
                xo_dir = candidate / ".xo"
                if candidate.is_dir() and xo_dir.is_dir() and not xo_dir.is_symlink():
                    # The runtime home is resolved through project_layout, which
                    # keys it by ``project.json:pid`` and answers None for a
                    # project that has none yet. None here means "runtime files
                    # not present", never an error.
                    project_dirs.append(
                        (
                            candidate.name,
                            xo_dir,
                            runtime_dir_for_project(candidate.name),
                        )
                    )
            except OSError:
                continue
            if len(project_dirs) >= 200:
                break

    bases = {
        _TIER_SYNCED: [xo_dir for _, xo_dir, _ in project_dirs],
        _TIER_RUNTIME: [rt for _, _, rt in project_dirs if rt is not None],
    }
    projects: list[dict[str, Any]] = []
    for project_id, xo_dir, runtime_dir in project_dirs:
        roots = {_TIER_SYNCED: xo_dir, _TIER_RUNTIME: runtime_dir}
        files = []
        total_bytes = 0
        latest_mtime = 0.0
        for definition in _PROJECT_OUTPUT_CONTRACT:
            base = roots.get(definition.get("tier", _TIER_SYNCED))
            if base is None:
                continue
            found, size, mtime = _measure(base / definition["path"])
            if not found:
                continue
            files.append(definition["path"])
            total_bytes += size
            latest_mtime = max(latest_mtime, mtime)
        legacy_activity = (xo_dir / "activity.json").is_file()
        projects.append(
            {
                "project_id": project_id,
                "container_path": str(xo_dir),
                "runtime_path": str(runtime_dir) if runtime_dir else "",
                "host_path": (
                    str(Path(host_root) / project_id / ".xo")
                    if host_root
                    else ""
                ),
                "watcher_files": files,
                "watcher_file_count": len(files),
                "bytes": total_bytes,
                "updated_at": _iso_time(latest_mtime) if latest_mtime else None,
                "legacy_activity_file": legacy_activity,
            }
        )

    workspace_xo = projects_root / ".xo"
    workspace_runtime = workspace_runtime_dir()
    workspace_synced_dirs = [workspace_xo] if workspace_xo.is_dir() else []
    workspace_runtime_dirs = (
        [workspace_runtime] if workspace_runtime.is_dir() else []
    )
    legacy_count = sum(
        1 for row in projects if row["legacy_activity_file"]
    ) + int((workspace_xo / "activity.json").is_file())
    return {
        "root": {
            "container_path": str(projects_root),
            "host_path": host_root,
            "exists": projects_root.exists(),
            "readable": projects_root.exists() and os.access(projects_root, os.R_OK),
        },
        "project_count": len(projects),
        "projects": projects,
        "project_contract": _contract_status(
            bases,
            _PROJECT_OUTPUT_CONTRACT,
            prefixes={
                _TIER_SYNCED: "<project>/.xo",
                _TIER_RUNTIME: "<quirq state>/projects/<pid>",
            },
        ),
        "workspace_contract": _contract_status(
            {
                _TIER_SYNCED: workspace_synced_dirs,
                _TIER_RUNTIME: workspace_runtime_dirs,
            },
            _WORKSPACE_OUTPUT_CONTRACT,
            prefixes={
                _TIER_SYNCED: "<XO root>/.xo",
                _TIER_RUNTIME: "<quirq state>/workspace",
            },
        ),
        "legacy_activity_files": legacy_count,
        "legacy_activity_note": (
            ".xo/activity.json is legacy. Current presence is written only "
            "under .quirq/watcher/activity."
        ),
    }


def quirq_catalog() -> dict[str, Any]:
    root = quirq_state_dir()
    tree, totals = _tree(root)
    credentials = sorted(
        {
            str(entry.get("key") or "").strip()
            for entry in load_env_entries()
            if str(entry.get("key") or "").strip()
        }
    )
    roots = root_settings()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": {
            "container_path": str(root),
            "host_path": (
                os.getenv("QUIRQ_HOST_STATE_ROOT", "") or ""
            ).strip(),
            "exists": root.exists(),
            "readable": root.exists() and os.access(root, os.R_OK),
            "writable": root.exists() and os.access(root, os.W_OK),
        },
        "totals": totals,
        "tree": tree,
        "activity": _activity(root),
        "watcher": _watcher(root),
        "runtime": configured_settings(),
        "credentials": [
            {"key": key, "configured": True, "value": "••••••"}
            for key in credentials
        ],
        "install_state": _install_state(root),
        "root_change_required": roots["change_required"],
        "project_outputs": _project_outputs(),
    }
