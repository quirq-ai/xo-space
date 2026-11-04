"""What a chat's threads made (v2): the files they shared or wrote, the links
in their replies, and their branches, one row each in ``made``.

``Made`` hears every commit (an engine ``Listener``) and records:

* a ``share`` event's files (``source`` ``share``): the agent showed them
  to the person;
* at a turn's end, the files its ``create_file`` / ``edit_file`` calls
  wrote, those that succeeded (``write``), each tied to the turn's reply;
* an agent message's ``http(s)`` links (``reply``), with the link's text
  as its name when it is a Markdown link;
* a ``thread.git`` state with commits of the thread's own: its branch
  (``branch``).

One row per thing however often it was touched (its id hashes the chat and
the file's path, the URL or the branch); a write after a share keeps
``share``. Each transaction that changed rows appends one ``made {items}``
event, so open streams see them. Pins are the person's (``pin``).

Before a thread's worktree is removed (``before_removal``, called by the
worktrees), the files it shared are copied to
``<project>/.xo/made/thread-<ts>/`` (kept out of git by its own
``.gitignore``), so Made never goes blank. A listed file is found where it
was made, else at its copy, else, for a file made in a thread's worktree, at
the same place in the project folder (its branch was merged): ``where``
says which, and ``exists`` is false when none has it. ``project_id`` and
``relative_path`` address it on ``/api/xo-projects/{project_id}/file``,
``/image`` and ``/download``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Iterable, Mapping, Sequence

from services.cowork_agent import project_layout
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import Engine, Writes
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.worktrees import trees
from services.errors import ServiceError
from services.storage.atomic_write import create_file_exclusive
from services.timestamps import iso, parse_ts
from services.xo_structure import WORKTREES_DIRNAME, XO_DIRNAME, XO_GITIGNORE

log = logging.getLogger(__name__)

#: Under a project's ``.xo/``: the copies of what removed worktrees shared.
MADE_DIRNAME = "made"
GITIGNORE_TEXT = "# XO Space: copies of what threads shared, kept out of git\n*\n"
#: The tool kinds whose successful calls made a file.
WRITE_KINDS = ("create_file", "edit_file")
#: A file larger than this isn't copied when its worktree goes.
COPY_MAX_BYTES = 50 * 1024 * 1024
#: Path segments whose files are never Made: git's, XO's own records, and
#: what a build or install recreates.
SKIPPED_SEGMENTS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"})
#: The links one message adds at most.
MAX_LINKS = 20
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
EVENT = "made"
_MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
_URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
_TRAILING = ".,;:!?*_~'\""
_COLUMNS = ("id, seq, chat_id, thread_ts, message_id, turn_id, kind, source, path, url, name, title, mime, bytes, "
            "copy_path, created_at, updated_at, pinned_at")

_running: "Made | None" = None


class MadeRefused(ServiceError):
    pass


def current() -> "Made":
    if _running is None:
        raise MadeRefused("made_unavailable", "Made isn't running in this server yet; retry shortly.", status=503)
    return _running


def item_id(chat_id: str, key_kind: str, key: str) -> str:
    return "made_" + hashlib.sha256(f"{chat_id}\0{key_kind}\0{key}".encode("utf-8")).hexdigest()[:16]


def shared_path(folder: str | Path, path: str) -> Path:
    """Where a shared file is: its ``share`` event's ``folder`` and ``path``."""
    return Path(os.path.normpath(Path(folder) / path))


def file_id(chat_id: str, path: str | Path) -> str:
    """The Made item of a file at ``path`` (absolute, normalized)."""
    return item_id(chat_id, "file", str(path))


def _file_kind(name: str, mime: str | None) -> tuple[str, str]:
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return ("image" if mime.startswith("image/") else "file"), mime


def _skipped(path: Path) -> bool:
    parts = path.parts
    if SKIPPED_SEGMENTS.intersection(parts):
        return True
    # XO's own records under .xo/ are never Made; a thread worktree's files are.
    for i, part in enumerate(parts[:-1]):
        if part == XO_DIRNAME and not (i + 1 < len(parts) and parts[i + 1] == WORKTREES_DIRNAME):
            return True
    return False


def links(text: str) -> list[tuple[str, str]]:
    """``(url, name)`` of each http(s) link in ``text``, once each, in order:
    a Markdown link's text is its name, a bare URL is its own."""
    found: dict[str, str] = {}
    for name, url in _MD_LINK.findall(text):
        found.setdefault(url.rstrip(_TRAILING), name.strip())
    for url in _URL.findall(text):
        url = url.rstrip(_TRAILING)
        found.setdefault(url, url)
    return list(found.items())[:MAX_LINKS]


class Made:
    def __init__(self) -> None:
        self.engine: Engine | None = None
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    # ── life ─────────────────────────────────────────────────────────────

    def start(self, engine: Engine) -> None:
        global _running
        self.engine = engine
        engine.subscribe(self)
        _running = self

    async def stop(self) -> None:
        global _running
        if _running is self:
            _running = None
        if self.engine is not None:
            self.engine.unsubscribe(self)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── hearing the engine ───────────────────────────────────────────────

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if not event.chat_id:
                continue
            if event.type == "share":
                self._spawn(event.chat_id, self._shared(event))
            elif event.type == "turn.finished" and event.turn_id and event.session_id:
                self._spawn(event.chat_id, self._turn_wrote(event))
            elif event.type == "message" and event.data.get("author_kind") == "agent":
                self._spawn(event.chat_id, self._linked(event))
            elif event.type == "thread.git" and event.thread_ts:
                self._spawn(event.chat_id, self._branch(event))

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        """Nothing live matters here."""

    async def _shared(self, event: StoredEvent) -> None:
        folder = Path(event.data.get("folder") or "")
        items = []
        for f in event.data.get("files") or []:
            if not isinstance(f, Mapping) or not f.get("path") or not folder.is_absolute():
                continue
            path = shared_path(folder, f["path"])
            kind, mime = _file_kind(path.name, f.get("type"))
            items.append({"kind": kind, "source": "share", "path": str(path), "name": f.get("name") or path.name,
                          "title": f.get("title"), "mime": mime, "bytes": f.get("bytes")})
        await self._record(event, items, turn_id=event.turn_id)

    async def _turn_wrote(self, event: StoredEvent) -> None:
        engine = self._engine()

        def read(conn) -> tuple[str | None, list[tuple], tuple | None]:
            spec = conn.execute("SELECT spec FROM sessions WHERE id = ?", (event.session_id,)).fetchone()
            cwd = json.loads(spec[0]).get("cwd") if spec else None
            steps = conn.execute(
                "SELECT type, json_extract(data, '$.call_id'), json_extract(data, '$.kind'), "
                "json_extract(data, '$.target'), json_extract(data, '$.status'), json_extract(data, '$.size') "
                "FROM events WHERE turn_id = ? AND type IN ('step.tool', 'step.tool_done') ORDER BY seq",
                (event.turn_id,)).fetchall()
            reply = conn.execute(
                "SELECT m.id FROM messages m JOIN turns t ON t.id = ? WHERE m.chat_id = ? AND m.thread_ts IS ? "
                "AND m.author_kind = 'agent' AND m.author_session_id = ? AND m.created_at >= t.started_at "
                "ORDER BY m.seq DESC LIMIT 1",
                (event.turn_id, event.chat_id, event.thread_ts, event.session_id)).fetchone()
            return cwd, steps, reply
        cwd, steps, reply = await engine.store.read(read)
        started: dict[str, tuple[str, str]] = {}
        items: dict[str, dict[str, Any]] = {}
        for type_, call_id, kind, target, status, size in steps:
            if type_ == "step.tool":
                if kind in WRITE_KINDS and target:
                    started[call_id] = (kind, target)
                continue
            if status != "ok" or call_id not in started:
                continue
            target = started[call_id][1]
            path = Path(target) if os.path.isabs(target) else Path(cwd or "/") / target
            if not path.is_absolute():
                continue
            path = Path(os.path.normpath(path))
            if _skipped(path):
                continue
            file_kind, mime = _file_kind(path.name, None)
            items[str(path)] = {"kind": file_kind, "source": "write", "path": str(path), "name": path.name,
                                "mime": mime, "bytes": size}
        message_id = reply[0] if reply else None
        await self._record(event, list(items.values()), turn_id=event.turn_id, message_id=message_id,
                           tie_turn=True)

    async def _linked(self, event: StoredEvent) -> None:
        found = links(event.data.get("text") or "")
        if not found:
            return
        engine = self._engine()
        session = event.data.get("author_session_id")
        turn = await engine.store.read(lambda conn: conn.execute(
            "SELECT id FROM turns WHERE session_id = ? AND status = 'running' ORDER BY started_at DESC LIMIT 1",
            (session,)).fetchone()) if session else None
        items = [{"kind": "link", "source": "reply", "url": url, "name": name[:300], "mime": None, "bytes": None}
                 for url, name in found]
        await self._record(event, items, turn_id=turn[0] if turn else None, message_id=event.data.get("id"))

    async def _branch(self, event: StoredEvent) -> None:
        git = event.data.get("git") or {}
        if not git.get("branch") or not git.get("commits"):
            return
        await self._record(event, [{"kind": "branch", "source": "branch", "name": git["branch"], "mime": None,
                                    "bytes": None}])

    # ── writing rows ─────────────────────────────────────────────────────

    async def _record(self, event: StoredEvent, items: list[dict[str, Any]], *, turn_id: str | None = None,
                      message_id: str | None = None, tie_turn: bool = False) -> None:
        if not items and not (tie_turn and message_id):
            return
        chat_id, thread_ts = event.chat_id, event.thread_ts

        def write(w: Writes) -> None:
            if w.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
                return
            seq = 0                         # the seq of this transaction's event, set once it is appended
            changed: list[str] = []
            for item in items:
                key_kind, key = (("link", item["url"]) if item["kind"] == "link" else
                                 ("branch", item["name"]) if item["kind"] == "branch" else ("file", item["path"]))
                id_ = item_id(chat_id, key_kind, key)       # a file's is file_id(chat_id, path)
                before = w.execute("SELECT source, message_id, turn_id, bytes, mime, title FROM made WHERE id = ?",
                                   (id_,)).fetchone()
                if before is None:
                    w.execute(
                        f"INSERT INTO made ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, "
                        "NULL)",
                        (id_, seq, chat_id, thread_ts, message_id, turn_id, item["kind"], item["source"],
                         item.get("path"), item.get("url"), item["name"], item.get("title"), item["mime"],
                         item["bytes"], w.now, w.now))
                    changed.append(id_)
                    continue
                source = "share" if "share" in (before[0], item["source"]) else item["source"]
                after = (source, before[1] or message_id, turn_id or before[2],
                         item["bytes"] if item["bytes"] is not None else before[3], item["mime"] or before[4],
                         item.get("title") or before[5])
                if after == tuple(before) and item["kind"] == "branch":
                    continue                # a branch read again, unchanged: nothing new
                w.execute("UPDATE made SET source = ?, message_id = ?, turn_id = ?, bytes = ?, mime = ?, title = ?, "
                          "seq = ?, updated_at = ?, thread_ts = coalesce(thread_ts, ?) WHERE id = ?",
                          (*after, seq, w.now, thread_ts, id_))
                changed.append(id_)
            if tie_turn and message_id:     # what the turn shared before its reply now points at the reply
                for (id_,) in w.execute("SELECT id FROM made WHERE turn_id = ? AND message_id IS NULL",
                                        (turn_id,)).fetchall():
                    w.execute("UPDATE made SET message_id = ? WHERE id = ?", (message_id, id_))
                    changed.append(id_)
            if changed:
                _announce(w, sorted(set(changed)), chat_id, thread_ts)
        await self._engine().write(write)

    # ── copies before a worktree goes ────────────────────────────────────

    async def before_removal(self, chat_id: str, thread_ts: str, tree: Path) -> None:
        """Copy what the thread shared from ``tree`` to the project's
        ``.xo/made/thread-<ts>/``; failures are logged, never raised (the
        removal goes on, and the item says it no longer exists)."""
        engine = self._engine()
        where = trees.thread_of(tree)
        if where is None:
            return
        project = where[0]
        prefix = os.path.normpath(str(tree)) + os.sep
        rows = await engine.store.read(lambda conn: conn.execute(
            "SELECT id, path FROM made WHERE chat_id = ? AND source = 'share' AND copy_path IS NULL AND path LIKE ? "
            "ESCAPE '\\'", (chat_id, _like_prefix(prefix))).fetchall())
        copied: list[tuple[str, str]] = []
        for id_, path in rows:
            rel = path[len(prefix):]
            target = project / XO_DIRNAME / MADE_DIRNAME / f"{trees.TREE_PREFIX}{thread_ts}" / rel
            try:
                if await asyncio.to_thread(_copy, Path(path), target, project):
                    copied.append((id_, str(target)))
            except OSError as exc:
                log.warning("storm made: couldn't copy %s before its worktree went: %s", path, exc)
        if not copied:
            return

        def write(w: Writes) -> None:
            for id_, target in copied:
                w.execute("UPDATE made SET copy_path = ? WHERE id = ?", (target, id_))
            _announce(w, [i for i, _ in copied], chat_id, thread_ts)
        await engine.write(write)

    # ── the person's view ────────────────────────────────────────────────

    async def listing(self, chat: Mapping[str, Any], *, since: str | None = None, before: str | None = None,
                      thread: str | None = None, pinned: bool | None = None,
                      limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """The chat's items, the most recently made or touched first."""
        engine = self._engine()
        since, before = _when(since, "since"), _when(before, "before")
        limit = max(1, min(limit, MAX_LIMIT))

        def read(conn) -> tuple[list[tuple], dict[str, dict]]:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM made WHERE chat_id = :chat AND (:thread IS NULL OR thread_ts = :thread) "
                "AND (:since IS NULL OR updated_at >= :since) AND (:before IS NULL OR updated_at < :before) "
                "AND (:pinned IS NULL OR (pinned_at IS NOT NULL) = :pinned) "
                "ORDER BY updated_at DESC, seq DESC LIMIT :limit",
                {"chat": chat["id"], "thread": thread, "since": since, "before": before,
                 "pinned": None if pinned is None else int(pinned), "limit": limit}).fetchall()
            git = {t: json.loads(s) for t, s in conn.execute(
                "SELECT thread_ts, state FROM thread_git WHERE chat_id = ?", (chat["id"],)).fetchall()}
            return found, git
        found, git = await engine.store.read(read)
        return await asyncio.to_thread(lambda: [_located(_view(r), git) for r in found])

    async def pin(self, chat: Mapping[str, Any], made_id: str, pinned: bool) -> dict[str, Any]:
        engine = self._engine()

        def write(w: Writes) -> tuple | None:
            row = w.execute("SELECT pinned_at FROM made WHERE id = ? AND chat_id = ?", (made_id, chat["id"])).fetchone()
            if row is None:
                return None
            if (row[0] is not None) != pinned:
                w.execute("UPDATE made SET pinned_at = ? WHERE id = ?", (w.now if pinned else None, made_id))
                _announce(w, [made_id], chat["id"], _rows(w, [made_id])[0][3])
            return _rows(w, [made_id])[0]
        row = await engine.write(write)
        if row is None:
            raise MadeRefused("made_not_found", f"This chat has no made item {made_id!r}.", status=404)
        git = await engine.store.read(lambda conn: {t: json.loads(s) for t, s in conn.execute(
            "SELECT thread_ts, state FROM thread_git WHERE chat_id = ? AND thread_ts IS ?",
            (chat["id"], row[3])).fetchall()})
        return await asyncio.to_thread(_located, _view(row), git)

    # ── plumbing ─────────────────────────────────────────────────────────

    def _engine(self) -> Engine:
        if self.engine is None:
            raise MadeRefused("made_unavailable", "Made isn't running in this server yet; retry shortly.", status=503)
        return self.engine

    def _spawn(self, chat_id: str, coro: Awaitable[Any]) -> None:
        async def serially() -> None:
            async with self._locks.setdefault(chat_id, asyncio.Lock()):
                await coro
        task = asyncio.ensure_future(serially())
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm made: recording what a thread made failed", exc_info=task.exception())


def _announce(w: Writes, ids: list[str], chat_id: str, thread_ts: str | None) -> None:
    """One ``made {items}`` event for the rows the transaction changed; their
    ``seq`` becomes its seq."""
    seq = w.append(EVENT, {"items": [_view(r) for r in _rows(w, ids)]}, chat_id=chat_id, thread_ts=thread_ts)
    marks = ", ".join("?" * len(ids))
    w.execute(f"UPDATE made SET seq = ? WHERE id IN ({marks})", (seq, *ids))


def _rows(conn, ids: Iterable[str]) -> list[tuple]:
    ids = list(ids)
    marks = ", ".join("?" * len(ids))
    return conn.execute(f"SELECT {_COLUMNS} FROM made WHERE id IN ({marks}) ORDER BY updated_at DESC, seq DESC",
                        ids).fetchall() if ids else []


def _view(row: Sequence[Any]) -> dict[str, Any]:
    (id_, _seq, chat_id, thread_ts, message_id, turn_id, kind, source, path, url, name, title, mime, size, copy_path,
     created_at, updated_at, pinned_at) = row
    return {"id": id_, "chat_id": chat_id, "kind": kind, "source": source, "path": path, "url": url,
            "name": name, "title": title, "mime": mime, "bytes": size, "thread_ts": thread_ts, "message_id": message_id,
            "turn_id": turn_id, "copy_path": copy_path, "created_at": created_at, "updated_at": updated_at,
            "pinned": pinned_at is not None, "pinned_at": pinned_at}


def _located(view: dict[str, Any], git: Mapping[str, dict]) -> dict[str, Any]:
    """Where the item is now (blocking): ``exists``, ``where``, and its
    ``project_id`` and ``relative_path`` when a file lives in a project."""
    out = {**view, "exists": True, "where": None, "project_id": None, "relative_path": None}
    if view["kind"] == "link":
        return out
    if view["kind"] == "branch":
        state = git.get(view["thread_ts"]) or {}
        out.update(exists=bool(state.get("branch_exists")), git={k: state.get(k) for k in (
            "merged", "merged_at", "ahead", "behind", "commits", "pr", "upstream")} if state else None)
        return out
    candidates = [("made", view["path"]), ("copy", view["copy_path"])]
    where = _tree_of(Path(view["path"]))
    if where is not None:
        project, tree = where
        candidates.append(("project", str(project / Path(view["path"]).relative_to(tree))))
    for label, candidate in candidates:
        if candidate and os.path.isfile(candidate):
            out.update(where=label, **address(Path(candidate)))
            try:
                out["bytes"] = os.path.getsize(candidate)
            except OSError:
                pass
            return out
    out.update(exists=False, **address(Path(view["path"])))
    return out


def _tree_of(path: Path) -> tuple[Path, Path] | None:
    """``(project, tree)`` when ``path`` is inside an XO thread's worktree."""
    for parent in path.parents:
        where = trees.thread_of(parent)
        if where is not None:
            return where[0], parent
    return None


def address(path: Path) -> dict[str, Any]:
    """``project_id`` and ``relative_path`` for a file under the projects root
    (both None elsewhere): its address on ``/api/xo-projects/{p}/…``."""
    root = Path(os.path.realpath(project_layout.xo_projects_root()))
    try:
        rel = Path(os.path.realpath(path)).relative_to(root)
    except ValueError:
        try:
            rel = path.relative_to(root)
        except ValueError:
            return {"project_id": None, "relative_path": None}
    parts = rel.parts
    if len(parts) < 2:
        return {"project_id": None, "relative_path": None}
    return {"project_id": parts[0], "relative_path": "/".join(parts[1:])}


def _copy(source: Path, target: Path, project: Path) -> bool:
    """Copy one shared file under ``.xo/made/`` (blocking). Only a regular
    file inside the project, under ``COPY_MAX_BYTES``, is copied."""
    real = Path(os.path.realpath(source))
    root = Path(os.path.realpath(project))
    if root not in real.parents or not real.is_file() or real.stat().st_size > COPY_MAX_BYTES:
        return False
    made = project / XO_DIRNAME / MADE_DIRNAME
    made.mkdir(parents=True, exist_ok=True)
    create_file_exclusive(made / XO_GITIGNORE, GITIGNORE_TEXT.encode("utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real, target)
    return True


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _when(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    parsed = parse_ts(value)
    if parsed is None:
        raise MadeRefused(f"bad_{name}", f"{name} must be an ISO-8601 time, not {value[:40]!r}.", status=400)
    return iso(parsed)
