"""What the store keeps of a session's sub-agents (design ch 14 §3; ch 08 §2),
as SQL on the store's thread: the ``runs`` rows and their ``run.*`` events,
each change inside the one transaction of the write that causes it (the
engine's ``Writes``, or boot recovery's ``Tx``: both ``execute`` and
``append``).

A row's id is the store's (``run_…``); ``native_run_id`` is the runtime's.
Every event's ``run_id`` column names the runtime's run, as the engine's
other events do (PR 7), and a ``run.*`` event's data names the store's:

* ``run.started {run_id, parent_run_id, state, source, info}``: a row was
  made (``source`` ``events``, or ``disk`` for a run found only in the CLI's
  files), or an ended run runs again. ``info`` is the runtime's ``RunInfo``.
* ``run.state {run_id, state, stop_requested_at, stopped_by}``: a live run
  waits on the person (``waiting``) or works again (``running``), or the
  person asked to stop it (the state is unchanged until its end event).
* ``run.finished {run_id, state, status, summary, usage, tokens, tool_calls,
  last_tool, stopped_by}``: it ended. ``state`` is ``finished``, ``failed``,
  ``stopped`` or ``lost`` (its process ended without saying how it went);
  ``status`` is the runtime's (``completed`` | ``failed`` | ``stopped``),
  null for ``lost`` and for a run found ended on disk.

Not stored: internal runs (``RunInfo.hidden``), and a run's transcript,
prompt, tool inputs and outputs, which stay in its CLI's files (ch 14 §3).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.runs.live import Progress

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.records import Ids

LIVE = ("running", "waiting")
ENDED = ("finished", "failed", "stopped", "lost")
STATES = LIVE + ENDED
#: The runtime's end status as a row's state.
END_STATE: Mapping[str, str] = {"completed": "finished", "failed": "failed", "stopped": "stopped"}
#: A run's one-line summary as stored (ch 14 §3).
SUMMARY_MAX = 280
ID_HEX = 16
#: The columns ``view`` reads, in its order.
COLUMNS = ("id, seq, session_id, turn_id, parent_run_id, native_run_id, spawn_call_id, kind, agent_type, title, "
           "background, depth, state, source, started_at, ended_at, last_activity_at, tokens, tool_calls, "
           "last_tool, summary, transcript_ref, stop_requested_at, stopped_by, epoch")
NAMES = tuple(name.strip() for name in COLUMNS.split(","))


def new_id() -> str:
    return f"run_{uuid.uuid4().hex[:ID_HEX]}"


# ── changes (inside a write) ─────────────────────────────────────────────────


def started(w: Any, ids: "Ids", info: c.RunInfo, *, turn_id: str | None, epoch: int, native_id: str | None,
            waiting: bool = False) -> str | None:
    """A run began: its row and ``run.started``, ``waiting`` when a call of
    its own is already held for the person (the call can reach the host
    before the start is written). Returns its store id; None for an
    internal run, which is never stored. A run the store already has (found
    on disk first) is the same run: a live one is left as it is, an ended
    one works again."""
    state = "waiting" if waiting else "running"
    if info.hidden:
        return None
    row = w.execute("SELECT id, state, parent_run_id, source FROM runs WHERE session_id = ? AND native_run_id = ?",
                    (ids.session_id, info.run_id)).fetchone()
    if row is not None:
        run_id, stored, parent, source = row
        if stored not in LIVE:
            w.execute("UPDATE runs SET state = ?, ended_at = NULL, turn_id = coalesce(?, turn_id), epoch = ?, "
                      "last_activity_at = ?, stop_requested_at = NULL, stopped_by = NULL WHERE id = ?",
                      (state, turn_id, epoch, w.now, run_id))
            _started_event(w, ids, run_id, parent, info, state, source, turn_id, epoch)
        return run_id
    parent = _store_id(w, ids.session_id, info.parent_run_id)
    run_id = new_id()
    seq = _started_event(w, ids, run_id, parent, info, state, "events", turn_id, epoch)
    _insert(w, ids.session_id, run_id, seq, info, parent=parent, state=state, source="events", turn_id=turn_id,
            epoch=epoch, native_id=native_id)
    return run_id


def found(w: Any, ids: "Ids", infos: Iterable[c.RunInfo], *, epoch: int, native_id: str | None,
          state_of: Callable[[str], str]) -> list[tuple[str, str]]:
    """Runs the CLI's files show that the store doesn't (ch 14 §2.2): stored
    with ``source`` ``disk``, each in the state ``state_of`` its native id
    gives (an ended one also gets its ``run.finished``; its times are when XO
    found it). Parents first, so a child links to its parent's row. Returns
    (native id, state) of each one added."""
    added: list[tuple[str, str]] = []
    for info in sorted(infos, key=lambda i: i.depth):
        if info.hidden or _store_id(w, ids.session_id, info.run_id) is not None:
            continue
        state = state_of(info.run_id)
        parent = _store_id(w, ids.session_id, info.parent_run_id)
        run_id = new_id()
        seq = _started_event(w, ids, run_id, parent, info, state, "disk", None, epoch)
        _insert(w, ids.session_id, run_id, seq, info, parent=parent, state=state, source="disk", turn_id=None,
                epoch=epoch, native_id=native_id)
        if state in ENDED:
            w.execute("UPDATE runs SET ended_at = ? WHERE id = ?", (w.now, run_id))
            w.append("run.finished", _finished_data(run_id, state, None, None, None, 0, 0, None, None),
                     **ids.columns(run_id=info.run_id, epoch=epoch))
        added.append((info.run_id, state))
    return added


def finished(w: Any, ids: "Ids", event: c.RunFinished, *, epoch: int, progress: Progress | None,
             process_ended: bool = False) -> str | None:
    """A run ended (``RunFinished``): its row closes with its numbers (the
    runtime's usage, else its last progress) and ``run.finished``. A stop
    the person asked for names them; a run that ended otherwise doesn't. A
    run the runtime reports stopped because its process ended
    (``process_ended``: a restart, a crash) and nobody asked is ``lost``
    (ch 14 §5.3). Returns the state stored; None when the store shows no
    live run by that id (an internal one)."""
    row = w.execute("SELECT id, stopped_by FROM runs WHERE session_id = ? AND native_run_id = ? AND state IN "
                    "('running', 'waiting')", (ids.session_id, event.run)).fetchone()
    if row is None:
        return None
    run_id, requested_by = row
    state = END_STATE[event.status]
    if state == "stopped" and requested_by is None and process_ended:
        state = "lost"
    progress = progress or Progress()
    tokens = usage_tokens(event.usage) if event.usage is not None else progress.tokens
    stopped_by = requested_by if state == "stopped" else None
    summary = event.summary[:SUMMARY_MAX] if event.summary else None
    w.execute("UPDATE runs SET state = ?, ended_at = ?, last_activity_at = coalesce(?, last_activity_at), tokens = ?, "
              "tool_calls = ?, last_tool = coalesce(?, last_tool), summary = ?, stopped_by = ? WHERE id = ?",
              (state, w.now, progress.at, tokens, progress.tool_calls, progress.last_tool, summary, stopped_by,
               run_id))
    untold(w, ids.session_id, state == "lost")
    usage = dataclasses.asdict(event.usage) if event.usage is not None else None
    w.append("run.finished", _finished_data(run_id, state, event.status, summary, usage, tokens, progress.tool_calls,
                                            progress.last_tool, stopped_by),
             **ids.columns(turn_id=event.turn_id, run_id=event.run, epoch=epoch))
    return state


def gone(w: Any, ids: "Ids", runs: Iterable[str], *, epoch: int) -> list[str]:
    """Runs known only from the CLI's lists of what runs, which a later list
    no longer names: they ended. One someone asked to stop closes
    ``stopped``, naming them (the stop is confirmed, as a run's own end
    confirms it); any other ``finished`` with no status, as a run found
    ended on disk does. Never ``lost``, so nobody is told a sub-agent was
    lost. Returns their ids."""
    closed = []
    for run in runs:
        row = w.execute("UPDATE runs SET state = CASE WHEN stopped_by IS NULL THEN 'finished' ELSE 'stopped' END, "
                        "ended_at = ? WHERE session_id = ? AND native_run_id = ? AND state IN ('running', 'waiting') "
                        "RETURNING id, state, tokens, tool_calls, last_tool, stopped_by",
                        (w.now, ids.session_id, run)).fetchone()
        if row is None:
            continue
        run_id, state, tokens, tool_calls, last_tool, stopped_by = row
        w.append("run.finished", _finished_data(run_id, state, None, None, None, tokens, tool_calls, last_tool,
                                                stopped_by), **ids.columns(run_id=run, epoch=epoch))
        closed.append(run_id)
    return closed


def set_waiting(w: Any, ids: "Ids", run: str, *, waiting: bool, turn_id: str | None, epoch: int) -> None:
    """A live run's call is held for the person (``waiting``), or no longer
    is (``running``): ``run.state``. Nothing for a run the store doesn't show."""
    new, old = ("waiting", "running") if waiting else ("running", "waiting")
    row = w.execute("UPDATE runs SET state = ? WHERE session_id = ? AND native_run_id = ? AND state = ? "
                    "RETURNING id, stop_requested_at, stopped_by", (new, ids.session_id, run, old)).fetchone()
    if row is not None:
        w.append("run.state", {"run_id": row[0], "state": new, "stop_requested_at": row[1], "stopped_by": row[2]},
                 **ids.columns(turn_id=turn_id, run_id=run, epoch=epoch))


def request_stop(w: Any, ids: "Ids", runs: Iterable[str], *, by: str, epoch: int) -> list[str]:
    """The person (or a session's Stop) asked these live runs to stop: the
    time and who, with ``run.state``. The run stays as it is until its own
    end event confirms the stop (ch 14 §4.3). Returns the store ids."""
    asked = []
    for run in runs:
        row = w.execute("UPDATE runs SET stop_requested_at = ?, stopped_by = ? WHERE session_id = ? AND "
                        "native_run_id = ? AND state IN ('running', 'waiting') RETURNING id, state",
                        (w.now, by, ids.session_id, run)).fetchone()
        if row is None:
            continue
        w.append("run.state", {"run_id": row[0], "state": row[1], "stop_requested_at": w.now, "stopped_by": by},
                 **ids.columns(run_id=run, epoch=epoch))
        asked.append(row[0])
    return asked


def lose(w: Any, *, now: str, session_id: str | None = None, before_epoch: int | None = None) -> dict[str, list[str]]:
    """Live runs whose process is gone without saying how they ended become
    ``lost``, with ``run.finished``: every one (boot recovery), one
    session's, or one session's from processes before ``before_epoch``.
    Returns the store ids per session."""
    rows = w.execute(
        "SELECT r.id, r.session_id, r.native_run_id, r.turn_id, r.epoch, r.tokens, r.tool_calls, r.last_tool, "
        "s.chat_id, s.thread_ts FROM runs r JOIN sessions s ON s.id = r.session_id "
        "WHERE r.state IN ('running', 'waiting') AND (:session IS NULL OR r.session_id = :session) "
        "AND (:before IS NULL OR r.epoch < :before) ORDER BY r.seq",
        {"session": session_id, "before": before_epoch}).fetchall()
    lost: dict[str, list[str]] = {}
    for run_id, session, native, turn_id, epoch, tokens, tool_calls, last_tool, chat_id, thread_ts in rows:
        w.execute("UPDATE runs SET state = 'lost', ended_at = ?, stopped_by = NULL WHERE id = ?", (now, run_id))
        w.append("run.finished", _finished_data(run_id, "lost", None, None, None, tokens, tool_calls, last_tool, None),
                 chat_id=chat_id, session_id=session, thread_ts=thread_ts, turn_id=turn_id, run_id=native,
                 epoch=epoch)
        lost.setdefault(session, []).append(run_id)
    for session, run_ids in lost.items():
        untold(w, session, len(run_ids))
    return lost


def lost_note(count: int) -> str:
    """What a resume note says of the runs a restart stopped (ch 14 §5.3)."""
    return f"{count} sub-agent{'s' if count != 1 else ''} stopped when the session restarted"


def untold(w: Any, session_id: str, count: int) -> None:
    """Every run XO saw working that is made ``lost`` counts here, in the
    transaction that marks it: the session's next input says how many
    (ch 14 §5.3) and clears the count as it is handed to its turn. A run
    only found on disk after its process was gone doesn't: nobody saw it
    work."""
    if count:
        w.execute("UPDATE sessions SET runs_lost_untold = runs_lost_untold + ? WHERE id = ?", (count, session_id))


def untold_note(count: int) -> str:
    """Said before the session's next input (a handed-back one carries its
    resume note right after)."""
    return f"{lost_note(count)}; check what they did before relying on it."


def usage_tokens(usage: c.TokenUsage) -> int:
    return usage.input + usage.output + usage.cache_read + usage.cache_write


# ── reads (on a store connection) ────────────────────────────────────────────


def counts(conn: "sqlite3.Connection", session_ids: Sequence[str], *,
           kinds: Sequence[str] = ()) -> dict[str, dict[str, int]]:
    """Each session's runs by state, in one query: ``{running, waiting,
    done, failed, stopped, lost, total, tokens}`` (``tokens`` as stored: a
    live run's join at its end). Every session asked for is in the result.
    ``kinds`` narrows it to those run kinds."""
    out = {s: dict.fromkeys(("running", "waiting", "done", "failed", "stopped", "lost", "total", "tokens"), 0)
           for s in session_ids}
    if not session_ids:
        return out
    marks = ", ".join("?" * len(session_ids))
    kind_clause = f" AND kind IN ({', '.join('?' * len(kinds))})" if kinds else ""
    for session, *numbers in conn.execute(
            "SELECT session_id, sum(state = 'running'), sum(state = 'waiting'), sum(state = 'finished'), "
            "sum(state = 'failed'), sum(state = 'stopped'), sum(state = 'lost'), count(*), sum(tokens) FROM runs "
            f"WHERE session_id IN ({marks}){kind_clause} GROUP BY session_id", (*session_ids, *kinds)):
        out[session] = dict(zip(("running", "waiting", "done", "failed", "stopped", "lost", "total", "tokens"),
                                numbers))
    return out


def view(row: Sequence[Any]) -> dict[str, Any]:
    """A row as the routes give it (``COLUMNS`` order)."""
    data = dict(zip(NAMES, row))
    data["background"] = bool(data["background"])
    data["transcript_ref"] = json.loads(data["transcript_ref"]) if data["transcript_ref"] else None
    return data


# ── helpers ──────────────────────────────────────────────────────────────────


def _store_id(w: Any, session_id: str, native: str | None) -> str | None:
    if not native:
        return None
    row = w.execute("SELECT id FROM runs WHERE session_id = ? AND native_run_id = ?", (session_id, native)).fetchone()
    return row[0] if row else None


def _insert(w: Any, session_id: str, run_id: str, seq: int, info: c.RunInfo, *, parent: str | None, state: str,
            source: str, turn_id: str | None, epoch: int, native_id: str | None) -> None:
    # The transcript pointer names the CLI session the run belongs to, which
    # outlives a later change of the session's own native id.
    ref = json.dumps({"native_id": native_id, "run": info.run_id}) if native_id else None
    w.execute("INSERT INTO runs (id, seq, session_id, turn_id, parent_run_id, native_run_id, spawn_call_id, kind, "
              "agent_type, title, background, depth, state, source, started_at, last_activity_at, transcript_ref, "
              "epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, json(?), ?)",
              (run_id, seq, session_id, turn_id, parent, info.run_id, info.spawn_call_id, info.kind, info.agent_type,
               info.title, int(info.background), info.depth, state, source, w.now, w.now, ref, epoch))


def _started_event(w: Any, ids: "Ids", run_id: str, parent: str | None, info: c.RunInfo, state: str, source: str,
                   turn_id: str | None, epoch: int) -> int:
    return w.append("run.started", {"run_id": run_id, "parent_run_id": parent, "state": state, "source": source,
                                    "info": dataclasses.asdict(info)},
                    **ids.columns(turn_id=turn_id, run_id=info.run_id, epoch=epoch))


def _finished_data(run_id: str, state: str, status: str | None, summary: str | None,
                   usage: Mapping[str, Any] | None, tokens: int, tool_calls: int, last_tool: str | None,
                   stopped_by: str | None) -> dict[str, Any]:
    return {"run_id": run_id, "state": state, "status": status, "summary": summary, "usage": usage,
            "tokens": tokens, "tool_calls": tool_calls, "last_tool": last_tool, "stopped_by": stopped_by}
