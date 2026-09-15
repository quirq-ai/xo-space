"""Read-only Space session telemetry for local Cursor agent sessions.

Cursor keeps several overlapping local stores. This capability prefers the
host-visible agent transcript JSONL under ``~/.cursor/projects/*/agent-transcripts``
(present on remote SSH workspaces) and optionally enriches with:

- ``~/.cursor/chats/**/store.db`` session metadata (model / timestamps)
- Cursor ``state.vscdb`` bubble token snapshots when the desktop user-data
  directory is mounted on this host

Prompt text, reasoning, tool arguments, and tool results are never retained
in the returned payload. When native token counters are missing, character
length is converted to an explicit ``unclassified`` estimate
(``breakdown_known: false``) so unknown usage is never labeled as fresh input.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote


SOURCE_ID = "cursor"
SOURCE_LABEL = "Cursor"
META_PRIORITY = 20
COST_STATUS = "unavailable"
# How Space's Configure page presents and edits this source. Read generically
# by services/telemetry_sources.py; the env key is the one _cursor_home() honors.
SOURCE_CONFIG = {
    "vendor": "cursor",
    "path_env": "CURSOR_HOME",
    "path_default": "~/.cursor",
    "path_kind": "dir",
    "path_label": "Cursor home",
    "collects": ["Agent transcripts", "Chat sessions", "Token snapshots"],
    "never": "No prompt text stored",
}

MAX_SESSIONS = 500
MAX_TOOLS_PER_SESSION = 10
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
_BUSY_TIMEOUT_MS = 2000

_TIMESTAMP_RE = re.compile(
    r"<timestamp>\s*([^<]+?)\s*</timestamp>", re.IGNORECASE
)
def _cursor_home() -> Path:
    configured = (os.getenv("CURSOR_HOME") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".cursor"


def _user_data_roots() -> list[Path]:
    """Candidate Cursor/VS Code user-data roots that may hold state.vscdb.

    When any Cursor location is configured via env (CURSOR_HOME,
    CURSOR_USER_DATA, CURSOR_DATA_DIR), only explicit roots are used so a
    redirected environment never picks up this machine's real Cursor data.
    """
    roots: list[Path] = []
    for env_name in ("CURSOR_USER_DATA", "CURSOR_DATA_DIR"):
        configured = (os.getenv(env_name) or "").strip()
        if configured:
            roots.append(Path(configured).expanduser())
    if not roots and not (os.getenv("CURSOR_HOME") or "").strip():
        home = Path.home()
        roots.extend([
            home / ".config" / "Cursor",
            home / "Library" / "Application Support" / "Cursor",
            home / ".cursor-server" / "data",
            home / "AppData" / "Roaming" / "Cursor",
        ])
    # Preserve order while dropping duplicates.
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def runtime_mounts() -> list[Path]:
    """Native directories the managed Docker runtime may read if present."""
    return [_cursor_home(), *_user_data_roots()]


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute(f"pragma busy_timeout={_BUSY_TIMEOUT_MS}")
    return connection


def _iso_from_epoch_ms(value: Any) -> str | None:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    # Accept seconds accidentally stored as small numbers.
    if ms < 1_000_000_000_000:
        ms *= 1000
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _parse_timestamp_text(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    # ISO-ish first.
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError:
        pass
    # Cursor agent transcript banner: "Sunday, Jul 19, 2026, 12:25 PM (UTC+5:30)"
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    for fmt in (
        "%A, %b %d, %Y, %I:%M %p",
        "%A, %B %d, %Y, %I:%M %p",
        "%b %d, %Y, %I:%M %p",
        "%B %d, %Y, %I:%M %p",
    ):
        try:
            return datetime.strptime(cleaned, fmt).replace(
                tzinfo=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # Rough local estimate only — surfaced as unclassified, never as fresh.
    return max(1, len(text) // 4) if text.strip() else 0


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _iter_tool_names(content: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(content, list):
        return names
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type in {
            "tool_use", "tool_call", "function_call", "mcp_tool_call",
            "toolcall", "tool-call",
        }:
            name = (
                item.get("name")
                or item.get("toolName")
                or item.get("tool_name")
            )
            if not name and isinstance(item.get("function"), dict):
                name = item["function"].get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _project_label(project_dir_name: str, project_path: str) -> str:
    if project_path:
        return project_path.rstrip("/").rsplit("/", 1)[-1] or project_dir_name
    return project_dir_name or "(unknown)"


def _decode_project_path(project_dir_name: str) -> str:
    """Best-effort workspace path from Cursor's sanitized project folder name."""
    if not project_dir_name:
        return ""
    # Common forms: "home-coder", encoded URI fragments, or already absolute.
    if project_dir_name.startswith("/") or re.match(r"^[A-Za-z]:\\", project_dir_name):
        return project_dir_name
    if "%2F" in project_dir_name or "%2f" in project_dir_name:
        return unquote(project_dir_name)
    # Keep the sanitized id when the real path is unknown — still stable.
    return project_dir_name


def _discover_transcripts(root: Path) -> list[tuple[str, str, Path]]:
    """Return (session_id, project_dir_name, transcript_path) tuples."""
    projects = root / "projects"
    if not projects.is_dir():
        return []
    found: list[tuple[str, str, Path]] = []
    for project_dir in projects.iterdir():
        if not project_dir.is_dir():
            continue
        transcripts = project_dir / "agent-transcripts"
        if not transcripts.is_dir():
            continue
        for path in transcripts.rglob("*.jsonl"):
            if not path.is_file():
                continue
            session_id = path.stem
            # Nested form: <uuid>/<uuid>.jsonl — prefer the directory name.
            if path.parent.name != "agent-transcripts":
                session_id = path.parent.name
            found.append((session_id, project_dir.name, path))
    found.sort(key=lambda item: item[2].stat().st_mtime_ns, reverse=True)
    return found


def _load_store_meta(root: Path) -> dict[str, dict]:
    """Map session/agent id → {model, started_at, ended_at, name} from store.db."""
    chats = root / "chats"
    if not chats.is_dir():
        return {}
    out: dict[str, dict] = {}
    for db_path in chats.rglob("store.db"):
        if not db_path.is_file():
            continue
        connection = None
        try:
            connection = _connect_ro(db_path)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            if "meta" not in tables:
                continue
            rows = connection.execute("select key, value from meta").fetchall()
            meta = {
                str(key): value
                for key, value in rows
                if isinstance(key, str)
            }
            raw = meta.get("value") or meta.get("data") or meta.get("session")
            payload: dict[str, Any] = {}
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")
            if isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        payload = loaded
                except json.JSONDecodeError:
                    payload = {}
            if not payload:
                # Flat key/value meta rows.
                for key, value in meta.items():
                    if isinstance(value, (bytes, bytearray)):
                        try:
                            value = value.decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                    if isinstance(value, str) and value[:1] in "[{":
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            pass
                    payload[key] = value
            agent_id = str(
                payload.get("agentId")
                or payload.get("agent_id")
                or payload.get("id")
                or db_path.parent.name
            )
            if not agent_id:
                continue
            created = (
                _iso_from_epoch_ms(payload.get("createdAt"))
                or _parse_timestamp_text(str(payload.get("createdAt") or ""))
            )
            updated = (
                _iso_from_epoch_ms(payload.get("updatedAt") or payload.get("lastUpdatedAt"))
                or created
            )
            model = str(payload.get("lastUsedModel") or payload.get("model") or "")
            out[agent_id] = {
                "model": model or "unknown",
                "started_at": created,
                "ended_at": updated,
                "name": str(payload.get("name") or ""),
            }
        except (OSError, sqlite3.Error, UnicodeDecodeError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return out


def _load_bubble_tokens(user_roots: list[Path]) -> dict[str, dict]:
    """Map composerId → {tokens, fresh, output, model, started_at, ended_at}."""
    out: dict[str, dict] = {}
    for root in user_roots:
        db_path = root / "User" / "globalStorage" / "state.vscdb"
        if not db_path.is_file():
            continue
        connection = None
        try:
            connection = _connect_ro(db_path)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            if "cursorDiskKV" not in tables:
                continue
            rows = connection.execute(
                "select key, value from cursorDiskKV "
                "where key like 'composerData:%' or key like 'bubbleId:%'"
            ).fetchall()
        except (OSError, sqlite3.Error):
            continue
        finally:
            if connection is not None:
                connection.close()

        composers: dict[str, dict] = {}
        bubbles: dict[str, list[dict]] = defaultdict(list)
        for key, value in rows:
            if not isinstance(key, str):
                continue
            if isinstance(value, (bytes, bytearray)):
                try:
                    value = value.decode("utf-8", errors="ignore")
                except Exception:
                    continue
            if not isinstance(value, str) or not value:
                continue
            try:
                payload = json.loads(value)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if key.startswith("composerData:"):
                composer_id = key.split(":", 1)[1]
                composers[composer_id] = payload
            elif key.startswith("bubbleId:"):
                parts = key.split(":", 2)
                if len(parts) != 3:
                    continue
                bubbles[parts[1]].append(payload)

        for composer_id, composer in composers.items():
            fresh = 0
            output = 0
            native = False
            for bubble in bubbles.get(composer_id, []):
                token_count = bubble.get("tokenCount")
                if isinstance(token_count, dict):
                    input_tokens = token_count.get("inputTokens")
                    output_tokens = token_count.get("outputTokens")
                    if isinstance(input_tokens, (int, float)) and input_tokens >= 0:
                        fresh += int(input_tokens)
                        native = True
                    if isinstance(output_tokens, (int, float)) and output_tokens >= 0:
                        output += int(output_tokens)
                        native = True
                else:
                    for in_key, out_key in (
                        ("inputTokens", "outputTokens"),
                        ("input_tokens", "output_tokens"),
                    ):
                        if isinstance(bubble.get(in_key), (int, float)):
                            fresh += int(bubble[in_key])
                            native = True
                        if isinstance(bubble.get(out_key), (int, float)):
                            output += int(bubble[out_key])
                            native = True
            if not native and not composer:
                continue
            started = (
                _iso_from_epoch_ms(composer.get("createdAt"))
                or _iso_from_epoch_ms(composer.get("createdAtEpochMs"))
            )
            ended = (
                _iso_from_epoch_ms(composer.get("lastUpdatedAt"))
                or _iso_from_epoch_ms(composer.get("updatedAt"))
                or started
            )
            model = str(
                composer.get("model")
                or composer.get("lastUpdatedModel")
                or composer.get("lastUsedModel")
                or "unknown"
            )
            out[composer_id] = {
                "fresh": fresh,
                "output": output,
                "tokens": fresh + output,
                "breakdown_known": native and (fresh + output) > 0,
                "model": model or "unknown",
                "started_at": started,
                "ended_at": ended,
                "project_path": _composer_project_path(composer),
            }
        # One readable global DB is enough.
        if out:
            break
    return out


def _composer_project_path(composer: dict) -> str:
    workspace = composer.get("workspaceIdentifier")
    if isinstance(workspace, dict):
        uri = workspace.get("uri")
        if isinstance(uri, dict):
            fs_path = uri.get("fsPath") or uri.get("path")
            if isinstance(fs_path, str) and fs_path.strip():
                return fs_path.strip()
        workspace_id = workspace.get("id")
        if isinstance(workspace_id, str) and workspace_id.strip():
            return workspace_id.strip()
    for key in ("workspacePath", "projectPath", "cwd"):
        value = composer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_transcript(path: Path) -> dict:
    turns = 0
    estimated = 0
    tools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    started_at: str | None = None
    ended_at: str | None = None
    model = "unknown"
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileNotFoundError(f"Cursor transcript unreadable: {path}") from exc
    if size > MAX_TRANSCRIPT_BYTES:
        # Still count the session from filesystem metadata; skip heavy parse.
        mtime = _iso_from_epoch_ms(path.stat().st_mtime * 1000)
        return {
            "turns": 0,
            "estimated": 0,
            "tools": [],
            "started_at": mtime,
            "ended_at": mtime,
            "model": "unknown",
            "oversized": True,
        }

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "")
            message = row.get("message")
            content: Any = None
            if isinstance(message, dict):
                content = message.get("content")
                for key in ("model", "modelName", "model_id"):
                    value = message.get(key)
                    if isinstance(value, str) and value.strip():
                        model = value.strip()
            elif isinstance(message, str):
                content = message
            text = _content_text(content)
            if role == "user":
                turns += 1
                match = _TIMESTAMP_RE.search(text)
                if match:
                    parsed = _parse_timestamp_text(match.group(1))
                    if parsed:
                        if started_at is None or parsed < started_at:
                            started_at = parsed
                        if ended_at is None or parsed > ended_at:
                            ended_at = parsed
            estimated += _estimate_tokens(text)
            for name in _iter_tool_names(content):
                tools[name][0] += 1
            # Timestamp fields some writers attach at the top level.
            for key in ("timestamp", "createdAt", "created_at"):
                value = row.get(key)
                parsed = None
                if isinstance(value, (int, float)):
                    parsed = _iso_from_epoch_ms(value)
                elif isinstance(value, str):
                    parsed = _parse_timestamp_text(value) or _iso_from_epoch_ms(value)
                if parsed:
                    if started_at is None or parsed < started_at:
                        started_at = parsed
                    if ended_at is None or parsed > ended_at:
                        ended_at = parsed

    if started_at is None or ended_at is None:
        try:
            stat = path.stat()
            fallback = _iso_from_epoch_ms(stat.st_mtime * 1000)
            started_at = started_at or fallback
            ended_at = ended_at or fallback
        except OSError:
            pass

    tool_rows = [
        {"name": name, "calls": values[0], "errors": values[1]}
        for name, values in sorted(tools.items(), key=lambda item: (-item[1][0], item[0]))
    ]
    return {
        "turns": turns,
        "estimated": estimated,
        "tools": tool_rows,
        "started_at": started_at,
        "ended_at": ended_at,
        "model": model,
        "oversized": False,
    }


def _duration_sec(started_at: str | None, ended_at: str | None) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = int((end - start).total_seconds())
    return max(0, seconds)


def collect_session_telemetry() -> dict:
    root = _cursor_home()
    if not root.is_dir():
        raise FileNotFoundError(
            f"Cursor home not found at {root} (set CURSOR_HOME to change)"
        )

    transcripts = _discover_transcripts(root)
    store_meta = _load_store_meta(root)
    bubble_tokens = _load_bubble_tokens(_user_data_roots())

    if not transcripts and not bubble_tokens and not store_meta:
        raise FileNotFoundError(
            f"No Cursor sessions found under {root}/projects/*/agent-transcripts "
            "or Cursor chat/state stores"
        )

    sessions: list[dict] = []
    daily_models: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    daily_sessions: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    daily_tools: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    project_keys: set[str] = set()
    seen_ids: set[str] = set()

    def add_daily(
        day: str,
        session_id: str,
        model: str,
        tokens: int,
        unclassified: int,
        tool_rows: list[dict],
    ) -> None:
        if not day:
            return
        daily_models[(day, model or "unknown")][0] += tokens
        daily_models[(day, model or "unknown")][1] += unclassified
        daily_sessions[(day, session_id)][0] += tokens
        daily_sessions[(day, session_id)][1] += unclassified
        for tool in tool_rows:
            name = str(tool.get("name") or "?")
            daily_tools[(day, name)][0] += int(tool.get("calls") or 0)
            daily_tools[(day, name)][1] += int(tool.get("errors") or 0)

    for session_id, project_dir_name, path in transcripts:
        if session_id in seen_ids:
            continue
        parsed = _parse_transcript(path)
        meta = store_meta.get(session_id) or {}
        bubble = bubble_tokens.get(session_id) or {}
        project_path = (
            bubble.get("project_path")
            or _decode_project_path(project_dir_name)
        )
        project_keys.add(project_path or project_dir_name or "(unknown)")
        model = (
            (bubble.get("model") if bubble.get("model") not in (None, "", "unknown") else None)
            or (meta.get("model") if meta.get("model") not in (None, "", "unknown") else None)
            or parsed.get("model")
            or "unknown"
        )
        started_at = (
            parsed.get("started_at")
            or meta.get("started_at")
            or bubble.get("started_at")
        )
        ended_at = (
            parsed.get("ended_at")
            or meta.get("ended_at")
            or bubble.get("ended_at")
            or started_at
        )
        if bubble.get("breakdown_known"):
            fresh = int(bubble.get("fresh") or 0)
            output = int(bubble.get("output") or 0)
            tokens = int(bubble.get("tokens") or (fresh + output))
            unclassified = 0
            breakdown_known = True
        else:
            fresh = 0
            output = 0
            tokens = int(parsed.get("estimated") or 0)
            unclassified = tokens
            breakdown_known = False
        # Keep zero-usage rows when they have turns — Cursor transcripts often
        # lack native counters, and the session list is still useful.
        if tokens <= 0 and int(parsed.get("turns") or 0) <= 0 and not path.stat().st_size:
            continue
        day = (started_at or ended_at or "")[:10]
        tool_rows = parsed.get("tools") or []
        add_daily(day, session_id, str(model), tokens, unclassified, tool_rows)
        sessions.append({
            "id": session_id,
            "key": f"{SOURCE_ID}:{session_id}",
            "agent": SOURCE_ID,
            "project": _project_label(project_dir_name, str(project_path or "")),
            "project_path": str(project_path or project_dir_name or ""),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_sec": _duration_sec(started_at, ended_at),
            "model": str(model or "unknown"),
            "agent_version": "",
            "turns": int(parsed.get("turns") or 0),
            "fresh": fresh,
            "output": output,
            "cache_read": 0,
            "cache_write": 0,
            "tokens": tokens,
            "own_tokens": tokens,
            "total_tokens": tokens,
            "unclassified": unclassified,
            "breakdown_known": breakdown_known,
            "cost": 0.0,
            "cost_known": False,
            "tools": tool_rows[:MAX_TOOLS_PER_SESSION],
            "subagents": [],
        })
        seen_ids.add(session_id)
        if len(sessions) >= MAX_SESSIONS:
            break

    # Desktop-only composers that never wrote a host transcript.
    if len(sessions) < MAX_SESSIONS:
        for composer_id, bubble in sorted(
            bubble_tokens.items(),
            key=lambda item: item[1].get("ended_at") or item[1].get("started_at") or "",
            reverse=True,
        ):
            if composer_id in seen_ids:
                continue
            tokens = int(bubble.get("tokens") or 0)
            if tokens <= 0:
                continue
            project_path = str(bubble.get("project_path") or "(unknown)")
            project_keys.add(project_path)
            started_at = bubble.get("started_at")
            ended_at = bubble.get("ended_at") or started_at
            model = str(bubble.get("model") or "unknown")
            day = (started_at or ended_at or "")[:10]
            fresh = int(bubble.get("fresh") or 0)
            output = int(bubble.get("output") or 0)
            unclassified = 0 if bubble.get("breakdown_known") else tokens
            add_daily(day, composer_id, model, tokens, unclassified, [])
            sessions.append({
                "id": composer_id,
                "key": f"{SOURCE_ID}:{composer_id}",
                "agent": SOURCE_ID,
                "project": _project_label("", project_path),
                "project_path": project_path,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_sec": _duration_sec(started_at, ended_at),
                "model": model,
                "agent_version": "",
                "turns": 0,
                "fresh": fresh,
                "output": output,
                "cache_read": 0,
                "cache_write": 0,
                "tokens": tokens,
                "own_tokens": tokens,
                "total_tokens": tokens,
                "unclassified": unclassified,
                "breakdown_known": bool(bubble.get("breakdown_known")),
                "cost": 0.0,
                "cost_known": False,
                "tools": [],
                "subagents": [],
            })
            seen_ids.add(composer_id)
            if len(sessions) >= MAX_SESSIONS:
                break

    if not sessions:
        raise ValueError(f"Cursor stores under {root} contain no usable sessions")

    sessions.sort(key=lambda row: row.get("started_at") or "", reverse=True)
    all_tokens = sum(int(row.get("tokens") or 0) for row in sessions)
    kept_ids = {row["id"] for row in sessions}
    return {
        "source": {
            "id": SOURCE_ID,
            "label": SOURCE_LABEL,
            "cost_status": COST_STATUS,
            "transcript_files": len(transcripts),
            "store_sessions": len(store_meta),
            "composer_sessions": len(bubble_tokens),
        },
        "meta_priority": META_PRIORITY,
        "meta": {
            "db_path": str(root),
            "schema_version": None,
            "pricing_version": None,
        },
        "totals": {
            "sessions": len(sessions),
            "tokens": all_tokens,
            "cost_usd": 0.0,
            "projects": len(project_keys),
            "sessions_by_agent": {SOURCE_ID: len(sessions)},
        },
        "project_keys": sorted(project_keys),
        "sessions": sessions,
        "daily_models": [
            {
                "day": day, "agent": SOURCE_ID, "model": model,
                "tokens": values[0], "unclassified": values[1],
                "breakdown_known": values[1] == 0,
                "cost": 0.0, "cost_known": False,
            }
            for (day, model), values in sorted(daily_models.items())
        ],
        "daily_sessions": [
            {
                "day": day, "agent": SOURCE_ID, "session_id": session_id,
                "session_key": f"{SOURCE_ID}:{session_id}",
                "tokens": values[0], "unclassified": values[1],
                "breakdown_known": values[1] == 0,
                "cost": 0.0, "cost_known": False,
            }
            for (day, session_id), values in sorted(daily_sessions.items())
            if session_id in kept_ids
        ],
        "daily_tools": [
            {
                "day": day, "agent": SOURCE_ID, "name": name,
                "calls": values[0], "errors": values[1],
            }
            for (day, name), values in sorted(daily_tools.items())
        ],
    }
