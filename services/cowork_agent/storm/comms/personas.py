"""The agent tools, for every role (plan B4), as every role has the job tools.

* ``create_persona`` only asks: it opens an ``approval`` holding the whole
  file in ``detail.create_persona`` (no new item kind, so no schema change,
  as ``manage_runtime``'s ``detail.runtime_action``), and nothing is written
  until the person approves it. A new name shows the file; an existing one
  shows the changes and keeps the version it was proposed against, so the
  save (``save_allowed``) never overwrites a newer edit silently. Agents
  can't write ``.xo/agents/`` themselves: an agent steers future runs, so
  the card is the only way in. A file with mistakes, or holding a secret
  (the item is stored redacted, so what the person approves would differ),
  is refused at once.
* ``list_personas`` reads the agents a project sees, its own and the
  Space's (``storm.personas.listing``).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from typing import Any, Mapping

from services.cowork_agent.storm import personas
from services.cowork_agent.storm.chats import rows
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall, current_turn
from services.cowork_agent.storm.comms.jobs import _own_project
from services.cowork_agent.storm.comms.tools import ID_MAX, WHY_MAX
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.redact import redact
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.tiers import Reason, Tier
from services.project_files import ops, read, save
from services.project_files.address import AGENTS_REL, FileRefused

log = logging.getLogger(__name__)

GUIDANCE = "Nothing is saved until the owner approves it; their answer arrives as a new wake. Keep working."
SECRET = ("The agent file holds what looks like a secret. It is stored and shown to the owner, so name its "
          "secrets-store entry instead.")


def _folder(level: str, project: str | None) -> str:
    return personas.SPACE_ID if level == personas.SPACE else project  # type: ignore[return-value]


def _current_text(level: str, project: str | None, name: str) -> tuple[str | None, str | None]:
    """The file's text and version now; (None, None) when there is none."""
    try:
        found = read.read_text(_folder(level, project), personas.rel_path(level, name), purpose="edit")
    except FileRefused as exc:
        if exc.status == 404:
            return None, None
        raise CommsError("persona_unreadable", f"The agent file can't be read now: {exc.message}") from exc
    return found.content, found.version


def _changes(before: str, after: str, path: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))


async def create_persona(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    text = a.text(t.args, "text")
    why = a.text(t.args, "why", required=False, limit=WHY_MAX)
    own = await _own_project(t)
    level = a.choice(t.args, "level", (personas.PROJECT, personas.SPACE)) or \
        (personas.PROJECT if own else personas.SPACE)
    if level == personas.PROJECT and own is None:
        raise CommsError("project_required", "You have no project of your own, so an agent you propose is the "
                                             "Space's: use level \"space\".")
    if redact(text) != text:
        raise CommsError("secret_in_persona", SECRET)
    parsed, problems = personas.parse(text)
    if parsed is None or problems or not personas.NAME_RE.fullmatch(parsed.name or ""):
        found = "; ".join(f"line {p.line}: {p.message}" if p.line else p.message for p in problems) or \
            "name is 1-64 lowercase letters, digits and hyphens, starting with a letter or digit."
        raise CommsError("invalid_persona", f"The agent file has mistakes to fix first: {found}")
    if level == personas.PROJECT:
        t.chats.writable(await t.chats.chat(caller.chat_id))   # a retired project gets no new agents
    name, project = parsed.name, own if level == personas.PROJECT else None
    path = personas.rel_path(level, name)
    before, version = await asyncio.to_thread(_current_text, level, project, name)
    if before == text:
        raise CommsError("persona_unchanged", f"The agent {name!r} already reads exactly this.")
    where = f"project {project}" if project else "the Space"
    title = (f"Change the agent “{name}” in {where}" if before is not None else
             f"Add the agent “{name}” to {where}")
    proposal = {"name": name, "level": level, "project": project, "path": path, "file": text,
                "base_version": version, **({"changes": _changes(before, text, path)} if before is not None else {})}

    def fn(w: Writes) -> dict[str, Any]:
        item = t.chats.queue.open_item(w, ItemSpec(
            kind="approval", title=title, origin="agent", session_id=caller.session_id,
            chat_id=caller.chat_id, thread_ts=caller.thread_ts, turn_id=current_turn(w, caller.session_id),
            epoch=caller.epoch, risk=Tier.NOTABLE,
            risk_reasons=(Reason(Tier.NOTABLE, "steers every run that works as this agent"),), why=why,
            detail={"text": why or parsed.description or title, "create_persona": proposal}))
        return t.record(w, {"review_id": item, "state": "open", "guidance": GUIDANCE})
    return await t.engine.write(fn)


def _view(p: personas.Persona) -> dict[str, Any]:
    return {"name": p.name, "level": p.level, "description": p.description, "runtime": p.runtime,
            "model": p.model, "effort": p.effort, "tools": list(p.tools) if p.tools is not None else None,
            "disallowed_tools": list(p.disallowed_tools),
            **({"replaces": p.shadows} if p.shadows else {}), **({"replaced_by": p.shadowed} if p.shadowed else {}),
            **({"problems": [{"line": x.line, "message": x.message} for x in p.problems]} if p.problems else {})}


async def list_personas(t: ToolCall) -> dict[str, Any]:
    project = await _own_project(t)
    try:
        found = await asyncio.to_thread(personas.listing, project)
    except FileRefused as exc:
        raise CommsError("persona_unreadable", f"The agents can't be read now: {exc.message}") from exc
    listed = {"personas": [_view(p) for p in found]}
    return await t.write(lambda w: listed)


# ── Allow ─────────────────────────────────────────────────────────────────────


def _save(detail: Mapping[str, Any]) -> None:
    level, project = detail["level"], detail.get("project")
    if level == personas.PROJECT:
        try:
            ops.mkdir(project, AGENTS_REL)
        except FileRefused as exc:
            if exc.code != "exists":
                raise
    save.save(_folder(level, project), detail["path"], detail["file"], detail.get("base_version"))


async def save_allowed(chats: Any, item: Mapping[str, Any]) -> None:
    """The person approved a proposed agent: save it through the project-files
    code, guarded by the version it was proposed against. A file changed
    since is never overwritten; that, or any refusal, is said where the
    agent asked."""
    detail = item["detail"]["create_persona"]
    try:
        await asyncio.to_thread(_save, detail)
        said = f"The agent “{detail['name']}” is saved ({detail['path']})."
    except FileRefused as exc:
        log.info("storm personas: allowed agent %s not saved: %s", detail.get("name"), exc.code)
        said = (f"The agent “{detail['name']}” wasn't saved: its file changed after it was proposed. Read it "
                "again and propose your change on top of it." if exc.code == "file_changed" else
                f"The agent “{detail['name']}” wasn't saved: {exc.message}")
    if not item.get("chat_id"):
        return
    await chats.engine.write(lambda w: rows.add_message(w, chat_id=item["chat_id"], thread_ts=item.get("thread_ts"),
                                                        author_kind="system", author_id="xo", text=said))
