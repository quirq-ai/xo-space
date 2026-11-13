"""Continue a thread's work in a new thread, on another runtime (mixed-runtime
plan, step 9). A thread never changes runtime in place: its session's memory
is its CLI's own transcript. Moving the work means a new, linked thread.

``continue_thread`` does it in order, and is the one path for every caller
(the coordinator's ``continue_thread`` tool, the owner's route, the usage
card):

1. **Check** the old thread (it exists, isn't already continued) and the
   target runtime (``Chats.choose_runtime``), before anything changes.
2. **Stop** the old thread (``Engine.stop``; no API call, so it works while
   its runtime is at its limit) and wait until it is between inputs.
3. **Snapshot** its work: XO commits the old worktree, untracked files
   included, as one WIP commit on the old thread's branch, and makes the new
   thread's branch and worktree from it (its base stays the old thread's, so
   the diff is the whole work). A project that isn't a git repository has
   no snapshot: the new thread works in the same folder.
4. **Freeze** the old thread: its session is paused (the owner's next
   message there, or ``paused: false``, picks it back up with its memory).
5. **Start** the new thread on a timeline message saying where it came
   from, linked both ways (``thread.continued`` on the old thread, the root
   message's ``extra.continued_from`` on the new one), with a handoff XO
   builds from the store and git. Its first turn is a checkpoint: it reports
   what is done and half done, its plan and what it's unsure of, and asks
   the owner before it changes anything.

A failure after the stop leaves the old thread frozen, whatever failed, and
says why on it: the loop breaker's pause is lifted only by the owner's own
message there or an explicit resume (w2-w3 design §6), never quietly. A
chat the founder stopped or paused refuses the new thread
(``IntentRefused``, from the engine's one session seam).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.storm.chats import rows
from services.cowork_agent.storm.chats.envelope import escape_text
from services.cowork_agent.storm.engine import Wake, Writes
from services.cowork_agent.storm.presets import Intelligence
from services.cowork_agent.storm.worktrees import trees
from services.cowork_agent.storm.worktrees.git import git, git_out
from services.errors import ServiceError

log = logging.getLogger(__name__)

EVENT = "thread.continued"
PURPOSE = "continue this thread"
STEPS_IN_HANDOFF = 40
MESSAGES_IN_HANDOFF = 30
DIFFSTAT_MAX = 6000
#: How long the stop may take before the old thread is between inputs.
STOP_WAIT_S = 30.0
WIP_AUTHOR = ("-c", "user.name=XO", "-c", "user.email=xo@localhost")
CHECKPOINT = ("Your first turn is a checkpoint. Don't change any file yet. Read the handoff, the code and the "
              "diff, then reply with: what is done, what is half done, your plan, and what you are unsure of. "
              "Then call ask (to founder, kind approval, blocking true) to confirm before you change anything.")


class ContinueRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(code, message, status=status)


def continued_to(conn, chat_id: str, ts: str) -> str | None:
    """The thread ``ts`` was continued in, if it was."""
    row = conn.execute("SELECT json_extract(data, '$.to') FROM events WHERE type = ? AND chat_id = ? AND "
                       "thread_ts = ? ORDER BY seq DESC LIMIT 1", (EVENT, chat_id, ts)).fetchone()
    return row[0] if row else None


async def continue_thread(chats: Any, chat: Mapping[str, Any], old_ts: str, *, agent: str | None,
                          model: str | None = None, effort: str | None = None, reason: str,
                          instructions: str | None = None, by: str) -> dict[str, Any]:
    """Continue thread ``old_ts`` in a new thread on ``agent`` (None: the
    project's choice). ``by`` is who asked (``owner``, a coordinator's role,
    ``xo``). Returns ``{thread_id, from_thread_id, session_id, agent, wip}``."""
    engine = chats.engine
    chats.writable(chat)

    def found(conn) -> dict[str, Any]:
        thread = rows.get_thread(conn, chat["id"], old_ts)
        if thread is None:
            raise ContinueRefused("thread_not_found", f"There is no thread {old_ts!r} in this chat.", status=404)
        if (to := continued_to(conn, chat["id"], old_ts)) is not None:
            raise ContinueRefused("already_continued", f"Thread {old_ts} was continued in thread {to} already.")
        return thread
    old = await engine.store.read(found)
    old_agent = (await engine.meta(old["session_id"])).agent
    target, _runtime, runtime_note = await chats.choose_runtime(chat, agent, PURPOSE)   # refuses before any change

    await engine.stop(old["session_id"], by=by)
    if not await _between_inputs(engine, old["session_id"]):
        raise ContinueRefused("still_busy", f"Thread {old_ts} didn't stop within {STOP_WAIT_S:g} s; try again.")
    await engine.pause(old["session_id"], reason=f"continued elsewhere ({reason})")
    try:
        return await _continue(chats, chat, old, old_agent, target, runtime_note, model, effort, reason,
                               instructions, by)
    except BaseException as exc:
        why = exc.message if isinstance(exc, ServiceError) else type(exc).__name__
        log.exception("storm chats: continuing thread %s failed; it stays frozen", old_ts)
        await engine.write(lambda w: rows.add_message(
            w, chat_id=chat["id"], thread_ts=old_ts, author_kind="system", author_id="xo",
            text=f"Continuing this thread elsewhere failed ({why}). It stays frozen; write here to pick it back "
                 "up with its own memory."))
        raise


async def _between_inputs(engine: Any, session_id: str) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STOP_WAIT_S
    while True:
        async with engine.between_inputs(session_id) as idle:
            if idle:
                return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def _continue(chats: Any, chat: Mapping[str, Any], old: Mapping[str, Any], old_agent: str, target: str,
                    runtime_note: str | None, model: str | None, effort: str | None, reason: str,
                    instructions: str | None, by: str) -> dict[str, Any]:
    engine = chats.engine
    new_ts = rows.new_id("cmsg")
    wip = await _snapshot(chats, chat, old, new_ts)
    parts = await engine.store.read(lambda conn: _handoff_parts(conn, chat, old))
    title = f"{old['title'] or 'Thread'} (continued)"[:120]
    said = (f"Continuing “{old['title'] or old['ts']}” on {target}: {reason}")

    def root(w: Writes) -> None:
        rows.add_message(w, chat_id=chat["id"], author_kind="system", author_id="xo", message_id=new_ts, text=said,
                         extra={"continued_from": old["ts"]})

    def also(w: Writes, session_id: str, note: str | None) -> dict[str, Any]:
        if wip is not None:
            w.execute("UPDATE threads SET branch = ?, base_commit = ? WHERE chat_id = ? AND ts = ?",
                      (wip["branch"], wip["base"], chat["id"], new_ts))
        w.append(EVENT, {"to": new_ts, "from": old["ts"], "agent": target, "from_agent": old_agent, "reason": reason,
                         "by": by, **({"wip": wip["sha"]} if wip else {})},
                 chat_id=chat["id"], session_id=old["session_id"], thread_ts=old["ts"])
        rows.add_message(w, chat_id=chat["id"], thread_ts=old["ts"], author_kind="system", author_id="xo",
                         text=f"Continued in [{title}](#{new_ts}) on {target}: {reason}. This thread is frozen; "
                              "write here to pick it back up with its own memory.",
                         extra={"continued_in": new_ts})
        w.wake(session_id, Wake(id=f"continue:{old['ts']}", priority="now", reason="continue", origin="system",
                                text=_envelope(chat, old, old_agent, target, reason, instructions, by, wip, parts,
                                               runtime_note, w.now)))
        return {"thread_id": new_ts, "from_thread_id": old["ts"], "session_id": session_id, "agent": target,
                "wip": wip["sha"] if wip else None, **({"note": note} if note else {})}
    return await chats.start_thread(chat, new_ts, title, Intelligence(model, effort), also, agent=target,
                                    root=root, chosen_by=by)


async def _snapshot(chats: Any, chat: Mapping[str, Any], old: Mapping[str, Any], new_ts: str
                    ) -> dict[str, str] | None:
    """Commit the old worktree as one WIP commit and make the new thread's
    branch and worktree from it; None outside git."""
    path = old.get("worktree_path")
    if not path or not Path(path).is_dir() or trees.thread_of(Path(path)) is None:
        return None
    tree = Path(path)
    await git_out(tree, "stage the old thread's work", "add", "-A")
    staged = await git(tree, "diff", "--cached", "--quiet")
    if not staged.ok:                       # something to commit
        await git_out(tree, "commit the old thread's work", *WIP_AUTHOR, "commit", "--no-verify", "-q", "-m",
                      f"WIP: thread {old['ts']} continued in thread {new_ts}")
    sha = (await git_out(tree, "read the WIP commit", "rev-parse", "HEAD")).strip()
    project = chats.folder(chat)
    branch = trees.BRANCH_PREFIX + secrets.token_hex(trees.BRANCH_HEX_BYTES)
    await git_out(project, "make the new thread's branch", "branch", branch, sha)
    base = old.get("base_commit") or sha
    await trees.ensure(project, new_ts, chat_id=chat["id"], branch=branch, base_commit=base)
    log_ = (await git(tree, "log", "--oneline", "-n", "30", f"{base}..{sha}")).output.strip() if base != sha else ""
    stat = (await git(tree, "diff", "--stat", base, sha)).output.strip() if base != sha else ""
    return {"sha": sha, "branch": branch, "base": base, "log": log_, "stat": stat[:DIFFSTAT_MAX]}


def _handoff_parts(conn, chat: Mapping[str, Any], old: Mapping[str, Any]) -> dict[str, Any]:
    brief = conn.execute("SELECT note FROM hops WHERE chat_id = ? AND thread_ts = ? AND kind = 'brief' "
                         "ORDER BY seq LIMIT 1", (chat["id"], old["ts"])).fetchone()
    steps = conn.execute("SELECT json_extract(data, '$.title'), json_extract(data, '$.target') FROM events "
                         "WHERE session_id = ? AND type = 'step.tool' ORDER BY seq DESC LIMIT ?",
                         (old["session_id"], STEPS_IN_HANDOFF)).fetchall()
    return {"root": rows.get_message(conn, old["ts"]), "brief": brief[0] if brief else None,
            "messages": rows.latest_messages(conn, chat_id=chat["id"], thread_ts=old["ts"], limit=MESSAGES_IN_HANDOFF),
            "card": rows.latest_card(conn, chat["id"], old["ts"]), "status": old.get("status_md"),
            "steps": [f"{t}{f' ({g})' if g else ''}" for t, g in reversed(steps) if t]}


def _envelope(chat: Mapping[str, Any], old: Mapping[str, Any], old_agent: str, target: str, reason: str,
              instructions: str | None, by: str, wip: dict[str, str] | None, parts: Mapping[str, Any],
              runtime_note: str | None, now: str) -> str:
    """The new thread's first input, built by XO from the store and git."""
    def tag(name: str, body: str | None, **attrs: Any) -> str:
        if not body:
            return ""
        attr = "".join(f' {k.replace("_", "-")}="{escape_text(str(v))}"' for k, v in attrs.items() if v)
        return f"<{name}{attr}>\n{escape_text(body)}\n</{name}>\n"
    messages = "\n".join(f"[{m['created_at']}] {m['author_kind']}{'/' + m['author_id'] if m.get('author_id') else ''}"
                         f" ({m['id']}): {m['text']}" for m in parts["messages"])
    root = parts["root"]
    out = [f'<continuation from-thread="{old["ts"]}" from-agent="{old_agent}" to-agent="{target}" '
           f'by="{escape_text(by)}" current-time="{now}" project="{escape_text(chat.get("project") or "")}">',
           "You continue another agent's thread. It ran on a different runtime and is now frozen; you start from "
           "what XO kept of it, and from the code.",
           tag("reason", reason),
           tag("instructions", instructions),
           tag("original-request", root["text"] if root else None, id=old["ts"], author=root and root["author_kind"]),
           tag("brief", parts["brief"]),
           tag("thread-messages", messages),
           tag("latest-card", json.dumps(parts["card"], ensure_ascii=False) if parts["card"] else None),
           tag("checklist", parts["status"]),
           tag("steps-taken", "\n".join(parts["steps"])),
           tag("work-in-progress", "\n".join(x for x in (
               f"The old thread's work is committed as {wip['sha']} (WIP) on your branch {wip['branch']}; "
               f"its base is {wip['base']}. Review it with git log and git diff {wip['base'][:12]}..HEAD.",
               wip["log"], wip["stat"]) if x) if wip else "No git snapshot: you work in the same folder."),
           tag("note", runtime_note),
           tag("checkpoint", CHECKPOINT),
           "</continuation>"]
    return "\n".join(x.rstrip("\n") for x in out if x)
