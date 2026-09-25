"""A local file-reading policy exposed through the Space agent contract."""
from __future__ import annotations

import asyncio
import html
import json
import uuid
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.adapters.fly import artifacts, runtime, sessions
from services.cowork_agent.helpers import strip_workspace_preamble
from services.errors import ServiceError

HELP = (
    "This fly ranks local JSON/CSV files and extracts fields into a cited report.\n\n"
    "Send `/run` to use its trained task template, or supply explicit overrides:\n\n"
    '```text\n/run {"query":"workspace job failures","fields":["job","status","error_code"],"maxReads":4}\n```\n\n'
    "Only query, fields, and maxReads can change. The selected project and fly revision stay pinned. "
    "This version does not interpret general instructions, run shell commands, modify project files, or repair failures."
)


def parse_task(question: str, template: dict) -> dict | None:
    if not isinstance(question, str) or len(question.encode("utf-8")) > 16_000:
        raise ServiceError("invalid_task", "The fly command must be text smaller than 16 KB.")
    question = strip_workspace_preamble(question).strip()
    if question in {"/help", "help"}:
        return None
    if question == "/run":
        return dict(template)
    if not question.startswith("/run "):
        raise ServiceError("explicit_task_required", "Use /run for the saved task, /run {\"fields\":[...],\"query\":\"...\",\"maxReads\":4} for overrides, or /help.")
    try:
        override = json.loads(question[5:])
    except ValueError as exc:
        raise ServiceError("invalid_task", "The /run overrides must be a JSON object.") from exc
    if not isinstance(override, dict) or set(override) - {"query", "fields", "maxReads"}:
        raise ServiceError("invalid_task", "Supported overrides are query, fields, and maxReads. Paths, tools, and commands cannot be supplied.")
    task = {**template, **override}
    # The shared runtime applies the full canonical field/size contract before reading.
    if (not isinstance(task.get("query"), str)
            or not isinstance(task.get("fields"), list) or not 1 <= len(task["fields"]) <= 12
            or any(not isinstance(field, str) or not field for field in task["fields"])
            or type(task.get("maxReads")) is not int or not 1 <= task["maxReads"] <= 64):
        raise ServiceError("invalid_task", "Use a text query, 1–12 field names, and an integer maxReads from 1 to 64.")
    return task


def pinned_workspace(session: dict):
    workspace = sessions.workspace_for(session["project_id"])
    info = workspace.stat()
    if (str(workspace) != session["workspace"]
            or sessions.project_layout.runtime_key(session["project_id"]) != session["pid"]
            or [info.st_dev, info.st_ino] != session["workspace_identity"]):
        raise ServiceError("workspace_changed", "The pinned project binding changed. Start a new session.", 409)
    return workspace


def _cell(value: Any, limit: int = 700) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "… [truncated]"
    text = html.escape(text, quote=False).replace("|", "&#124;")
    for char in "\\`*_[]{}()#!":
        text = text.replace(char, "\\" + char)
    return text


def format_report(result: dict, session: dict, run_id: str) -> str:
    report = result["report"]
    scope = report["scope"]
    lines = [f"### {_cell(session['fly_name'])} · {_cell(report['status'])} report", "",
             _cell(report["reason"]), "",
             f"Inspected **{scope['inspectedFiles']} / {scope['eligibleFiles']}** eligible files. "
             f"Extracted **{len(report['records'])}** records. No policy weights changed.", ""]
    records = report["records"]
    fields = report["fields"]
    if records:
        lines += ["| " + " | ".join([*map(_cell, fields), "Source"]) + " |",
                  "| " + " | ".join(["---"] * (len(fields) + 1)) + " |"]
        shown = 0
        for record in records[:50]:
            source = record["source"]
            row = "| " + " | ".join([*[_cell(record["values"][field]) for field in fields],
                                      _cell(f"{source['path']} · {source['location']} · SHA-256 {source['contentHash']}")]) + " |"
            if sum(len(line) for line in lines) + len(row) > 60_000:
                break
            lines.append(row)
            shown += 1
        lines += ["", f"Showing {shown} of {len(records)} records; long cell values are marked when truncated."]
    else:
        lines.append("No complete records with all requested fields were found in the files inspected.")
    if report.get("errors"):
        lines += ["", "Read errors:", *[f"- {_cell(e['path'])}: {_cell(e['message'])}" for e in report["errors"][:64]]]
    if report.get("missingFields"):
        lines += ["", "Fields not observed: " + ", ".join(map(_cell, report["missingFields"])) + "."]
    lines += ["", f"[Full report and trace](/api/fly/runs/{run_id})", "",
              f"Run `{run_id}` · revision {session['revision']} · artifact `{session['artifact_digest']}`", "",
              "These are extracted observations, not a root-cause diagnosis. Unread files may contain additional evidence."]
    return "\n".join(lines)


class FlyAdapter(BaseAgentAdapter):
    @property
    def adapter_name(self) -> str:
        return "fly"

    async def prepare(self, question: str, *, our_session_id: str | None = None,
                      agent_id: str | None = None, model: str | None = None,
                      is_new_session: bool = False, **kwargs) -> dict:
        sid = sessions.checked_id(our_session_id or str(uuid.uuid4()))
        previous = sessions.load(sid)
        if previous:
            if is_new_session:
                raise ServiceError("session_exists", "The session already exists.", 409)
            if agent_id is not None and agent_id != previous["project_id"]:
                raise ServiceError("project_mismatch", "This session is pinned to another project. Start a new session.", 409)
            if model is not None and model != previous["model"]:
                raise ServiceError("fly_mismatch", "This session is pinned to another fly. Start a new session.", 409)
            pinned_workspace(previous)
            artifact = await artifacts.load_snapshot(previous["artifact_digest"])
            session = previous
        else:
            if our_session_id and not is_new_session:
                raise ServiceError("session_not_found", "The fly session does not exist.", 404)
            workspace = sessions.workspace_for(agent_id)
            selected = await artifacts.resolve_artifact(model)
            artifact = selected["artifact"]
            task = parse_task(question, artifact["task"])
            session = sessions.create(sid, agent_id, workspace, selected)
            return {"session": session, "artifact": artifact, "task": task, "question": question}
        return {"session": session, "artifact": artifact, "task": parse_task(question, artifact["task"]), "question": question}

    async def stream(self, question: str, session_id: str | None = None, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        prepared = kwargs.get("prepared") or await self.prepare(question, our_session_id=kwargs.get("our_session_id") or session_id,
                                                                agent_id=kwargs.get("agent_id"), model=kwargs.get("model"),
                                                                is_new_session=kwargs.get("is_new_session", session_id is None and not kwargs.get("our_session_id")))
        sid = prepared["session"]["id"]
        cancel_event = kwargs.get("cancel_event") or asyncio.Event()
        with sessions.turn_lease(sid):
            session = sessions.load(sid)
            sessions.recover(session)
            if len(session["runs"]) >= 100:
                raise ServiceError("session_full", "This session has reached 100 runs. Start a new session.", 409)
            run_id = str(uuid.uuid4())
            session["runs"].append({"id": run_id, "status": "running", "started_at": sessions.now()})
            session["title"] = session["title"] if session["messages"] else strip_workspace_preamble(question).strip()[:80]
            sessions.append_message(session, "user", strip_workspace_preamble(question).strip(), run_id=run_id)
            sessions.save(session)
            try:
                if cancel_event.is_set():
                    raise asyncio.CancelledError()
                if prepared["task"] is None:
                    result, message = None, HELP
                else:
                    yield {"type": "model-loading", "label": "Inspecting local file metadata with the pinned fly policy"}
                    result = await asyncio.wait_for(runtime.execute(prepared["artifact"], pinned_workspace(session),
                                                                   task=prepared["task"], cancel_event=cancel_event,
                                                                   workspace_identity=session["workspace_identity"]), timeout=120)
                    if cancel_event.is_set():
                        raise asyncio.CancelledError()
                    message = format_report(result, session, run_id)
                sessions.finish(session, run_id, "completed", message, result)
            except asyncio.CancelledError:
                sessions.finish(session, run_id, "cancelled", "Fly inspection cancelled. No further files were read and no report is claimed as complete.")
                raise
            except Exception as exc:
                message = "Fly inspection failed: " + (str(exc) if isinstance(exc, (ValueError, ServiceError)) else "the local runtime could not complete the inspection. Check the installed Node runtime and file access, then retry explicitly.")
                committed = sessions.finish(session, run_id, "failed", message)
                if committed["status"] == "completed":
                    # The report can be durable even if the following session
                    # index write failed. finish reconciles that existing record.
                    message = committed["message"]
                else:
                    yield {"type": "error", "error": message}
                    yield {"done": True, "native_session_id": sid, "run_id": run_id}
                    return
        # Persistence completes before the first final token or done notification.
        for offset in range(0, len(message), 2000):
            yield {"type": "token", "token": message[offset:offset + 2000]}
        yield {"done": True, "native_session_id": sid, "run_id": run_id}

    async def run(self, question: str, session_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        output = {"message": "", "native_session_id": session_id}
        async for event in self.stream(question, session_id, **kwargs):
            if event.get("type") == "token":
                output["message"] += event["token"]
            if event.get("type") == "error":
                output["error"] = event["error"]
                output["message"] = event["error"]
            if event.get("done"):
                output.update(native_session_id=event["native_session_id"], run_id=event.get("run_id"))
        return output

    async def health(self) -> dict[str, Any]:
        try:
            catalog = await artifacts.reload_catalog()
            return {"ok": bool(catalog["flies"]), "flies": len(catalog["flies"]), "errors": catalog["errors"],
                    "message": "Copy a trained *.fly.json into the configured .quirq/flies directory, then reload." if not catalog["flies"] else "Local Report Scout runtime ready."}
        except Exception:
            return {"ok": False, "message": "Fly discovery failed. Install Node 20+ and check the state directory and FLY_NODE_PATH."}


Adapter = FlyAdapter
