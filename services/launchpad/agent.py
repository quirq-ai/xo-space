"""Ask an agent to deploy: a project thread that prepares the project.

``POST /api/deploy/{project}/agent`` opens one thread in the project's chat
the way a job's run does (``storm/jobs/runs.py``): one transaction writes its
root message (the owner's request from the Deploy panel), the thread and its
session, and its first wake. The wake quotes that message and adds XO's
brief, built from flow.json and the project's check, as a system note.

The agent only prepares: a Dockerfile, the port, a launchpad.json without an
app, committed on its thread's branch. It never deploys and never holds a
key; once its work is merged the owner presses Deploy and the script does
the rest. While a deploy thread of the project is still open, asking again
returns that thread instead of starting a second one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from services.launchpad import launchpad, runs

TITLE = "Deploy to Launchpad"
REQUEST = "Prepare this project for deployment to XO Launchpad."
CLIENT_PREFIX = "launchpad:"
#: A thread's states once it has finished; any other is still open.
FINISHED = ("done", "failed", "resolved")


class _Exists(Exception):
    """The request's client id already opened a thread."""


def _open_thread(conn, chat_id: str) -> str | None:
    """The project's latest deploy thread, while it is still open."""
    row = conn.execute("SELECT t.ts, t.state, t.resolved_at FROM threads t JOIN messages m ON m.id = t.ts "
                       "WHERE t.chat_id = ? AND m.client_id LIKE ? ORDER BY m.seq DESC LIMIT 1",
                       (chat_id, CLIENT_PREFIX + "%")).fetchone()
    if row is None or row[1] in FINISHED or row[2]:
        return None
    return row[0]


def _by_client(conn, chat_id: str, key: str) -> str | None:
    row = conn.execute("SELECT id FROM messages WHERE chat_id = ? AND client_id = ?", (chat_id, key)).fetchone()
    return row[0] if row else None


def brief(checked: dict[str, Any]) -> str:
    """XO's note to the agent: the rules from flow.json and this project's check."""
    flow = launchpad.flow()
    agent, config = flow["agent"], flow["config"]
    state = checked["state"]
    fields = checked["fields"]
    port = fields["port"]
    lines = [
        "XO Space's Deploy panel started this thread. Prepare this project so the Space's deploy script can "
        "deploy it to XO Launchpad. The xo-deploy skill has the same rules.",
        "",
        agent["role"],
        "",
        "The project's check (files only) says:",
        f"- state: {state}: {flow['states'][state]['means']}",
    ]
    lines += [f"- why: {reason}" for reason in checked.get("reasons") or []]
    lines += [f"- blocked: {blocker}" for blocker in checked.get("blockers") or []]
    lines += [f"- warning: {warning}" for warning in checked.get("warnings") or []]
    lines += [
        f"- app name: {fields['name']['value']} (from {fields['name']['source']})",
        f"- port: {port['value'] if port['value'] else 'unknown'}"
        + (f" (from {port['source']}{'' if port.get('confident') else ', a guess'})" if port["value"] else ""),
        f"- health path: {fields['health_path']['value'] or 'none (a port check)'}",
        "- env names: " + (", ".join(f"{e['name']}{' (sensitive)' if e['sensitive'] else ''}"
                                     for e in fields["env"]) or "none found"),
    ]
    build = checked.get("build") or {}
    if build.get("dockerfile"):
        lines.append("- build: the Dockerfile at the root")
    elif build.get("template"):
        lines.append(f"- build: no Dockerfile; the script would generate a {build['template']} one. Write a proper "
                     "Dockerfile instead when that template does not fit the app.")
    if checked.get("config_error"):
        lines.append(f"- launchpad.json is invalid: {checked['config_error']}")
    lines += ["", "Requirements:"] + [f"- {r}" for r in flow["requirements"]]
    agent_fields = {k: v for k, v in config["fields"].items() if k in ("schema", "name", "port", "health_path", "env")}
    example = {k: v for k, v in config["example"].items() if k in agent_fields}
    lines += ["", f"{config['file']}, at the project root and committed: {config['rule']} You write only these "
              "fields:"] + [f"- {k}: {v}" for k, v in agent_fields.items()]
    lines += [f"For example: {json.dumps(example)}", ""]
    lines += ["Do:"] + [f"- {item}" for item in agent["does"]]
    lines += ["", "Never:"] + [f"- {item}" for item in agent["never"]]
    command = agent["check"].format(script=runs.SCRIPT, dir=".")
    lines += ["", f"Check your work from your working folder with `{command}` (it only reads files); aim for "
              "ready, or linked when the project already has an app.",
              f"The whole flow, with every step and failure, is {launchpad.FLOW_PATH}.", "",
              f"When you are done: {agent['finish']}"]
    return "\n".join(lines)


async def ask_agent(name: str, *, note: str | None = None, client_id: str | None = None) -> dict[str, Any]:
    """Open the project's deploy thread, or return the one still open."""
    from services.cowork_agent.storm import chats as storm_chats
    from services.cowork_agent.storm.chats import envelope, rows
    from services.cowork_agent.storm.engine import Wake
    from services.cowork_agent.storm.presets import Intelligence

    path: Path = runs.project_path(name)
    chats = storm_chats.current()
    chat = await chats.project(path.name)
    chats.writable(chat)
    store = chats.engine.store
    key = CLIENT_PREFIX + (client_id or rows.new_id("req"))
    found = await store.read(lambda conn: _by_client(conn, chat["id"], key))
    if found is None:
        found = await store.read(lambda conn: _open_thread(conn, chat["id"]))
    if found is not None:
        return {"chat_id": chat["id"], "thread_ts": found, "already": True}

    checked = await asyncio.to_thread(launchpad.check, path)
    text = REQUEST + (f"\n\n{note.strip()}" if note and note.strip() else "")
    ts = rows.new_id("cmsg")
    note_text = brief(checked)
    made: dict[str, Any] = {}

    def root(w) -> None:
        if _by_client(w, chat["id"], key) is not None:
            raise _Exists
        made["row"] = rows.add_message(w, chat_id=chat["id"], author_kind="owner", author_id="owner", text=text,
                                       client_id=key, message_id=ts,
                                       extra={"launchpad": {"request": "prepare", "state": checked["state"],
                                                            "source": "deploy-panel"}})

    def also(w, session_id: str, _note: str | None) -> dict[str, Any]:
        wake = envelope.wake(reason="mention", chat=chat, trigger=made["row"], now=w.now, role="initiator",
                             system_note=note_text)
        w.wake(session_id, Wake(id=f"launchpad:{ts}", priority="now", reason="message", origin="person",
                                text=wake))
        return {"chat_id": chat["id"], "thread_ts": ts, "already": False, "state": checked["state"]}

    try:
        return await chats.start_thread(chat, ts, TITLE, Intelligence(), also, root=root)
    except _Exists:
        found = await store.read(lambda conn: _by_client(conn, chat["id"], key))
        return {"chat_id": chat["id"], "thread_ts": found, "already": True}
