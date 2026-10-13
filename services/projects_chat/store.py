"""The projects-chat store: chats, messages, threads, cards, sessions, decisions.

Everything is machine-local, under the state root, never in a project's
``.xo/`` (chat content must not sync)::

    ~/.quirq/projects/
    ├── workspace.chat/             the workspace chat (chat_workspace); the
    │   │                              dot keeps it apart from every project key,
    │   │                              which can never hold one
    │   └── …same files as below…
    └── <pid>/chat/                 one project's chat (chat_<pid>)
        ├── chat.json               identity and settings
        ├── messages.jsonl             the timeline, one message per line
        ├── threads/<ts>/
        │   ├── thread.json            the thread: title, session, worktree, state
        │   ├── messages.jsonl         messages written inside the thread
        │   ├── status.md              the thread's live checklist (update_status)
        │   ├── card.json              the latest status card
        │   └── cards.jsonl            every card, in order
        └── router/decisions.jsonl     one line per routing decision

Sessions (a coordinator incarnation or a thread's agent) live beside the
session index they belong to, one folder each, the shape Claude Code gives
its own sessions: ``<sessions dir>/<cse>/session.json`` + ``events.jsonl``.
The workspace coordinator runs in the projects root, so its sessions sit in
the Space's ``~/.quirq/sessions/``.

A chat is addressed by project folder name, or ``None`` for the
workspace. Files follow the state-root rules: a ``schema`` number on every
document, ``ts`` and ``type`` first on every line, times in UTC ending in
``Z``, project records keyed by ``pid``. The agent's own transcript is never
read or copied here: history is what this store holds.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from services.cowork_agent import project_layout
from services.errors import ServiceError
from services.storage import flock
from services.storage.atomic_write import append_jsonl, write_json_atomic
from services.storage.layout import projects_dir, sessions_dir
from services.storage.reader import read_json

log = logging.getLogger(__name__)

SCHEMA = 1
WORKSPACE_CHAT_ID = "chat_workspace"
CHAT_SUBDIR = "chat"
#: The workspace chat's folder in ``projects/``. Runtime keys can never
#: contain a dot, so no project folder can take this name.
WORKSPACE_CHAT_DIR = "workspace.chat"

MESSAGE_ID = re.compile(r"^cmsg_[A-Za-z0-9_-]{1,128}$")
SESSION_ID = re.compile(r"^cse_[A-Za-z0-9]{8,64}$")

ROLES = ("workspace-coordinator", "project-coordinator", "thread")
AUTHOR_KINDS = ("owner", "agent", "system")
THREAD_STATES = ("working", "needs_reply", "needs_approval", "done", "failed", "resolved")


class StoreError(ServiceError):
    """A projects-chat record is missing or a value is not acceptable."""


# ── time and ids ──────────────────────────────────────────────────────────────

def now_ms_iso() -> str:
    """UTC with milliseconds, ending in ``Z`` (the rule for event lines)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


_id_lock = threading.Lock()
_last_ms = 0
_counter = 0


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def new_message_id() -> str:
    """``cmsg_<time base36><counter><random>``: sorts by creation within a process."""
    global _last_ms, _counter
    with _id_lock:
        ms = int(time.time() * 1000)
        if ms == _last_ms:
            _counter += 1
        else:
            _last_ms, _counter = ms, 0
        stamp = _base36(ms).rjust(9, "0") + _base36(_counter).rjust(3, "0")
    return f"cmsg_{stamp}{secrets.token_hex(4)}"


def new_session_id() -> str:
    return "cse_" + secrets.token_hex(12)


# ── chats ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Chat:
    """A resolved chat: its id, files folder, and the project it belongs to."""

    id: str
    dir: Path
    project: Optional[str]  # folder name; None for the workspace
    pid: Optional[str]

    @property
    def kind(self) -> str:
        return "workspace" if self.project is None else "project"


def chat(project: Optional[str], *, create: bool = False) -> Chat:
    """The workspace chat (``project=None``) or a project's chat.

    Raises :class:`StoreError` for a project with no folder under the
    projects root.
    """
    if not project:
        return Chat(WORKSPACE_CHAT_ID, projects_dir() / WORKSPACE_CHAT_DIR, None, None)
    runtime = project_layout.runtime_dir_for_project(project, create=create)
    if runtime is None:
        raise StoreError("no_such_project", f"There is no project called {project!r}.", status=404)
    meta = project_layout.load_project(project) or {}
    pid = str(meta["pid"]) if meta.get("pid") and not meta.get("_template") else runtime.name
    return Chat(f"chat_{runtime.name}", runtime / CHAT_SUBDIR, project, pid)


def ensure_chat(project: Optional[str]) -> dict:
    """Create the chat's ``chat.json`` if missing; return it."""
    ch = chat(project, create=True)
    path = ch.dir / "chat.json"
    with flock.locked(path):
        doc = read_json(path)
        if isinstance(doc, dict) and doc.get("id") == ch.id:
            return doc
        doc = {
            "schema": SCHEMA,
            "id": ch.id,
            "kind": ch.kind,
            "project_id": ch.project,
            "pid": ch.pid,
            "created_at": now_ms_iso(),
            "settings": {},
        }
        write_json_atomic(path, doc)
    return doc


def read_chat(project: Optional[str]) -> Optional[dict]:
    try:
        ch = chat(project)
    except StoreError:
        return None
    doc = read_json(ch.dir / "chat.json")
    return doc if isinstance(doc, dict) else None


# ── messages ──────────────────────────────────────────────────────────────────

def _messages_path(ch: Chat, thread_ts: Optional[str]) -> Path:
    if thread_ts:
        return _thread_dir(ch, thread_ts) / "messages.jsonl"
    return ch.dir / "messages.jsonl"


def _identity(ch: Chat) -> dict:
    return {"pid": ch.pid, "project_id": ch.project} if ch.project else {}


def post_message(
    project: Optional[str],
    *,
    author_kind: str,
    author_id: str,
    text: str,
    thread_ts: Optional[str] = None,
    author_session_id: Optional[str] = None,
    forwarded_from: Optional[dict] = None,
    in_reply_to: Optional[str] = None,
) -> dict:
    """Append one message to the timeline, or to a thread; return the row."""
    if author_kind not in AUTHOR_KINDS:
        raise StoreError("invalid_author", f"author_kind must be one of {', '.join(AUTHOR_KINDS)}.")
    if not isinstance(text, str) or not text.strip():
        raise StoreError("empty_message", "A message needs some text.")
    ch = chat(project, create=True)
    if thread_ts and get_thread(project, thread_ts) is None:
        raise StoreError("no_such_thread", f"There is no thread {thread_ts!r} here.", status=404)
    row: dict[str, Any] = {"ts": now_ms_iso(), "type": "message", "schema": SCHEMA, **_identity(ch)}
    row.update({
        "id": new_message_id(),
        "chat_id": ch.id,
        "where": "thread" if thread_ts else "timeline",
        "thread_ts": thread_ts,
        "author_kind": author_kind,
        "author_id": author_id,
        "author_session_id": author_session_id,
        "text": text,
        "written_at": row["ts"],
    })
    if forwarded_from:
        row["forwarded_from"] = forwarded_from
    if in_reply_to:
        row["in_reply_to"] = in_reply_to
    path = _messages_path(ch, thread_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, [row])
    return row


def _read_lines(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue  # a torn or hand-edited line is skipped, never fatal
        if isinstance(line, dict):
            out.append(line)
    return out


def add_reaction(project: Optional[str], message_id: str, *, emoji: str, by: str,
                 thread_ts: Optional[str] = None, remove: bool = False) -> dict:
    """Record a reaction (or its removal) as its own line beside the messages."""
    ch = chat(project, create=True)
    line = {"ts": now_ms_iso(), "type": "reaction.removed" if remove else "reaction", "schema": SCHEMA,
            **_identity(ch), "message_id": message_id, "emoji": emoji[:32], "by": by}
    path = _messages_path(ch, thread_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, [line])
    return line


def read_messages(project: Optional[str], *, thread_ts: Optional[str] = None,
                  after: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
    """Messages oldest first; ``after`` keeps those with a later id, ``limit`` the newest N."""
    try:
        ch = chat(project)
    except StoreError:
        return []
    rows = [r for r in _read_lines(_messages_path(ch, thread_ts)) if r.get("type") == "message"]
    rows.sort(key=lambda r: (r.get("ts") or "", r.get("id") or ""))
    if after:
        rows = [r for r in rows if (r.get("id") or "") > after]
    return rows[-limit:] if limit else rows


def get_message(project: Optional[str], message_id: str) -> Optional[dict]:
    """A message by id, from the timeline or any thread of the chat."""
    if not isinstance(message_id, str) or not MESSAGE_ID.match(message_id):
        return None
    for row in read_messages(project):
        if row.get("id") == message_id:
            return row
    for thread in list_threads(project):
        for row in read_messages(project, thread_ts=thread["ts"]):
            if row.get("id") == message_id:
                return row
    return None


# ── threads ───────────────────────────────────────────────────────────────────

def _thread_dir(ch: Chat, ts: str) -> Path:
    if not isinstance(ts, str) or not MESSAGE_ID.match(ts):
        raise StoreError("invalid_thread", f"{ts!r} is not a thread id.")
    return ch.dir / "threads" / ts


def create_thread(project: Optional[str], *, root_id: str, title: str, session_id: str,
                  created_by_session: Optional[str] = None, cwd: Optional[str] = None,
                  branch: Optional[str] = None, worktree_path: Optional[str] = None) -> dict:
    """Root a thread on a timeline message. Raises if the message is unknown
    or already roots a thread."""
    ch = chat(project, create=True)
    root = next((r for r in read_messages(project) if r.get("id") == root_id), None)
    if root is None:
        raise StoreError("no_such_message", f"There is no timeline message {root_id!r}.", status=404)
    path = _thread_dir(ch, root_id) / "thread.json"
    with flock.locked(path):
        if read_json(path) is not None:
            raise StoreError("already_running", "That message already has a thread.", status=409)
        doc = {
            "schema": SCHEMA, **_identity(ch),
            "ts": root_id,
            "chat_id": ch.id,
            "title": title.strip()[:120] or "Thread",
            "label": None,
            "session_id": session_id,
            "created_by_session": created_by_session,
            "cwd": cwd,
            "branch": branch,
            "worktree_path": worktree_path,
            "state": "working",
            "resolved_at": None,
            "outputs": {"prs": [], "files": []},
            "created_at": now_ms_iso(),
            "last_activity_at": now_ms_iso(),
        }
        write_json_atomic(path, doc)
    return doc


def get_thread(project: Optional[str], ts: str) -> Optional[dict]:
    try:
        ch = chat(project)
        doc = read_json(_thread_dir(ch, ts) / "thread.json")
    except StoreError:
        return None
    return doc if isinstance(doc, dict) else None


def update_thread(project: Optional[str], ts: str, **fields: Any) -> dict:
    """Merge ``fields`` into ``thread.json`` (``state`` is checked)."""
    if "state" in fields and fields["state"] not in THREAD_STATES:
        raise StoreError("invalid_state", f"state must be one of {', '.join(THREAD_STATES)}.")
    ch = chat(project)
    path = _thread_dir(ch, ts) / "thread.json"
    with flock.locked(path):
        doc = read_json(path)
        if not isinstance(doc, dict):
            raise StoreError("no_such_thread", f"There is no thread {ts!r} here.", status=404)
        doc.update(fields)
        doc["last_activity_at"] = now_ms_iso()
        write_json_atomic(path, doc)
    return doc


def list_threads(project: Optional[str]) -> list[dict]:
    """Every thread of the chat, most recently active first."""
    try:
        ch = chat(project)
    except StoreError:
        return []
    base = ch.dir / "threads"
    out = []
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        doc = read_json(folder / "thread.json")
        if isinstance(doc, dict):
            card = read_json(folder / "card.json")
            out.append({**doc, "card": card if isinstance(card, dict) else None})
    out.sort(key=lambda t: t.get("last_activity_at") or "", reverse=True)
    return out


def write_status(project: Optional[str], ts: str, text: str) -> None:
    """The thread's live checklist (``update_status``), replaced whole."""
    ch = chat(project)
    path = _thread_dir(ch, ts) / "status.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_status(project: Optional[str], ts: str) -> Optional[str]:
    try:
        return (_thread_dir(chat(project), ts) / "status.md").read_text(encoding="utf-8")
    except (OSError, StoreError):
        return None


def get_card(project: Optional[str], ts: str) -> Optional[dict]:
    try:
        doc = read_json(_thread_dir(chat(project), ts) / "card.json")
    except StoreError:
        return None
    return doc if isinstance(doc, dict) else None


def write_card(project: Optional[str], ts: str, card: dict) -> dict:
    """The thread's status card: latest in ``card.json``, history in ``cards.jsonl``."""
    ch = chat(project)
    folder = _thread_dir(ch, ts)
    stamped = {"schema": SCHEMA, **_identity(ch), "thread_ts": ts, **card, "updated_at": now_ms_iso()}
    write_json_atomic(folder / "card.json", stamped)
    append_jsonl(folder / "cards.jsonl", [{"ts": stamped["updated_at"], "type": "card", **{
        k: v for k, v in stamped.items() if k not in ("updated_at",)}}])
    return stamped


# ── routing decisions ─────────────────────────────────────────────────────────

def record_decision(project: Optional[str], decision: dict) -> None:
    """Append one routing decision (the router's :class:`Decision`, as a dict)."""
    ch = chat(project, create=True)
    path = ch.dir / "router" / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, [{"ts": now_ms_iso(), "type": "router.decision", **_identity(ch), **decision}])


def read_decisions(project: Optional[str], limit: Optional[int] = None) -> list[dict]:
    try:
        ch = chat(project)
    except StoreError:
        return []
    rows = _read_lines(ch.dir / "router" / "decisions.jsonl")
    return rows[-limit:] if limit else rows


# ── sessions ──────────────────────────────────────────────────────────────────

def _sessions_root(project: Optional[str], *, create: bool = False) -> Path:
    if not project:
        return sessions_dir()
    root = project_layout.runtime_dir_for_project(project, create=create)
    if root is None:
        raise StoreError("no_such_project", f"There is no project called {project!r}.", status=404)
    return root / project_layout.RUNTIME_SESSIONS_SUBDIR


def _session_dir(project: Optional[str], session_id: str, *, create: bool = False) -> Path:
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        raise StoreError("invalid_session", f"{session_id!r} is not a session id.")
    return _sessions_root(project, create=create) / session_id


def create_session(project: Optional[str], *, role: str, cwd: str,
                   thread_ts: Optional[str] = None, incarnation: Optional[int] = None) -> dict:
    """A new agent session record (a coordinator incarnation or a thread's agent).

    It carries a comms token (only its hash is stored) and the UUID the agent
    CLI will use as its own session id. Returns the record plus ``token``,
    which is shown once and never written to disk.
    """
    if role not in ROLES:
        raise StoreError("invalid_role", f"role must be one of {', '.join(ROLES)}.")
    ch = chat(project, create=True)
    session_id = new_session_id()
    token_secret = secrets.token_urlsafe(24)
    record = {
        "schema": SCHEMA, **_identity(ch),
        "id": session_id,
        "role": role,
        "chat_id": ch.id,
        "thread_ts": thread_ts,
        "agent_session_uuid": str(uuid.uuid4()),
        "cwd": cwd,
        "epoch": 0,
        "status": "idle",
        "incarnation": incarnation,
        "token_sha256": _digest(token_secret),
        "turns": 0,
        "created_at": now_ms_iso(),
        "updated_at": now_ms_iso(),
        "retired_at": None,
    }
    folder = _session_dir(project, session_id, create=True)
    folder.mkdir(parents=True, exist_ok=True)
    write_json_atomic(folder / "session.json", record)
    append_session_event(project, session_id, "session.created", role=role, thread_ts=thread_ts)
    return {**record, "token": f"{session_id}.{token_secret}"}


def issue_token(project: Optional[str], session_id: str) -> str:
    """A fresh comms token for a session's next turn; the previous one stops working."""
    secret = secrets.token_urlsafe(24)
    update_session(project, session_id, token_sha256=_digest(secret))
    return f"{session_id}.{secret}"


def _digest(secret: str) -> str:
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def get_session(project: Optional[str], session_id: str) -> Optional[dict]:
    try:
        doc = read_json(_session_dir(project, session_id) / "session.json")
    except StoreError:
        return None
    return doc if isinstance(doc, dict) else None


def update_session(project: Optional[str], session_id: str, **fields: Any) -> dict:
    path = _session_dir(project, session_id) / "session.json"
    with flock.locked(path):
        doc = read_json(path)
        if not isinstance(doc, dict):
            raise StoreError("no_such_session", f"There is no session {session_id!r}.", status=404)
        doc.update(fields)
        doc["updated_at"] = now_ms_iso()
        write_json_atomic(path, doc)
    return doc


def append_session_event(project: Optional[str], session_id: str, event_type: str, **fields: Any) -> None:
    path = _session_dir(project, session_id) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, [{"ts": now_ms_iso(), "type": event_type, **fields}])


def read_session_events(project: Optional[str], session_id: str) -> list[dict]:
    try:
        return _read_lines(_session_dir(project, session_id) / "events.jsonl")
    except StoreError:
        return []


def list_sessions(project: Optional[str], *, role: Optional[str] = None) -> list[dict]:
    try:
        root = _sessions_root(project)
    except StoreError:
        return []
    out = []
    for folder in sorted(root.glob("cse_*")) if root.is_dir() else []:
        doc = read_json(folder / "session.json")
        if isinstance(doc, dict) and (role is None or doc.get("role") == role):
            out.append(doc)
    return out


def resolve_token(token: str) -> Optional[tuple[Optional[str], dict]]:
    """``(project, session record)`` for a comms token, or ``None``.

    The token is ``<cse>.<secret>``; the session is looked up in the workspace
    and every project, and the secret compared in constant time.
    """
    if not isinstance(token, str) or "." not in token:
        return None
    session_id, secret = token.split(".", 1)
    if not SESSION_ID.match(session_id) or not secret:
        return None
    for project in _candidate_projects():
        doc = get_session(project, session_id)
        if doc and doc.get("retired_at") is None and hmac.compare_digest(
                str(doc.get("token_sha256") or ""), _digest(secret)):
            return project, doc
    return None


def _candidate_projects() -> Iterator[Optional[str]]:
    yield None
    root = project_layout.xo_projects_root()
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        names = []
    yield from names
