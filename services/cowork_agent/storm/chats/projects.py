"""Projects coming into the workspace (design ch 11 §9; requirement R3).

* **The person allows a ``create_project`` item:** the server scaffolds the
  project through the existing path (``project_layout.scaffold_project``,
  what ``POST /api/files/mkdir {scaffold: true}`` does), with the purpose as
  its description; the workspace chat gets the system note "Project X
  created"; the owner messages the proposal cited are copied into the new
  project's chat and its coordinator is woken with them (and the first task
  as the workspace's note). Only the person creates (I4).
* **Any project made another way** (the UI's Create button, a clone, a
  folder) gets the same note the first time storm sees it, so the workspace
  coordinator meets it on its next wake. The first time storm ever lists the
  workspace, the projects already there are taken in silently.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from services.cowork_agent import project_layout
from services.cowork_agent.storm.chats import Chats, envelope, project_folders, project_pid, router, rows
from services.cowork_agent.storm.engine import Wake, Writes

log = logging.getLogger(__name__)

KNOWN_KEY = "chats.projects_known"


def _note(w: Writes, workspace: Mapping[str, Any], text: str) -> None:
    rows.add_message(w, chat_id=workspace["id"], author_kind="system", author_id="xo", text=text)


async def sync(chats: Chats) -> list[dict[str, Any]]:
    """Every project folder's chat, created the first time it is seen (with
    the workspace's note, except on the very first listing)."""
    engine = chats.engine
    workspace = await chats.workspace()
    names = await asyncio.to_thread(project_folders)
    pids = await asyncio.to_thread(lambda: {n: project_pid(n) for n in names})

    def known(conn) -> list[dict[str, Any]] | None:
        found = {r[3]: rows.chat_view(r) for r in conn.execute(
            f"SELECT {rows.CHAT_COLUMNS} FROM chats WHERE kind = 'project'").fetchall()}
        if not router.read_setting(conn, KNOWN_KEY, False) or any(
                n not in found or found[n]["project_pid"] != (pids[n] or n) for n in names):
            return None
        return [found[n] for n in names]
    if (listed := await engine.store.read(known)) is not None:
        return listed                               # nothing new: no write

    def register(w: Writes) -> list[dict[str, Any]]:
        silent = not router.read_setting(w, KNOWN_KEY, False)
        out = []
        for name in names:
            chat, created = rows.project_chat(w, name=name, pid=pids[name])
            if created and not silent:
                _note(w, workspace, f"Project {name} created.")
            out.append(chat)
        if silent:
            router.write_setting(w, KNOWN_KEY, True)
        return out
    return await engine.write(register)


async def create(chats: Chats, item: Mapping[str, Any]) -> None:
    """The person allowed a proposed project: make it, say so, and hand the
    cited request to its coordinator."""
    detail = item["detail"]
    typed = item["decision"].get("answer") or detail["name"]
    workspace = await chats.workspace()
    try:
        # The folder's name, which may differ from what was typed ("Status Board" → status-board).
        name = await asyncio.to_thread(project_layout.resolve_project_dirname, typed)
        await asyncio.to_thread(project_layout.scaffold_project, name, description=detail.get("purpose"),
                                display_name=typed if typed != name else None)
    except (OSError, ValueError) as exc:
        log.error("storm chats: could not create project %s: %s", typed, exc)
        failed = f"Project {typed} could not be created: {exc}"
        await chats.engine.write(lambda w: _note(w, workspace, failed))
        return
    chat = await chats.project(name)

    def hand_over(w: Writes, coordinator: str) -> None:
        _note(w, workspace, f"Project {name} created.")
        cited = [rows.get_message(w, m) for m in detail.get("context_message_ids") or ()]
        copies = [rows.add_message(w, chat_id=chat["id"], author_kind="owner", author_id=m["author_id"],
                                   text=m["text"], extra={"forwarded_from": {"chat_id": m["chat_id"],
                                                                             "message_id": m["id"],
                                                                             "via": ["workspace"]}})
                  for m in cited if m is not None and m["author_kind"] == "owner"]
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=chat["id"], thread_ts=None, kind="forward",
                     from_role="workspace-coordinator", from_session=item["session_id"],
                     to_role="project-coordinator", to_session=coordinator, via=["workspace"], cause=item["id"],
                     message_ids=[m["id"] for m in copies], note=detail.get("first_task"), wake_id=f"deleg:{hop_id}")
        parts = [envelope.relay(sender_role="workspace-coordinator", sender_session=item["session_id"],
                                note=detail.get("first_task"), reason="new-project", now=w.now, hop=hop_id)] \
            if detail.get("first_task") else []
        parts += [envelope.wake(reason="mention", chat=chat, trigger=copy, now=w.now) for copy in copies]
        if parts:
            w.wake(coordinator, Wake(id=f"deleg:{hop_id}", text="\n\n".join(parts), priority="next",
                                     reason="forward"))
    await chats.with_coordinator(chat, hand_over)
