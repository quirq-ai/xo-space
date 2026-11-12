"""Every ``xo_comms`` tool's handler, by name (the tools' table is
``tools.py``; which role has which is ``tools.ROLE_TOOLS``)."""

from __future__ import annotations

from typing import Mapping

from services.cowork_agent.storm.comms import jobs, owner, talk, timers, work
from services.cowork_agent.storm.comms.calls import Handler

HANDLERS: Mapping[str, Handler] = {
    "post_message": talk.post_message, "reply": talk.reply, "update_status": talk.update_status,
    "react": talk.react, "no_reply_needed": talk.no_reply_needed, "no_such_project": talk.no_such_project,
    "forward_to_project": talk.forward_to_project, "create_task": talk.create_task,
    "message_thread": talk.message_thread, "stop_thread": talk.stop_thread,
    "continue_thread": talk.continue_thread, "set_thread_resolved": talk.set_thread_resolved,
    "rename_thread": talk.rename_thread, "set_thread_label": talk.set_thread_label,
    "list_projects": talk.list_projects, "fetch_project_card": talk.fetch_project_card,
    "list_thread_sessions": talk.list_thread_sessions, "fetch_thread": talk.fetch_thread,
    "fetch_timeline": talk.fetch_timeline, "fetch_messages": talk.fetch_messages,
    "ask": owner.ask, "withdraw_ask": owner.withdraw_ask, "list_review": owner.list_review,
    "annotate_review": owner.annotate_review, "propose_project": owner.propose_project,
    "read_memory": work.read_memory, "update_memory": work.update_memory, "add_todo": work.add_todo,
    "update_todo": work.update_todo, "list_todos": work.list_todos, "share_files": work.share_files,
    "report": work.report, "submit_result": work.submit_result, "set_goal": work.set_goal,
    "read_state": work.read_state, "handoff_note": work.handoff_note,
    "create_job": jobs.create_job, "list_jobs": jobs.list_jobs, "cancel_job": jobs.cancel_job,
    "wake_me": timers.wake_me, "cancel_wake": timers.cancel_wake, "lease": timers.lease, "release": timers.release,
}
