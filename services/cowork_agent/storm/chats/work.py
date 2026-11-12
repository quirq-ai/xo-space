"""The founder's current work snapshot for the future goals/timers panel.

Finished goals and unrelated finished tasks remain in the audit records;
the snapshot includes every unfinished task and every task of a live goal.
No agent session token or tool call is needed to read this view.
"""

from dataclasses import asdict

from services.cowork_agent.storm.records import goals, leases, tasks, timers


def _record(value):
    record = asdict(value)
    record.pop("request_key", None)
    record.pop("instructions", None)  # Thread detail carries the brief; this is a work overview.
    return record


async def snapshot(chats, chat_id: str):
    await chats.chat(chat_id)

    def read(conn):
        current_goals = [g for g in goals.of_chat(conn, chat_id) if g.state not in goals.FINAL]
        goal_ids = {g.id for g in current_goals}
        current_tasks = [t for t in tasks.of_chat(conn, chat_id, limit=-1)
                         if t.state in ("open", "stopped") or t.goal_id in goal_ids]
        current_timers = [timers.get(conn, row[0]) for row in conn.execute(
            "SELECT id FROM timers WHERE chat_id = ? AND state = 'armed' ORDER BY due_at IS NULL, due_at, id",
            (chat_id,))]
        held = [leases.get(conn, row[0]) for row in conn.execute(
            "SELECT l.id FROM leases l JOIN sessions s ON s.id = l.holder_session "
            "WHERE s.chat_id = ? AND l.released_at IS NULL ORDER BY l.acquired_at, l.id", (chat_id,))]
        return {"chat_id": chat_id, "through": conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0],
                "goals": [_record(g) for g in current_goals], "tasks": [_record(t) for t in current_tasks],
                "timers": [{**_record(t), "job": t.definition, "fired": t.last} for t in current_timers],
                "leases": [_record(item) for item in held]}
    return await chats.engine.store.read(read)
