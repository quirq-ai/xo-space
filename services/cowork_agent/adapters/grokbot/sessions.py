"""Space-indexed Grok Bot sessions with text history read from the gateway."""
from __future__ import annotations

import logging
from pathlib import Path

from services.cowork_agent.adapters.grokbot.gateway import GrokbotGatewayError, GrokbotHistoryGateway
from services.cowork_agent.adapters.grokbot.session_seats import indexed_sessions, lookup_seat
from services.cowork_agent.adapters.grokbot.transcript import message_text, transcript_entries
from services.cowork_agent.helpers import iso_now, ms_to_iso, strip_workspace_preamble

logger = logging.getLogger(__name__)

USES_PROJECT_SESSIONS = True
_BACKEND = "grokbot"


def enrich_project_session(meta: dict, key: str, default_agent: str):
    created = meta.get("createdAt") or meta.get("updatedAt")
    return ms_to_iso(created) if created else None, meta.get("title"), default_agent


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    # Current hosts keep history behind the gateway, not in transcript files.
    return None


def list_native_sessions() -> list[dict]:
    # Space's index supplies titles/times without a transcript request per row.
    # Host-created seats are not imported into the Sessions tab.
    return []


def owns_session(session_id: str) -> bool:
    rows = indexed_sessions()
    return session_id in rows or any(row.get("nativeSessionId") == session_id for row in rows.values())


def _read_history(gateway: GrokbotHistoryGateway, agent_id: str) -> list[dict]:
    entries = transcript_entries(gateway.command("getAgentTranscript", {"id": agent_id}))
    if not entries:
        return []
    # getAgentTranscript can be an in-memory tail. Probe older entries rather
    # than assuming the array contains the beginning of the conversation.
    if any(type(entry.get("seq")) is not int for entry in entries):
        raise GrokbotGatewayError("Grok Bot history is missing seq; cannot page older messages.")
    by_seq = {entry["seq"]: entry for entry in entries}
    before_seq = min(by_seq)
    while before_seq > 1:
        payload = gateway.command("getAgentTranscriptPage", {
            "id": agent_id, "limit": 200, "beforeSeq": before_seq,
        })
        older = transcript_entries(payload)
        if not isinstance(payload, dict):
            raise GrokbotGatewayError("Grok Bot host returned an invalid transcript page.")
        if any(type(entry.get("seq")) is not int for entry in older):
            raise GrokbotGatewayError("Grok Bot history page is missing seq.")
        # Keep the latest copy if a page overlaps the initial window.
        for entry in older:
            by_seq.setdefault(entry["seq"], entry)
        next_seq = payload.get("nextBeforeSeq")
        if next_seq is None:
            break
        if type(next_seq) is not int or next_seq >= before_seq or not older:
            raise GrokbotGatewayError("Grok Bot history pagination did not advance.")
        before_seq = next_seq
    return [by_seq[seq] for seq in sorted(by_seq)]


def get_messages(session_id: str) -> list:
    agent_id = lookup_seat(session_id)
    if not agent_id:
        if not owns_session(session_id):
            return []
        agent_id = session_id
    try:
        with GrokbotHistoryGateway() as gateway:
            entries = _read_history(gateway, agent_id)
    except GrokbotGatewayError as exc:
        # Gateway errors redact the token; omit the underlying HTTP traceback.
        logger.warning("Could not read Grok Bot history for session %s: %s", session_id, exc)
        return []
    return _convert_messages(session_id, entries)


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Recorded no-op: host seats have no Space workspace-directory field."""
    if not owns_session(session_id):
        return None
    return {
        "ok": True,
        "session_id": session_id,
        "directory": directory,
        "backend": _BACKEND,
        "applied": False,
    }


def _convert_messages(session_id: str, records: list[dict]) -> list[dict]:
    messages = []
    for record in records:
        parsed = message_text(record)
        if not parsed:
            continue  # The live gateway exposes text, not tool calls/results.
        role, text = parsed
        if role == "user":
            text = strip_workspace_preamble(text)
        if not text.strip():
            continue
        mid = f"{session_id}:{record['seq']}"
        timestamp = ms_to_iso(record["timestampMs"]) if record.get("timestampMs") is not None else iso_now()
        messages.append({
            "id": mid,
            "session_id": session_id,
            "time_created": timestamp,
            "data": {"role": role},
            "parts": [{
                "id": f"{mid}_p0",
                "message_id": mid,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"type": "text", "text": text},
            }],
        })
    return messages
