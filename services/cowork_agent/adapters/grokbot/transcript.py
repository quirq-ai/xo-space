"""The gateway's transcript envelopes and turn correlation keys."""
from __future__ import annotations

from typing import Any

from services.cowork_agent.adapters.grokbot.gateway import GrokbotGatewayError


def transcript_entries(payload: Any) -> list[dict]:
    rows = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise GrokbotGatewayError("Grok Bot host returned an invalid transcript.")
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise GrokbotGatewayError("Grok Bot host returned an invalid transcript entry.")
        if isinstance(row.get("entry"), dict):
            row = {**row, **row["entry"]}
        entries.append(row)
    return entries


def message_text(entry: dict) -> tuple[str, str] | None:
    if entry.get("streaming") is True:
        return None
    if entry.get("kind") == "send-message":
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("type") != "text":
            return None
        role, text = "assistant", message.get("content")
    elif entry.get("kind") == "message" and entry.get("role") == "user":
        role, text = "user", entry.get("content")
    else:
        return None
    return (role, text) if isinstance(text, str) and text.strip() else None


def current_turn_reply(
    entries: list[dict], client_nonce: str, request_id: str | None = None,
) -> tuple[str | None, str]:
    """Bind once by nonce, then retain the request ID if the window slides."""
    for entry in entries:
        if entry.get("kind") == "message" and entry.get("role") == "user":
            if not entry.get("clientNonce") or not entry.get("requestId"):
                raise GrokbotGatewayError(
                    "Grok Bot transcript prompt is missing clientNonce/requestId; "
                    "cannot safely identify this turn. Check the host gateway version."
                )
        if entry.get("clientNonce") == client_nonce:
            candidate = entry.get("requestId")
            if not isinstance(candidate, str) or not candidate:
                raise GrokbotGatewayError("Grok Bot transcript prompt is missing requestId.")
            if request_id is not None and candidate != request_id:
                raise GrokbotGatewayError("Grok Bot transcript has conflicting requestIds for this nonce.")
            request_id = candidate

    texts = []
    for entry in entries:
        message = message_text(entry)
        if not message or message[0] != "assistant":
            continue
        if not entry.get("requestId"):
            raise GrokbotGatewayError(
                "Grok Bot transcript reply is missing requestId; cannot safely identify this turn."
            )
        if request_id is not None and entry["requestId"] == request_id:
            texts.append(message[1])
    return request_id, "\n\n".join(texts)
