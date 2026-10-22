"""Sub-agent runs as the routes read and change them (design ch 14 §4, §11;
ch 17 §2.3).

* **List** a session's runs from the store: flat in start order, or as a
  tree to a depth; filtered by state or kind; paged by ``seq`` (the run's
  first ``run.started``). Each row says whether it can be stopped (the
  runtime's ``runs.stop``). A working run's numbers are its last progress,
  kept in memory until it ends. The session's counts come with it ("14
  sub-agents ran").
* **Read** one run with its ancestry: its parents up to the session, the
  session, its chat and thread, for the Workspace › Project › Thread ›
  Sub-agent path. A row that isn't there reads as null.
* **Its transcript**, read from where its CLI keeps it (``read_run``), a
  page at a time, never cached; a tool's output is cut to
  ``TOOL_OUTPUT_MAX_BYTES`` with its size. Redacting known secrets is the
  safety baseline's (PR 17).
* **Stop one**, at once (the Undo is the UI's); its own end event confirms
  it.
* **Counts** for many sessions in one read, for a thread's "live runs".
"""

from __future__ import annotations

import json
import typing
from typing import Any, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import Engine, SessionRefused
from services.cowork_agent.storm.engine.model import Unsupported
from services.cowork_agent.storm.engine.session import native
from services.cowork_agent.storm.runs import records
from services.cowork_agent.storm.runs.live import Progress
from services.errors import ServiceError

KINDS: tuple[str, ...] = typing.get_args(c.RunKind)
#: A tool's output in a transcript page is cut to this many bytes (ch 14 §4.2).
TOOL_OUTPUT_MAX_BYTES = 2048


class RunNotFound(ServiceError):
    def __init__(self, run_id: str) -> None:
        super().__init__("run_not_found", f"There is no sub-agent run {run_id!r}. List a session's runs with "
                                          "GET /api/chat/sessions/{id}/runs.", status=404)


class RunsRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


class TranscriptGone(ServiceError):
    """The CLI no longer keeps the run's transcript; ``summary`` is what the
    store kept of it."""

    def __init__(self, run_id: str, summary: str | None) -> None:
        super().__init__("transcript_gone", f"The CLI no longer keeps {run_id}'s transcript; its summary is what "
                                            "is left.", status=410)
        self.summary = summary


async def list_runs(engine: Engine, session_id: str, *, states: Sequence[str] = (), kinds: Sequence[str] = (),
                    tree: bool = False, depth: int = 1, after_seq: int = 0, before_seq: int | None = None,
                    limit: int = 100) -> dict[str, Any]:
    """The session's runs, oldest first: the ``limit`` after ``after_seq``,
    or, with ``before_seq`` alone, the ``limit`` right before it. ``tree``:
    the page is of the top-level runs, each with its children nested to
    ``depth`` levels in all and every node's ``child_counts``. ``states``
    and ``kinds`` filter the page (the top level, in a tree); ``kinds`` also
    the counts."""
    _check("state", states, records.STATES)
    _check("kind", kinds, KINDS)
    await engine.meta(session_id)
    stoppable = await _stoppable(engine, session_id)
    backwards = before_seq is not None and after_seq == 0

    def read(conn) -> tuple[list, list, dict, dict, list]:
        where = ["session_id = ?", "seq > ?"]
        params: list[Any] = [session_id, after_seq]
        if before_seq is not None:
            where.append("seq < ?")
            params.append(before_seq)
        if tree:
            where.append("parent_run_id IS NULL")
        for column, values in (("state", states), ("kind", kinds)):
            if values:
                where.append(f"{column} IN ({', '.join('?' * len(values))})")
                params.extend(values)
        rows = conn.execute(f"SELECT {records.COLUMNS} FROM runs WHERE {' AND '.join(where)} "
                            f"ORDER BY seq {'DESC' if backwards else 'ASC'} LIMIT ?", (*params, limit)).fetchall()
        rows = rows[::-1] if backwards else rows
        below, children = _subtree(conn, [r[0] for r in rows], depth) if tree else ([], {})
        counts = records.counts(conn, [session_id], kinds=kinds)[session_id]
        return rows, below, children, counts, _live_natives(conn, [session_id], kinds)
    rows, below, children, counts, live = await engine.store.read(read)
    progress = engine.live_progress(session_id)
    views = [_view(r, stoppable, progress) for r in rows]
    if tree:
        views = _nest(views, [_view(r, stoppable, progress) for r in below], children)
    counts["tokens"] += sum(progress[n].tokens for _s, n in live if n in progress)
    full = len(views) == limit
    page = {"session_id": session_id, "runs": views, "counts": counts,
            "next_after_seq": views[-1]["seq"] if full else None}
    if before_seq is not None:                      # read backwards: where the page before this one ends
        page["next_before_seq"] = views[0]["seq"] if full else None
    return page


async def get_run(engine: Engine, run_id: str) -> dict[str, Any]:
    """One run with its ancestry (ch 14 §11.1) and its children's counts."""
    def read(conn) -> tuple | None:
        row = conn.execute(f"SELECT {records.COLUMNS} FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = records.view(row)
        parents = conn.execute(
            "WITH RECURSIVE up(id, hops) AS (SELECT ?, 0 UNION ALL SELECT r.parent_run_id, up.hops + 1 FROM runs r "
            "JOIN up ON r.id = up.id WHERE r.parent_run_id IS NOT NULL) "
            "SELECT r.id, r.title, r.kind, r.depth, r.state FROM runs r JOIN up ON r.id = up.id ORDER BY up.hops DESC",
            (run["parent_run_id"],)).fetchall() if run["parent_run_id"] else []
        where = conn.execute(
            "SELECT s.id, s.role, s.preset, s.agent, s.status, s.chat_id, s.thread_ts, ch.kind, ch.project_pid, "
            "ch.project_name, t.title FROM sessions s LEFT JOIN chats ch ON ch.id = s.chat_id "
            "LEFT JOIN threads t ON t.chat_id = s.chat_id AND t.ts = s.thread_ts WHERE s.id = ?",
            (run["session_id"],)).fetchone()
        children = conn.execute("SELECT count(*), coalesce(sum(state = 'running'), 0), "
                                "coalesce(sum(state = 'waiting'), 0) FROM runs WHERE parent_run_id = ?",
                                (run_id,)).fetchone()
        return row, run["session_id"], parents, where, children
    found = await engine.store.read(read)
    if found is None:
        raise RunNotFound(run_id)
    row, session_id, parents, where, children = found
    view = _view(row, await _stoppable(engine, session_id), engine.live_progress(session_id))
    view["child_counts"] = dict(zip(("total", "running", "waiting"), children))
    view["ancestry"] = _ancestry(parents, where)
    return view


async def read_transcript(engine: Engine, run_id: str, *, after: str | None = None, limit: int = 50) -> dict[str, Any]:
    """A page of the run's own transcript, oldest first, read from where its
    CLI keeps it (read-only, never cached). ``next`` continues it."""
    found = await engine.store.read(lambda conn: conn.execute(
        "SELECT r.session_id, r.native_run_id, r.summary, r.transcript_ref, s.native_id FROM runs r "
        "JOIN sessions s ON s.id = r.session_id WHERE r.id = ?", (run_id,)).fetchone())
    if found is None:
        raise RunNotFound(run_id)
    session_id, native_run, summary, ref, session_native = found
    runtime = await engine.session_runtime(session_id)
    if not native(runtime.features().features, "runs.read"):
        raise Unsupported("runs.read", f"{runtime.name} can't read a sub-agent's transcript.")
    native_id = (json.loads(ref) if ref else {}).get("native_id") or session_native
    if not native_id:
        raise TranscriptGone(run_id, summary)
    try:
        page = await runtime.read_run(native_id, native_run, after=after, limit=limit)
    except c.RuntimeFailure as exc:
        if exc.code == "session_not_found":
            raise TranscriptGone(run_id, summary) from exc
        if exc.code == "protocol":
            raise RunsRefused("bad_after", f"{exc.message}.") from exc
        if exc.code == "unsupported":
            raise Unsupported("runs.read", exc.message) from exc
        raise RunsRefused("runtime_failed", f"Reading the transcript failed ({exc.message}); retry shortly.",
                          status=503) from exc
    return {"run_id": run_id, "items": [_cut_outputs(item) for item in page.items], "next": page.next}


async def stop_run(engine: Engine, run_id: str, *, by: str) -> tuple[dict[str, Any], bool]:
    """Stop one working run. Returns the run and whether a stop was sent
    (False: it had ended already). ``Unsupported`` (501, ``runs.stop``)
    where the runtime can't; ``run_not_live`` (409) when the store shows it
    working but no process of its session runs it."""
    found = await engine.store.read(lambda conn: conn.execute(
        "SELECT session_id, native_run_id FROM runs WHERE id = ?", (run_id,)).fetchone())
    if found is None:
        raise RunNotFound(run_id)
    sent = await engine.stop_run(found[0], found[1], by=by)
    view = await get_run(engine, run_id)
    if not sent and view["state"] in records.LIVE:
        raise RunsRefused("run_not_live", f"{run_id} isn't working in a live process of its session, so there "
                                          "is nothing to stop; it is marked lost once the session settles.",
                          status=409)
    return view, sent


async def stop_all(engine: Engine, session_id: str, *, by: str) -> list[str]:
    """Stop every working sub-agent of a session and leave its root agent
    running (ch 14 §4.3): the stops go out together (``Engine.stop_runs``),
    and each run's end event confirms its stop. Returns the ids of the runs
    a stop was sent to. ``Unsupported`` (501, ``runs.stop``) where the
    runtime can't stop one: the session's own Stop stops them all."""
    await engine.meta(session_id)
    working = await engine.store.read(lambda conn: conn.execute(
        f"SELECT id, native_run_id FROM runs WHERE session_id = ? AND state IN ({', '.join('?' * len(records.LIVE))}) "
        "ORDER BY seq", (session_id, *records.LIVE)).fetchall())
    sent = set(await engine.stop_runs(session_id, [native_run for _id, native_run in working], by=by))
    return [run_id for run_id, native_run in working if native_run in sent]


async def counts(engine: Engine, session_ids: Sequence[str], *,
                 kinds: Sequence[str] = ()) -> dict[str, dict[str, int]]:
    """Each session's run counts (``{running, waiting, done, failed,
    stopped, lost, total, tokens}``) in one read, with the working runs'
    tokens so far: the aggregate on every thread (ch 14 §4.1)."""
    _check("kind", kinds, KINDS)
    ids = list(dict.fromkeys(session_ids))
    out, live = await engine.store.read(lambda conn: (records.counts(conn, ids, kinds=kinds),
                                                      _live_natives(conn, ids, kinds)))
    progress = {s: engine.live_progress(s) for s in {s for s, _n in live}}
    for session_id, native_run in live:
        if (p := progress[session_id].get(native_run)) is not None:
            out[session_id]["tokens"] += p.tokens
    return out


# ── helpers ──────────────────────────────────────────────────────────────────


def _check(name: str, values: Sequence[str], allowed: Sequence[str]) -> None:
    unknown = [v for v in values if v not in allowed]
    if unknown:
        raise RunsRefused("bad_filter", f"{name} is one of {', '.join(allowed)}, not {', '.join(unknown)}.")


async def _stoppable(engine: Engine, session_id: str) -> bool:
    try:
        return native((await engine.features(session_id)).features, "runs.stop")
    except SessionRefused:              # no runtime for its agent in this server: nothing can stop it here
        return False


def _live_natives(conn, session_ids: Sequence[str], kinds: Sequence[str]) -> list[tuple[str, str]]:
    if not session_ids:
        return []
    kind_clause = f" AND kind IN ({', '.join('?' * len(kinds))})" if kinds else ""
    return conn.execute(f"SELECT session_id, native_run_id FROM runs WHERE session_id IN "
                        f"({', '.join('?' * len(session_ids))}) AND state IN ('running', 'waiting'){kind_clause}",
                        (*session_ids, *kinds)).fetchall()


def _view(row: Sequence[Any], stoppable: bool, progress: Mapping[str, Progress]) -> dict[str, Any]:
    view = records.view(row)
    live = view["state"] in records.LIVE
    if live and (p := progress.get(view["native_run_id"])) is not None:
        view.update(tokens=p.tokens, tool_calls=p.tool_calls, last_tool=p.last_tool or view["last_tool"],
                    last_activity_at=p.at or view["last_activity_at"])
    view["can_stop"] = stoppable and live
    return view


def _subtree(conn, top: Sequence[str], depth: int) -> tuple[list, dict[str, tuple[int, int, int]]]:
    """The descendants of ``top`` within ``depth`` levels in all (in start
    order), and every node's direct children's counts."""
    if not top:
        return [], {}
    tree = ("WITH RECURSIVE tree(id, level) AS (SELECT id, 0 FROM runs WHERE id IN "
            f"({', '.join('?' * len(top))}) UNION ALL SELECT r.id, tree.level + 1 FROM runs r JOIN tree "
            "ON r.parent_run_id = tree.id WHERE tree.level + 1 < ?) ")
    below = conn.execute(tree + f"SELECT {', '.join('runs.' + n for n in records.NAMES)} FROM runs "
                         "JOIN tree ON tree.id = runs.id WHERE tree.level > 0 ORDER BY runs.seq",
                         (*top, depth)).fetchall()
    children = conn.execute(tree + "SELECT parent_run_id, count(*), sum(state = 'running'), sum(state = 'waiting') "
                            "FROM runs WHERE parent_run_id IN (SELECT id FROM tree) GROUP BY parent_run_id",
                            (*top, depth)).fetchall()
    return below, {parent: tuple(numbers) for parent, *numbers in children}


def _nest(top: list[dict[str, Any]], below: list[dict[str, Any]],
          children: Mapping[str, tuple[int, int, int]]) -> list[dict[str, Any]]:
    nodes = {v["id"]: v for v in (*top, *below)}
    for node in nodes.values():
        node["children"] = []
        node["child_counts"] = dict(zip(("total", "running", "waiting"), children.get(node["id"], (0, 0, 0))))
    for node in below:                  # in start order, so each parent's children are too
        nodes[node["parent_run_id"]]["children"].append(node)
    return top


def _ancestry(parents: Sequence[tuple], where: tuple | None) -> dict[str, Any]:
    """The path from the Space down to the run (ch 14 §11.1)."""
    runs = [dict(zip(("id", "title", "kind", "depth", "state"), p)) for p in parents]
    if where is None:
        return {"chat": None, "thread": None, "session": None, "runs": runs}
    session_id, role, preset, agent, status, chat_id, thread_ts, chat_kind, project_pid, project_name, title = where
    return {
        "chat": {"id": chat_id, "kind": chat_kind, "project_pid": project_pid, "name": project_name}
        if chat_id else None,
        "thread": {"ts": thread_ts, "title": title} if thread_ts else None,
        "session": {"id": session_id, "role": role, "preset": preset, "agent": agent, "state": status},
        "runs": runs,
    }


def _cut_outputs(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """The page's normalised message shape (as ``read_session`` gives it: a
    message whose content is a list of blocks, a tool's output a
    ``tool_result`` block), each tool output cut to
    ``TOOL_OUTPUT_MAX_BYTES`` with its size in ``bytes``."""
    message = item.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return item
    return {**item, "message": {**message, "content": [_cut_output(block) for block in content]}}


def _cut_output(block: Any) -> Any:
    if not isinstance(block, Mapping) or block.get("type") != "tool_result":
        return block
    body = block.get("content")
    raw = "" if body is None else body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    size = len(raw.encode())
    if size <= TOOL_OUTPUT_MAX_BYTES:
        return {**block, "bytes": size, "truncated": False}
    text = raw if not isinstance(body, list) else "\n".join(
        str(part.get("text", "")) for part in body if isinstance(part, Mapping) and part.get("type") == "text")
    kept = text.encode()[:TOOL_OUTPUT_MAX_BYTES].decode("utf-8", errors="ignore")
    return {**block, "content": kept, "bytes": size, "truncated": True}
